from flask import Response
from urllib.parse import urlparse


def register_comfyui_admin_routes(app, ctx):
    root_or_403 = ctx["root_or_403"]
    actor_value = ctx["actor_value"]
    json_resp = ctx["json_resp"]
    require_csrf = ctx["require_csrf"]
    require_csrf_safe = ctx["require_csrf_safe"]
    get_client_ip = ctx["get_client_ip"]
    get_ua = ctx["get_ua"]
    audit = ctx["audit"]
    parse_comfyui_endpoint = ctx["parse_comfyui_endpoint"]
    local_start_script_status = ctx["local_start_script_status"]
    client_for_url = ctx["client_for_url"]
    ComfyUIError = ctx["ComfyUIError"]
    configured_connection_mode = ctx["configured_connection_mode"]
    start_local_comfyui = ctx["start_local_comfyui"]
    local_comfyui_runtime_status = ctx["local_comfyui_runtime_status"]
    normalize_civitai_nsfw_mode = ctx["normalize_civitai_nsfw_mode"]
    normalize_civitai_search_type = ctx["normalize_civitai_search_type"]
    inspect_civitai_model = ctx["inspect_civitai_model"]
    search_civitai_models = ctx["search_civitai_models"]
    fetch_civitai_media = ctx["fetch_civitai_media"]
    parse_civitai_download_request = ctx["parse_civitai_download_request"]
    coerce_bool = ctx["coerce_bool"]
    create_model_download_job = ctx["create_model_download_job"]
    capture_request_audit_meta = ctx["capture_request_audit_meta"]
    run_comfyui_model_download_job = ctx["run_comfyui_model_download_job"]
    download_civitai_model_selection = ctx["download_civitai_model_selection"]
    upload_comfyui_model_file = ctx["upload_comfyui_model_file"]
    assert_model_download_job_owner = ctx["assert_model_download_job_owner"]
    local_start_template_path = ctx["local_start_template_path"]
    send_file = ctx["send_file"]
    threading = ctx["threading"]

    @app.route("/api/root/comfyui/test-connection", methods=["POST"])
    @require_csrf
    def root_comfyui_test_connection():
        actor, err = root_or_403()
        if err:
            return err
        try:
            data = ctx["request"].get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        url, endpoint, msg = parse_comfyui_endpoint(data)
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        local_script_status = local_start_script_status(data) if isinstance(endpoint, dict) and endpoint.get("mode") == "local" else None
        active_client = client_for_url(url)
        try:
            status = active_client.health_check(timeout=3) if hasattr(active_client, "health_check") else {"ok": True}
            audit(
                "COMFYUI_CONNECTION_TEST",
                get_client_ip(),
                user=actor_value(actor, "username"),
                success=True,
                ua=get_ua(),
                detail=f"url={url}",
            )
            return json_resp({
                "ok": True,
                "available": True,
                "comfyui_url": getattr(active_client, "base_url", url),
                "endpoint": endpoint,
                "connection_mode": endpoint.get("mode") if isinstance(endpoint, dict) else configured_connection_mode(),
                "local_script": local_script_status,
                "system": status.get("system") if isinstance(status, dict) else {},
            })
        except ComfyUIError as exc:
            autostart = {"attempted": False}
            if isinstance(endpoint, dict) and endpoint.get("mode") == "local":
                start_result, start_msg = start_local_comfyui(actor, wait_seconds=6, data=data)
                autostart = {
                    "attempted": True,
                    "ok": bool(start_result and not start_msg),
                    "message": start_msg or (start_result or {}).get("message") or "",
                    "available": bool((start_result or {}).get("available")),
                    "start": start_result,
                }
                if start_result and (start_result.get("available") or start_result.get("already_running")):
                    try:
                        status = active_client.health_check(timeout=3) if hasattr(active_client, "health_check") else {"ok": True}
                        return json_resp({
                            "ok": True,
                            "available": True,
                            "comfyui_url": getattr(active_client, "base_url", url),
                            "endpoint": endpoint,
                            "connection_mode": endpoint.get("mode") if isinstance(endpoint, dict) else configured_connection_mode(),
                            "local_script": local_script_status,
                            "autostart": autostart,
                            "system": status.get("system") if isinstance(status, dict) else {},
                        })
                    except ComfyUIError as exc2:
                        exc = exc2
            dependency_install_hint = None
            if isinstance(endpoint, dict) and endpoint.get("mode") == "diffusers" and "Python 套件" in str(exc):
                dependency_install_hint = "請用 ./test_for_develop.sh 選 requirements-hf.txt / requirements-comfyui.txt 補齊 HF / Diffusers 依賴；或執行 python3 -m pip install -r scripts/comfyui/generation_probe_requirements.txt。"
            runtime = (
                local_comfyui_runtime_status((endpoint or {}).get("port"))
                if isinstance(endpoint, dict) and endpoint.get("mode") == "local"
                else None
            )
            audit(
                "COMFYUI_CONNECTION_TEST",
                get_client_ip(),
                user=actor_value(actor, "username"),
                success=False,
                ua=get_ua(),
                detail=f"url={url}, error={exc}",
            )
            return json_resp({
                "ok": True,
                "available": False,
                "starting": bool(runtime),
                "msg": runtime["message"] if runtime else str(exc),
                "comfyui_url": getattr(active_client, "base_url", url),
                "endpoint": endpoint,
                "connection_mode": endpoint.get("mode") if isinstance(endpoint, dict) else configured_connection_mode(),
                "local_script": local_script_status,
                "autostart": autostart,
                "local_runtime": runtime,
                "dependency_install_hint": dependency_install_hint,
            })

    @app.route("/api/root/comfyui/local-start-template", methods=["GET"])
    @require_csrf_safe
    def root_comfyui_local_start_template():
        actor, err = root_or_403()
        if err:
            return err
        if not local_start_template_path.is_file():
            return json_resp({"ok": False, "msg": "ComfyUI 啟動腳本範本不存在"}), 503
        audit(
            "COMFYUI_LOCAL_TEMPLATE_DOWNLOADED",
            get_client_ip(),
            user=actor_value(actor, "username"),
            success=True,
            ua=get_ua(),
            detail=f"filename={local_start_template_path.name}",
        )
        return send_file(
            local_start_template_path,
            as_attachment=True,
            download_name=local_start_template_path.name,
            mimetype="text/x-shellscript",
        )

    @app.route("/api/root/comfyui/civitai/inspect", methods=["POST"])
    @require_csrf
    def root_comfyui_civitai_inspect():
        actor, err = root_or_403()
        if err:
            return err
        try:
            data = ctx["request"].get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        page_url = str(data.get("page_url") or data.get("url") or "").strip()
        result, msg = inspect_civitai_model(page_url)
        audit(
            "COMFYUI_CIVITAI_INSPECT",
            get_client_ip(),
            user=actor_value(actor, "username"),
            success=not bool(msg),
            ua=get_ua(),
            detail=f"model_id={(result or {}).get('model_id') or ''}, url_host={urlparse(page_url).hostname if page_url else ''}, error={msg or ''}"[:300],
        )
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        return json_resp({"ok": True, "model": result, "msg": f"已讀取 {result['name']}，請選擇版本與檔案"})

    @app.route("/api/root/comfyui/civitai/search", methods=["POST"])
    @require_csrf
    def root_comfyui_civitai_search():
        actor, err = root_or_403()
        if err:
            return err
        try:
            data = ctx["request"].get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        query = str(data.get("query") or "").strip()
        base_model = str(data.get("base_model") or "").strip()
        model_type = str(data.get("model_type") or data.get("type") or "").strip()
        nsfw_mode = normalize_civitai_nsfw_mode(data.get("nsfw_mode") or data.get("safety") or "safe")
        try:
            limit = max(1, min(24, int(data.get("limit") or 12)))
        except Exception:
            limit = 12
        result, msg = search_civitai_models(
            query,
            base_model=base_model,
            model_type=model_type,
            nsfw_mode=nsfw_mode,
            limit=limit,
        )
        audit(
            "COMFYUI_CIVITAI_SEARCH",
            get_client_ip(),
            user=actor_value(actor, "username"),
            success=not bool(msg),
            ua=get_ua(),
            detail=(
                f"query={query[:80]}, type={normalize_civitai_search_type(model_type) or '-'}, "
                f"base_model={base_model[:40] or '-'}, nsfw={nsfw_mode}, "
                f"count={len((result or {}).get('results') or [])}, error={msg or ''}"
            )[:300],
        )
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        total = int(result.get("total_items") or 0)
        count = len(result.get("results") or [])
        message = "沒有符合條件的 Civitai 模型，請調整關鍵字或篩選器。" if count == 0 else f"已找到 {count} 個 Civitai 模型（總數約 {total}）。"
        if result.get("source_errors"):
            failed_sources = ", ".join(str(item.get("source_site") or "").strip() for item in result.get("source_errors") or [] if item.get("source_site"))
            if failed_sources:
                message = f"{message} 但部分來源暫時失敗：{failed_sources}。"
        return json_resp({
            "ok": True,
            "results": result.get("results") or [],
            "filters": result.get("filters") or {},
            "total_items": total,
            "current_page": result.get("current_page") or 1,
            "page_size": result.get("page_size") or count,
            "search_sources": result.get("search_sources") or [],
            "source_errors": result.get("source_errors") or [],
            "msg": message,
        })

    @app.route("/api/root/comfyui/civitai/media", methods=["GET"])
    @require_csrf_safe
    def root_comfyui_civitai_media():
        actor, err = root_or_403()
        if err:
            return err
        media_url = str(ctx["request"].args.get("url") or "").strip()
        data, content_type, msg = fetch_civitai_media(media_url)
        if msg:
            audit(
                "COMFYUI_CIVITAI_MEDIA_PROXY",
                get_client_ip(),
                user=actor_value(actor, "username"),
                success=False,
                ua=get_ua(),
                detail=f"url_host={urlparse(media_url).hostname if media_url else ''}, error={msg}"[:300],
            )
            return json_resp({"ok": False, "msg": msg}), 400
        response = Response(data, mimetype=content_type)
        response.headers["Cache-Control"] = "private, max-age=3600"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/root/comfyui/civitai/download", methods=["POST"])
    @require_csrf
    def root_comfyui_download_civitai_model():
        actor, err = root_or_403()
        if err:
            return err
        try:
            data = ctx["request"].get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        request_data, msg = parse_civitai_download_request(data)
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        if coerce_bool(data.get("async_progress")):
            job_id = create_model_download_job(actor)
            request_meta = capture_request_audit_meta()
            worker = threading.Thread(
                target=run_comfyui_model_download_job,
                args=(job_id, dict(actor), request_data, request_meta),
                daemon=True,
            )
            worker.start()
            return json_resp({
                "ok": True,
                "async": True,
                "job": {
                    "job_id": job_id,
                    "status": "queued",
                    "progress": {
                        "phase": "queued",
                        "percent": 0,
                        "detail": "已建立模型下載工作",
                    },
                },
            })
        result, msg = download_civitai_model_selection(
            page_url=request_data["page_url"],
            version_id=request_data["version_id"],
            file_id=request_data["file_id"],
            model_type=request_data["model_type"],
            base_dir=request_data["base_dir"],
            relative_dir=request_data["relative_dir"],
        )
        audit(
            "COMFYUI_CIVITAI_DOWNLOAD",
            get_client_ip(),
            user=actor_value(actor, "username"),
            success=not bool(msg),
            ua=get_ua(),
            detail=f"type={request_data['model_type']}, version_id={request_data['version_id']}, file_id={request_data['file_id'] or ''}, url_host={urlparse(request_data['page_url']).hostname if request_data['page_url'] else ''}, filename={(result or {}).get('filename') or ''}, error={msg or ''}"[:300],
        )
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        return json_resp({"ok": True, "download": result, "msg": f"已下載 {result['label']}：{result['filename']}"})

    @app.route("/api/root/comfyui/model-upload", methods=["POST"])
    @require_csrf
    def root_comfyui_upload_model_file():
        actor, err = root_or_403()
        if err:
            return err
        request_obj = ctx["request"]
        model_file = request_obj.files.get("model_file")
        model_type = str(request_obj.form.get("type") or request_obj.form.get("model_type") or "").strip().lower()
        base_dir = request_obj.form.get("base_dir")
        relative_dir = request_obj.form.get("relative_dir") or request_obj.form.get("model_relative_path") or ""
        result, msg = upload_comfyui_model_file(
            uploaded_file=model_file,
            model_type=model_type,
            base_dir=base_dir,
            relative_dir=relative_dir,
            actor=actor,
        )
        audit(
            "COMFYUI_MODEL_UPLOAD",
            get_client_ip(),
            user=actor_value(actor, "username"),
            success=not bool(msg),
            ua=get_ua(),
            detail=f"type={model_type}, filename={getattr(model_file, 'filename', '') if model_file else ''}, saved={(result or {}).get('filename') or ''}, error={msg or ''}"[:300],
        )
        if msg:
            return json_resp({"ok": False, "msg": msg}), 400
        return json_resp({"ok": True, "upload": result, "msg": f"已匯入 {result['label']}：{result['filename']}"})

    @app.route("/api/root/comfyui/download-jobs/<job_id>", methods=["GET"])
    @require_csrf_safe
    def root_comfyui_download_job_status(job_id):
        actor, err = root_or_403()
        if err:
            return err
        job, err = assert_model_download_job_owner(job_id, actor)
        if err:
            return err
        return json_resp({
            "ok": True,
            "job": {
                "job_id": job["job_id"],
                "status": job["status"],
                "progress": job.get("progress") or {},
                "error": job.get("error") or "",
                "result": job.get("result"),
            },
        })
