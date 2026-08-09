import json
import re

from flask import Response, request, send_file, stream_with_context

from services.media.previews import build_preview_metadata, preview_category
from services.core.http_headers import build_content_disposition
from services.core.file_offload import x_accel_response
from services.media.streaming import (
    get_stream_status,
    open_realtime_proxy_stream,
    parse_subtitle_shift_ms,
    realtime_proxy_availability,
    realtime_proxy_runtime_status,
    shift_webvtt_text,
)
from services.storage.cloud_drive import (
    can_download_file,
    get_cloud_drive_security_policy,
    is_e2ee_file,
    resolve_file_storage_path,
)
from services.storage.storage_albums import (
    create_share_link,
    ensure_storage_album_schema,
    list_share_links,
    mark_album_share_link_accessed,
    mark_share_link_accessed,
    public_album_payload,
    resolve_album_share_file,
    resolve_album_share_token,
    resolve_share_token,
    revoke_share_link,
)
from services.share_access_events import log_share_access_event
from services.security.upload_security import log_file_access
from services.users.friends import assert_can_target_user


def register_file_share_preview_routes(app, ctx):
    get_db = ctx["get_db"]
    get_current_user_ctx = ctx.get("get_current_user_ctx", lambda: None)
    get_client_ip = ctx["get_client_ip"]
    get_ua = ctx["get_ua"]
    audit = ctx["audit"]
    json_resp = ctx["json_resp"]
    require_csrf = ctx["require_csrf"]
    require_csrf_safe = ctx["require_csrf_safe"]
    storage_root = ctx["storage_root"]
    ffmpeg_bin = ctx.get("ffmpeg_bin", "ffmpeg")
    ffprobe_bin = ctx.get("ffprobe_bin", "ffprobe")

    actor_or_401 = ctx["actor_or_401"]
    read_readable_file_path = ctx["readable_file_path"]
    decryption_unavailable_preview = ctx["decryption_unavailable_preview"]
    requires_download_warning = ctx["requires_download_warning"]
    preview_allowed_by_policy = ctx["preview_allowed_by_policy"]
    preview_row_with_storage_fallback = ctx["preview_row_with_storage_fallback"]
    send_readable_file = ctx["send_readable_file"]
    svg_placeholder_response = ctx["svg_placeholder_response"]

    # 保留原 route block 的 helper 名稱，避免搬動時混入行為改寫。
    _actor_or_401 = actor_or_401
    _readable_file_path = read_readable_file_path
    _decryption_unavailable_preview = decryption_unavailable_preview
    _requires_download_warning = requires_download_warning
    _preview_allowed_by_policy = preview_allowed_by_policy
    _preview_row_with_storage_fallback = preview_row_with_storage_fallback
    _send_readable_file = send_readable_file
    _svg_placeholder_response = svg_placeholder_response

    def _send_local_storage_file(path, *, as_attachment=False, download_name="download.bin", mimetype=None, conditional=True):
        offloaded = x_accel_response(
            path,
            storage_root=storage_root,
            as_attachment=as_attachment,
            download_name=download_name,
            mimetype=mimetype or "application/octet-stream",
        )
        if offloaded is not None:
            return offloaded
        response = send_file(
            path,
            as_attachment=as_attachment,
            download_name=download_name,
            mimetype=mimetype,
            conditional=conditional,
        )
        response.headers.setdefault("X-Hackme-Transfer-Mode", "python_send_file")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/storage/share-links", methods=["GET", "POST"])
    @require_csrf_safe
    def storage_share_links():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            if request.method == "GET":
                links = list_share_links(conn, actor=actor, storage_file_id=request.args.get("storage_file_id"))
                return json_resp({"ok": True, "share_links": links})
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            forbidden_e2ee_secret_fields = {"fragment_key", "share_key", "raw_file_key", "decryption_key"}
            leaked_fields = sorted(forbidden_e2ee_secret_fields.intersection(set(data.keys())))
            if leaked_fields:
                return json_resp({
                    "ok": False,
                    "msg": "E2EE 分享金鑰不得送到伺服器；請只提交瀏覽器端包裝後的分享授權。",
                    "fields": leaked_fields,
                }), 400
            access_scope = data.get("access_scope") or "link"
            required_user_id = data.get("required_user_id") or None
            required_username = data.get("required_username") or data.get("required_account") or None
            if access_scope == "account":
                target_row = None
                if required_user_id not in (None, ""):
                    try:
                        required_user_id = int(required_user_id)
                    except Exception:
                        return json_resp({"ok": False, "msg": "指定帳戶格式錯誤"}), 400
                    target_row = conn.execute("SELECT id, username FROM users WHERE id=? LIMIT 1", (required_user_id,)).fetchone()
                elif str(required_username or "").strip():
                    target_row = conn.execute(
                        "SELECT id, username FROM users WHERE username=? LIMIT 1",
                        (str(required_username or "").strip(),),
                    ).fetchone()
                if not target_row:
                    return json_resp({"ok": False, "msg": "找不到指定帳戶"}), 400
                allowed, deny_msg = assert_can_target_user(conn, actor, target_row["id"], context="cloud_drive_share")
                if not allowed:
                    return json_resp({"ok": False, "msg": deny_msg}), 403
                required_user_id = int(target_row["id"])
                required_username = None
            link, msg = create_share_link(
                conn,
                actor=actor,
                storage_file_id=data.get("storage_file_id"),
                file_id=data.get("file_id"),
                expires_at=data.get("expires_at") or None,
                can_preview=bool(data.get("can_preview", True)),
                access_scope=access_scope,
                required_user_id=required_user_id,
                required_username=required_username,
                max_views=data.get("max_views") or 0,
                wrapped_file_key_envelope=data.get("wrapped_file_key_envelope") or None,
                share_password=data.get("share_password") or None,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("STORAGE_SHARE_LINK_CREATE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"share_link_id={link['id']}")
            return json_resp({"ok": True, "share_link": link})
        finally:
            conn.close()

    @app.route("/api/storage/share-links/<link_id>/revoke", methods=["POST"])
    @require_csrf
    def storage_share_link_revoke(link_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            link, msg = revoke_share_link(conn, actor=actor, link_id=link_id)
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 404
            conn.commit()
            audit("STORAGE_SHARE_LINK_REVOKE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"share_link_id={link_id}")
            return json_resp({"ok": True, "share_link": link})
        finally:
            conn.close()

    def _storage_share_error_response(reason):
        if reason == "login_required":
            return json_resp({"ok": False, "msg": "此分享連結限定指定帳戶下載，請先登入。", "reason": reason}), 401
        if reason == "forbidden":
            return json_resp({"ok": False, "msg": "此分享連結不開放目前帳戶下載。", "reason": reason}), 403
        if reason == "password_required":
            return json_resp({"ok": False, "msg": "此分享連結需要密碼。", "reason": reason, "password_required": True}), 401
        if reason == "password_invalid":
            return json_resp({"ok": False, "msg": "分享密碼不正確。", "reason": reason, "password_required": True}), 403
        if reason == "view_limit_reached":
            return json_resp({"ok": False, "msg": "分享下載次數已用完。", "reason": reason}), 410
        if reason == "e2ee_share_authorization_missing":
            return json_resp({"ok": False, "msg": "E2EE 分享授權不完整，請重新產生分享連結。", "reason": reason}), 409
        return json_resp({"ok": False, "msg": "分享連結不存在或已失效", "reason": reason}), 404

    def _storage_share_stream_status(conn, row, token=""):
        file_id = str(row["file_id"] or "")
        if not file_id:
            return None
        try:
            file_row = conn.execute(
                "SELECT * FROM uploaded_files WHERE id=? AND deleted_at IS NULL",
                (file_id,),
            ).fetchone()
        except Exception:
            file_row = None
        if not file_row:
            return None
        try:
            asset = get_stream_status(conn, file_row=file_row, include_segments=False)
        except Exception:
            return None
        if not asset:
            return None
        proxy_state = realtime_proxy_availability(file_row)
        job = None
        try:
            job = conn.execute(
                """
                SELECT status, progress_percent, stage, stage_detail, error_message, updated_at
                FROM job_center_jobs
                WHERE source_module='media_hls_prepare' AND source_ref=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (f"media_stream:{file_id}",),
            ).fetchone()
        except Exception:
            job = None
        data = {
            "status": asset.get("status") or "",
            "media_type": asset.get("media_type") or "",
            "master_manifest_ready": bool(asset.get("master_manifest_path")),
            "master_url": f"/api/storage/shared/{token}/hls/master.m3u8" if asset.get("master_manifest_path") and token else "",
            "realtime_proxy_url": f"/api/storage/shared/{token}/realtime-proxy" if token and proxy_state.get("available") else "",
            "realtime_proxy": {
                **proxy_state,
                "url": f"/api/storage/shared/{token}/realtime-proxy" if token and proxy_state.get("available") else "",
                "selected_audio_query_param": "audio",
                "start_seconds_query_param": "start",
            },
            "error_message": asset.get("error_message") or "",
            "updated_at": asset.get("updated_at") or "",
            "premium_hls_profile_policy": asset.get("premium_hls_profile_policy") or {},
            "audio_tracks": [
                {
                    "name": str(item.get("name") or ""),
                    "label": str(item.get("label") or item.get("language") or item.get("name") or "音軌"),
                    "language": str(item.get("language") or "und"),
                    "is_default": bool(item.get("is_default")),
                    "codec": str(item.get("codec") or ""),
                    "stream_index": int(item.get("stream_index") or -1),
                    "url": f"/api/storage/shared/{token}/hls/{item.get('name')}/playlist.m3u8" if token and item.get("name") else "",
                    "playlist_url": f"/api/storage/shared/{token}/hls/{item.get('name')}/playlist.m3u8" if token and item.get("name") else "",
                }
                for item in (asset.get("audio_tracks") or [])
                if item.get("name")
            ],
            "subtitles": [
                {
                    "name": str(item.get("name") or ""),
                    "label": str(item.get("label") or item.get("language") or "字幕"),
                    "language": str(item.get("language") or "und"),
                    "is_default": bool(item.get("is_default")),
                    "is_forced": bool(item.get("is_forced")),
                    "url": f"/api/storage/shared/{token}/hls/subtitles/{item.get('name')}.vtt" if token and item.get("name") else "",
                }
                for item in (asset.get("subtitles") or [])
                if item.get("name")
            ],
        }
        if job:
            data.update({
                "job_status": job["status"] or "",
                "progress_percent": int(job["progress_percent"] or 0),
                "stage": job["stage"] or "",
                "stage_detail": job["stage_detail"] or "",
                "job_error_message": job["error_message"] or "",
                "job_updated_at": job["updated_at"] or "",
            })
        return data

    def _hls_url_with_request_query(url):
        raw = str(url or "")
        if not raw:
            return raw
        query = request.query_string.decode("utf-8", errors="ignore")
        if not query or "password=" in raw:
            return raw
        separator = "&" if "?" in raw else "?"
        return f"{raw}{separator}{query}"

    def _append_request_query_to_hls_manifest(text):
        uri_attr_pattern = re.compile(r"URI=([\"'])([^\"']+)\1")

        def replace_uri_attr(match):
            quote = match.group(1)
            url = match.group(2)
            return f"URI={quote}{_hls_url_with_request_query(url)}{quote}"

        output = []
        for raw in str(text or "").splitlines():
            line = raw
            if line.startswith("#") and "URI=" in line:
                line = uri_attr_pattern.sub(replace_uri_attr, line)
            elif line and not line.startswith("#"):
                line = _hls_url_with_request_query(line)
            output.append(line)
        return "\n".join(output) + "\n"

    def _storage_share_hls_asset(conn, token, *, include_segments=False):
        actor = get_current_user_ctx()
        row, reason = resolve_share_token(
            conn,
            token,
            actor=actor,
            require_download=False,
            password=_storage_share_password_from_request(),
        )
        if not row:
            return None, None, _storage_share_error_response(reason)
        denied = _storage_share_preview_denied(row)
        if denied:
            return row, None, denied
        if is_e2ee_file(row):
            return row, None, (json_resp({"ok": False, "msg": "E2EE 分享不支援伺服器端 HLS 預覽"}), 409)
        policy = get_cloud_drive_security_policy(conn)
        ok, msg = _preview_allowed_by_policy(policy, row)
        if not ok:
            return row, None, (json_resp({"ok": False, "msg": msg}), 403)
        asset = get_stream_status(conn, file_row=_storage_share_preview_row(row), include_segments=include_segments)
        if not asset or asset.get("status") != "ready" or not asset.get("master_manifest_path"):
            return row, None, (json_resp({"ok": False, "msg": "HLS 串流尚未準備完成", "error": "stream_not_ready"}), 409)
        return row, asset, None

    def _storage_share_hls_rendition_match(asset, name):
        clean = str(name or "").strip()
        if not clean or "/" in clean or ".." in clean:
            return None
        for group in ("variants", "audio_tracks"):
            match = next((item for item in ((asset or {}).get(group) or []) if item.get("name") == clean), None)
            if match:
                return match
        return None

    def _storage_share_realtime_proxy_audio_selector():
        return request.args.get("audio") or request.args.get("audio_track") or request.args.get("track") or ""

    def _storage_share_realtime_proxy_error(message, *, error="realtime_proxy_unavailable", status=409, extra=None):
        payload = {
            "ok": False,
            "msg": message,
            "error": error,
            "realtime_proxy": realtime_proxy_runtime_status(),
        }
        if extra:
            payload.update(extra)
        return json_resp(payload), status

    def _storage_share_realtime_proxy_response(row):
        availability = realtime_proxy_availability(row)
        if not availability.get("available"):
            return _storage_share_realtime_proxy_error(
                "即時轉封裝目前不可用，請改用直接串流或預處理 HLS。",
                error=str(availability.get("reason") or "realtime_proxy_unavailable"),
                status=409,
                extra={"availability": availability},
            )
        path = resolve_file_storage_path(storage_root, row)
        if not path.exists():
            return json_resp({"ok": False, "msg": "實體檔案不存在", "error": "file_missing"}), 404
        try:
            stream_info = open_realtime_proxy_stream(
                path,
                audio_track=_storage_share_realtime_proxy_audio_selector(),
                start_seconds=request.args.get("start") or 0,
                ffmpeg_bin=ffmpeg_bin,
                ffprobe_bin=ffprobe_bin,
            )
        except RuntimeError as exc:
            if str(exc).startswith("realtime_proxy_busy:"):
                return _storage_share_realtime_proxy_error(
                    "即時轉封裝目前已達併發上限，請稍後再試或使用預處理 HLS。",
                    error="realtime_proxy_busy",
                    status=429,
                )
            return _storage_share_realtime_proxy_error(str(exc), status=500)
        except ValueError as exc:
            return _storage_share_realtime_proxy_error(str(exc), error="invalid_realtime_proxy_request", status=400)
        except FileNotFoundError:
            return _storage_share_realtime_proxy_error("找不到 ffmpeg / ffprobe，無法啟動即時轉封裝。", error="ffmpeg_missing", status=503)
        filename = row["display_name"] or row["original_filename_plain_for_public"] or "video.mp4"
        response = Response(
            stream_with_context(stream_info["chunks"]),
            status=200,
            mimetype=stream_info.get("mimetype") or "video/mp4",
            direct_passthrough=True,
        )
        response.headers["Content-Disposition"] = build_content_disposition("inline", filename)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        response.headers["Accept-Ranges"] = "none"
        response.headers["X-Hackme-Transfer-Mode"] = "python_realtime_proxy"
        selected_audio = stream_info.get("audio_track") or {}
        if selected_audio.get("name"):
            response.headers["X-Hackme-Audio-Track"] = str(selected_audio.get("name") or "")
        response.headers["X-Hackme-Streaming-Mode"] = "realtime_proxy"
        return response

    def _storage_shared_file_payload(conn, row, token):
        e2ee = {}
        if is_e2ee_file(row):
            e2ee = {
                "wrapped_file_key_envelope": row["wrapped_file_key_envelope"] or "",
                "nonce": row["nonce"] or "",
                "encrypted_metadata": row["encrypted_metadata"] or "",
                "requires_fragment_key": bool(row["wrapped_file_key_envelope"]),
            }
        return {
            "id": row["id"],
            "file_id": row["file_id"],
            "display_name": row["display_name"] or row["original_filename_plain_for_public"] or "download.bin",
            "size_bytes": int(row["size_bytes"] or 0),
            "mime_type": row["mime_type_plain_for_public"] or "",
            "privacy_mode": row["privacy_mode"],
            "access_scope": row["access_scope"] or "link",
            "required_user_id": int(row["required_user_id"] or 0),
            "required_username": row["required_username"] or "",
            "expires_at": row["expires_at"] or "",
            "max_views": int(row["max_views"] or 0),
            "access_count": int(row["access_count"] or 0),
            "can_preview": bool(row["can_preview"]),
            "password_required": bool(int(row["password_required"] or 0)),
            "download_url": f"/api/storage/shared/{token}/download",
            "preview_url": f"/api/storage/shared/{token}/preview",
            "preview_content_url": f"/api/storage/shared/{token}/preview/content",
            "stream_asset": _storage_share_stream_status(conn, row, token),
            "e2ee": e2ee,
        }

    def _storage_share_preview_row(row):
        preview_row = dict(row)
        preview_row["id"] = row["file_id"]
        if not preview_row.get("original_filename_plain_for_public"):
            preview_row["original_filename_plain_for_public"] = row["display_name"] or "preview"
        return preview_row

    def _storage_share_preview_denied(row):
        data = dict(row)
        if not int(data.get("can_preview") or 0):
            return json_resp({"ok": False, "msg": "此分享連結未開放瀏覽器預覽"}), 403
        return None

    def _storage_share_password_from_request():
        header_value = request.headers.get("X-Share-Password")
        if header_value is not None:
            return header_value
        query_value = request.args.get("password")
        if query_value is not None:
            return query_value
        if request.method in {"POST", "PUT", "PATCH"}:
            data = request.get_json(silent=True) if request.is_json else None
            if isinstance(data, dict) and "password" in data:
                return data.get("password") or ""
            if "password" in request.form:
                return request.form.get("password") or ""
        return None

    def _html_safe_json(value):
        return (
            json.dumps(str(value or ""))
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )

    @app.route("/shared/files/<token>", methods=["GET"])
    def storage_share_file_page(token):
        safe_token = _html_safe_json(token)
        return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>檔案分享</title>
  <style>
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #172033; }}
    main {{ max-width: 720px; margin: 0 auto; padding: 40px 20px; }}
    .panel {{ background: #fff; border: 1px solid #dde3ea; border-radius: 8px; padding: 22px; }}
    h1 {{ overflow-wrap: anywhere; word-break: break-word; }}
    .meta {{ color: #667085; margin: 8px 0 18px; overflow-wrap: anywhere; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    button, .button-link {{ display: inline-flex; align-items: center; justify-content: center; min-height: 38px; padding: 10px 14px; border: 0; border-radius: 6px; background: #2357d9; color: #fff; cursor: pointer; text-decoration: none; box-sizing: border-box; }}
    button[disabled] {{ opacity: .55; cursor: not-allowed; }}
    .login-link {{ background: #0f766e; }}
    .password-form {{ display: grid; gap: 8px; max-width: 360px; margin-top: 14px; padding: 12px; border: 1px solid #e4e7ec; border-radius: 8px; background: #fbfcfe; }}
    .password-form label {{ font-weight: 700; color: #344054; }}
    .password-form input {{ width: 100%; min-height: 38px; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box; }}
    [hidden] {{ display: none !important; }}
    .preview {{ margin-top: 18px; border: 1px solid #e4e7ec; border-radius: 8px; background: #fbfcfe; overflow: hidden; }}
    .preview pre {{ margin: 0; padding: 14px; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 60vh; overflow: auto; }}
    .preview img, .preview video, .preview audio, .preview iframe {{ display: block; width: 100%; border: 0; }}
    .preview img {{ height: auto; max-height: 70vh; object-fit: contain; background: #111827; }}
    .preview video, .preview iframe {{ min-height: 360px; background: #111827; }}
    .preview-list {{ margin: 0; padding: 12px 18px 12px 34px; max-height: 55vh; overflow: auto; }}
    .shared-file-progress {{ display: grid; gap: 8px; padding: 14px; }}
    .shared-file-progress progress {{ width: 100%; height: 12px; }}
    .shared-file-progress span, .shared-file-progress small {{ color: #667085; }}
    .shared-file-subtitle-shift-row {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
    .shared-file-subtitle-shift-row input[type=number] {{ width:96px; min-height:38px; padding:8px 10px; border:1px solid #cbd5e1; border-radius:6px; box-sizing:border-box; }}
    .msg {{ margin-top: 14px; color: #667085; }}
    .err {{ color: #b42318; }}
  </style>
</head>
<body>
  <main>
    <section class="panel">
      <h1 id="shared-file-title">檔案分享</h1>
      <div class="meta" id="shared-file-meta">讀取中...</div>
      <div class="actions">
        <button id="shared-file-preview-btn" type="button" disabled>在瀏覽器預覽</button>
        <button id="shared-file-download-btn" type="button" disabled>下載檔案</button>
        <a id="shared-file-login-link" class="button-link login-link" href="/" hidden>前往登入</a>
      </div>
      <div class="preview" id="shared-file-preview" hidden></div>
      <div class="msg" id="shared-file-msg"></div>
    </section>
  </main>
  <script id="shared-file-token" type="application/json">{safe_token}</script>
  <script src="/js/shared-file.js"></script>
</body>
</html>"""

    @app.route("/api/storage/shared/<token>", methods=["GET"])
    def storage_share_file_api(token):
        actor = get_current_user_ctx()
        conn = get_db()
        try:
            row, reason = resolve_share_token(
                conn,
                token,
                actor=actor,
                require_download=False,
                password=_storage_share_password_from_request(),
            )
            if not row:
                return _storage_share_error_response(reason)
            return json_resp({"ok": True, "file": _storage_shared_file_payload(conn, row, token)})
        finally:
            conn.close()

    @app.route("/api/storage/shared/<token>/preview", methods=["GET"])
    def storage_share_link_preview(token):
        actor = get_current_user_ctx()
        conn = get_db()
        try:
            row, reason = resolve_share_token(
                conn,
                token,
                actor=actor,
                require_download=False,
                password=_storage_share_password_from_request(),
            )
            if not row:
                return _storage_share_error_response(reason)
            denied = _storage_share_preview_denied(row)
            if denied:
                return denied
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            preview_row = _storage_share_preview_row(row)
            if is_e2ee_file(row):
                preview = _decryption_unavailable_preview(preview_row, "E2EE 檔案須由瀏覽器端解密後預覽")
                preview["browser_decrypt_required"] = True
                return json_resp({"ok": True, "preview": preview})
            policy = get_cloud_drive_security_policy(conn)
            ok, msg = _preview_allowed_by_policy(policy, row)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), 403
            try:
                category, _mime_type = preview_category(preview_row)
                if category in {"text", "archive"}:
                    readable_path, _ = _readable_file_path(row)
                else:
                    readable_path = path
                preview = build_preview_metadata(preview_row, readable_path)
            except ValueError as exc:
                preview = _decryption_unavailable_preview(preview_row, str(exc))
            log_file_access(conn, file_id=row["file_id"], actor_user_id=(actor["id"] if actor else None), action="storage_share_preview", result="allowed", reason=preview["category"], ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            return json_resp({"ok": True, "preview": preview})
        finally:
            conn.close()

    @app.route("/api/storage/shared/<token>/preview/content", methods=["GET"])
    def storage_share_link_preview_content(token):
        actor = get_current_user_ctx()
        conn = get_db()
        try:
            row, reason = resolve_share_token(
                conn,
                token,
                actor=actor,
                require_download=False,
                password=_storage_share_password_from_request(),
            )
            if not row:
                return _storage_share_error_response(reason)
            denied = _storage_share_preview_denied(row)
            if denied:
                return denied
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            preview_row = _storage_share_preview_row(row)
            if is_e2ee_file(row):
                mark_share_link_accessed(conn, row["id"])
                log_share_access_event(conn, share_type="file", share_id=row["id"], ip=get_client_ip(), user_agent=get_ua())
                log_file_access(conn, file_id=row["file_id"], actor_user_id=(actor["id"] if actor else None), action="storage_share_e2ee_preview_ciphertext", result="allowed", reason="share_link", ip=get_client_ip(), user_agent=get_ua())
                conn.commit()
                return _send_local_storage_file(
                    path,
                    as_attachment=False,
                    download_name=preview_row["original_filename_plain_for_public"] or "e2ee.bin",
                    mimetype="application/octet-stream",
                )
            policy = get_cloud_drive_security_policy(conn)
            ok, msg = _preview_allowed_by_policy(policy, row)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), 403
            category, mime_type = preview_category(preview_row)
            if category not in {"audio", "video", "image", "pdf"}:
                return json_resp({"ok": False, "msg": "此檔案類型不支援 inline content preview"}), 415
            mark_share_link_accessed(conn, row["id"])
            log_share_access_event(conn, share_type="file", share_id=row["id"], ip=get_client_ip(), user_agent=get_ua())
            log_file_access(conn, file_id=row["file_id"], actor_user_id=(actor["id"] if actor else None), action="storage_share_preview_content", result="allowed", reason=category, ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            response = _send_readable_file(
                row,
                as_attachment=False,
                download_name=preview_row["original_filename_plain_for_public"] or "preview",
                mimetype=mime_type,
                conditional=True,
                actor=actor,
            )
            if response is None:
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            return response
        finally:
            conn.close()

    @app.route("/api/storage/shared/<token>/hls/master.m3u8", methods=["GET"])
    def storage_share_hls_master(token):
        conn = get_db()
        try:
            row, asset, err = _storage_share_hls_asset(conn, token, include_segments=False)
            if err:
                return err
            mark_share_link_accessed(conn, row["id"])
            log_share_access_event(conn, share_type="file", share_id=row["id"], ip=get_client_ip(), user_agent=get_ua())
            log_file_access(conn, file_id=row["file_id"], actor_user_id=None, action="storage_share_hls_preview", result="allowed", reason="hls_master", ip=get_client_ip(), user_agent=get_ua())
            path = resolve_file_storage_path(storage_root, {"storage_path": asset["master_manifest_path"]})
            conn.commit()
            text = _append_request_query_to_hls_manifest(path.read_text(encoding="utf-8"))
            return Response(text, status=200, mimetype="application/vnd.apple.mpegurl")
        finally:
            conn.close()

    @app.route("/api/storage/shared/<token>/hls/<variant>/playlist.m3u8", methods=["GET"])
    def storage_share_hls_playlist(token, variant):
        conn = get_db()
        try:
            _row, asset, err = _storage_share_hls_asset(conn, token, include_segments=False)
            if err:
                return err
            match = _storage_share_hls_rendition_match(asset, variant)
            if not match:
                return json_resp({"ok": False, "msg": "找不到串流變體", "error": "variant_not_found"}), 404
            path = resolve_file_storage_path(storage_root, {"storage_path": match["playlist_path"]})
            text = _append_request_query_to_hls_manifest(path.read_text(encoding="utf-8"))
            return Response(text, status=200, mimetype="application/vnd.apple.mpegurl")
        finally:
            conn.close()

    @app.route("/api/storage/shared/<token>/hls/subtitles/<subtitle_name>.vtt", methods=["GET"])
    def storage_share_hls_subtitle(token, subtitle_name):
        conn = get_db()
        try:
            _row, asset, err = _storage_share_hls_asset(conn, token, include_segments=False)
            if err:
                return err
            clean = str(subtitle_name or "").strip()
            if not clean or "/" in clean or ".." in clean:
                return json_resp({"ok": False, "msg": "找不到字幕", "error": "subtitle_not_found"}), 404
            match = next((item for item in (asset.get("subtitles") or []) if item.get("name") == clean), None)
            if not match:
                return json_resp({"ok": False, "msg": "找不到字幕", "error": "subtitle_not_found"}), 404
            path = resolve_file_storage_path(storage_root, {"storage_path": match["path"]})
            shift_ms = parse_subtitle_shift_ms(request.args.get("shift_ms"))
            if shift_ms:
                text = shift_webvtt_text(path.read_text(encoding="utf-8", errors="replace"), shift_ms)
                return Response(
                    text,
                    status=200,
                    mimetype="text/vtt; charset=utf-8",
                    headers={"Cache-Control": "no-store"},
                )
            return _send_local_storage_file(path, as_attachment=False, download_name=f"{clean}.vtt", mimetype="text/vtt; charset=utf-8")
        finally:
            conn.close()

    @app.route("/api/storage/shared/<token>/hls/<variant>/<segment>", methods=["GET"])
    def storage_share_hls_segment(token, variant, segment):
        conn = get_db()
        try:
            _row, asset, err = _storage_share_hls_asset(conn, token, include_segments=True)
            if err:
                return err
            if "/" in segment or ".." in segment:
                return json_resp({"ok": False, "msg": "無效的串流片段", "error": "invalid_segment"}), 400
            match = _storage_share_hls_rendition_match(asset, variant)
            if not match:
                return json_resp({"ok": False, "msg": "找不到串流變體", "error": "variant_not_found"}), 404
            rel = next((item["path"] for item in (match.get("segments") or []) if item.get("filename") == segment), "")
            if not rel:
                if segment == "init.mp4" and match.get("init_segment_path"):
                    rel = match["init_segment_path"]
                else:
                    return json_resp({"ok": False, "msg": "找不到串流片段", "error": "segment_not_found"}), 404
            path = resolve_file_storage_path(storage_root, {"storage_path": rel})
            mimetype = "video/mp4" if segment.endswith((".mp4", ".m4s")) else "application/octet-stream"
            return _send_local_storage_file(path, as_attachment=False, download_name=segment, mimetype=mimetype)
        finally:
            conn.close()

    @app.route("/api/storage/shared/<token>/realtime-proxy", methods=["GET"])
    def storage_share_realtime_proxy(token):
        actor = get_current_user_ctx()
        conn = get_db()
        try:
            row, reason = resolve_share_token(
                conn,
                token,
                actor=actor,
                require_download=False,
                password=_storage_share_password_from_request(),
            )
            if not row:
                return _storage_share_error_response(reason)
            denied = _storage_share_preview_denied(row)
            if denied:
                return denied
            if is_e2ee_file(row):
                return json_resp({"ok": False, "msg": "E2EE 分享不支援伺服器端即時轉封裝"}), 409
            policy = get_cloud_drive_security_policy(conn)
            ok, msg = _preview_allowed_by_policy(policy, row)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), 403
            mark_share_link_accessed(conn, row["id"])
            log_share_access_event(conn, share_type="file", share_id=row["id"], ip=get_client_ip(), user_agent=get_ua())
            log_file_access(conn, file_id=row["file_id"], actor_user_id=(actor["id"] if actor else None), action="storage_share_realtime_proxy", result="allowed", reason="realtime_proxy", ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            return _storage_share_realtime_proxy_response(row)
        finally:
            conn.close()

    @app.route("/api/storage/shared/<token>/download", methods=["GET"])
    def storage_share_link_download(token):
        actor = get_current_user_ctx()
        conn = get_db()
        try:
            row, reason = resolve_share_token(
                conn,
                token,
                actor=actor,
                password=_storage_share_password_from_request(),
            )
            if not row:
                return _storage_share_error_response(reason)
            policy = get_cloud_drive_security_policy(conn)
            if policy.get("block_unclean_downloads") and not is_e2ee_file(row) and row["scan_status"] not in {"clean", "not_required"}:
                return json_resp({"ok": False, "msg": "檔案尚未通過安全檢查"}), 403
            if _requires_download_warning(policy, row):
                confirmed = (
                    request.args.get("confirm_high_risk") == "1"
                    or request.headers.get("X-Confirm-High-Risk-Download", "").lower() in {"1", "true", "yes"}
                )
                if not confirmed:
                    return json_resp({
                        "ok": False,
                        "requires_confirmation": True,
                        "msg": "此分享檔案為高風險或無法完整掃描，請確認信任來源後再下載。",
                        "risk_level": row["risk_level"],
                        "scan_status": row["scan_status"],
                    }), 409
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            mark_share_link_accessed(conn, row["id"])
            log_share_access_event(conn, share_type="file", share_id=row["id"], ip=get_client_ip(), user_agent=get_ua())
            log_file_access(conn, file_id=row["file_id"], actor_user_id=(actor["id"] if actor else None), action="storage_share_download", result="allowed", reason="share_link", ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            response = _send_readable_file(row, as_attachment=True, download_name=row["display_name"] or row["original_filename_plain_for_public"] or "download.bin", actor=actor)
            if response is None:
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            return response
        finally:
            conn.close()

    def _html_safe_json(value):
        return (
            json.dumps(str(value or ""))
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )

    @app.route("/shared/albums/<token>", methods=["GET"])
    def storage_album_share_page(token):
        safe_token = _html_safe_json(token)
        return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>分享相簿</title>
  <style>
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #172033; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 32px 20px; }}
    .meta {{ color: #667085; margin: 8px 0 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 16px; }}
    .tile {{ background: #fff; border: 1px solid #dde3ea; border-radius: 8px; overflow: hidden; }}
    .thumb {{ aspect-ratio: 1 / 1; display: grid; place-items: center; background: #edf1f5; color: #667085; }}
    .thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .name {{ padding: 10px 12px; overflow-wrap: anywhere; font-size: 14px; }}
    .empty {{ padding: 24px; background: #fff; border: 1px solid #dde3ea; border-radius: 8px; }}
    .password-panel {{ display: none; margin: 16px 0 24px; padding: 16px; background: #fff; border: 1px solid #dde3ea; border-radius: 8px; }}
    .password-panel.show {{ display: block; }}
    .password-row {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .password-row input {{ min-width: 220px; flex: 1; padding: 10px 12px; border: 1px solid #c7d0dc; border-radius: 6px; }}
    .password-row button {{ padding: 10px 14px; border: 0; border-radius: 6px; background: #2357d9; color: #fff; cursor: pointer; }}
  </style>
</head>
<body>
  <main>
    <h1 id="album-title">分享相簿</h1>
    <div class="meta" id="album-meta">讀取中...</div>
    <form class="password-panel" id="album-password-panel">
      <label for="album-password-input">這本相簿需要分享密碼</label>
      <div class="password-row">
        <input type="password" id="album-password-input" autocomplete="current-password" placeholder="輸入分享密碼">
        <button type="submit">開啟相簿</button>
      </div>
    </form>
    <div class="grid" id="album-files"></div>
  </main>
  <script>
  const SHARE_KEY = {safe_token};
  const titleEl = document.getElementById("album-title");
  const metaEl = document.getElementById("album-meta");
  const filesEl = document.getElementById("album-files");
  const passwordPanel = document.getElementById("album-password-panel");
  const passwordInput = document.getElementById("album-password-input");
  let sharePassword = "";
  function fileKind(file) {{
    const mime = String(file.mime_type || "").toLowerCase();
    if (mime.startsWith("image/")) return "image";
    if (mime.startsWith("video/")) return "video";
    return "file";
  }}
  function esc(value) {{
    return String(value || "").replace(/[&<>"']/g, (ch) => ({{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }}[ch]));
  }}
  function fileUrl(file, inline) {{
    const raw = file.download_url || "#";
    try {{
      const url = new URL(raw, window.location.origin);
      if (sharePassword) url.searchParams.set("password", sharePassword);
      if (inline) url.searchParams.set("inline", "1");
      return url.pathname + url.search;
    }} catch (err) {{
      return raw;
    }}
  }}
  function loadAlbum() {{
    const headers = sharePassword ? {{ "X-Album-Share-Password": sharePassword }} : {{}};
    fetch(`/api/storage/shared/albums/${{encodeURIComponent(SHARE_KEY)}}`, {{ headers }})
    .then((res) => res.json().then((body) => ({{ status: res.status, body }})))
    .then((result) => {{
      if (!result.body.ok) {{
        if (result.body.reason === "password_required" || result.body.reason === "password_invalid") {{
          passwordPanel.classList.add("show");
          metaEl.textContent = result.body.reason === "password_invalid" ? "密碼不正確，請重新輸入。" : "請輸入分享密碼。";
          filesEl.innerHTML = "";
          passwordInput.focus();
          return;
        }}
        throw new Error(result.body.msg || "分享相簿不存在或已失效");
      }}
      passwordPanel.classList.remove("show");
      const album = result.body.album || {{}};
      titleEl.textContent = album.title || "分享相簿";
      metaEl.textContent = `${{(album.files || []).length}} 個檔案${{album.description ? " · " + album.description : ""}}`;
      if (!album.files || !album.files.length) {{
        filesEl.innerHTML = '<div class="empty">這本相簿目前沒有可顯示的檔案</div>';
        return;
      }}
      filesEl.innerHTML = album.files.map((file) => {{
        const kind = fileKind(file);
        const href = fileUrl(file, false);
        const inlineHref = fileUrl(file, true);
        const safeHref = esc(href);
        const thumb = kind === "image"
          ? `<a class="thumb" href="${{safeHref}}" target="_blank" rel="noreferrer"><img src="${{esc(inlineHref)}}" alt=""></a>`
          : `<a class="thumb" href="${{safeHref}}" target="_blank" rel="noreferrer">${{esc(kind)}}</a>`;
        return `<article class="tile">${{thumb}}<div class="name">${{esc(file.display_name || file.file_id || "file")}}</div></article>`;
      }}).join("");
    }})
    .catch((err) => {{
      titleEl.textContent = "分享相簿無法開啟";
      metaEl.textContent = err.message || "分享相簿不存在或已失效";
      filesEl.innerHTML = "";
    }});
  }}
  passwordPanel.addEventListener("submit", (event) => {{
    event.preventDefault();
    sharePassword = passwordInput.value || "";
    loadAlbum();
  }});
  loadAlbum();
  </script>
</body>
</html>"""

    def _album_share_password_from_request():
        return request.headers.get("X-Album-Share-Password") or request.args.get("password") or ""

    def _album_share_error_response(reason):
        if reason == "password_required":
            return json_resp({
                "ok": False,
                "msg": "這本相簿需要分享密碼",
                "reason": reason,
                "password_required": True,
            }), 401
        if reason == "password_invalid":
            return json_resp({
                "ok": False,
                "msg": "分享密碼不正確",
                "reason": reason,
                "password_required": True,
            }), 403
        if reason == "expired":
            return json_resp({"ok": False, "msg": "分享相簿已到期", "reason": reason}), 410
        if reason == "view_limit_reached":
            return json_resp({"ok": False, "msg": "分享相簿存取次數已用完", "reason": reason}), 410
        return json_resp({"ok": False, "msg": "分享相簿不存在或已失效", "reason": reason}), 404

    @app.route("/api/storage/shared/albums/<token>", methods=["GET"])
    def storage_album_share_api(token):
        conn = get_db()
        try:
            row, reason = resolve_album_share_token(conn, token, _album_share_password_from_request())
            if not row:
                return _album_share_error_response(reason)
            album = public_album_payload(conn, row)
            mark_album_share_link_accessed(conn, row["id"])
            log_share_access_event(conn, share_type="album", share_id=row["id"], ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            return json_resp({"ok": True, "album": album})
        finally:
            conn.close()

    @app.route("/api/storage/shared/albums/<token>/files/<file_id>/download", methods=["GET"])
    def storage_album_share_file_download(token, file_id):
        conn = get_db()
        try:
            resolved, reason = resolve_album_share_file(conn, token, file_id, _album_share_password_from_request())
            if not resolved:
                if reason in {"password_required", "password_invalid", "expired", "view_limit_reached"}:
                    return _album_share_error_response(reason)
                return json_resp({"ok": False, "msg": "分享檔案不存在或已失效", "reason": reason}), 404
            share = resolved["share"]
            row = resolved["file"]
            policy = get_cloud_drive_security_policy(conn)
            if policy.get("block_unclean_downloads") and not is_e2ee_file(row) and row["scan_status"] not in {"clean", "not_required"}:
                return json_resp({"ok": False, "msg": "檔案尚未通過安全檢查"}), 403
            if _requires_download_warning(policy, row):
                confirmed = (
                    request.args.get("confirm_high_risk") == "1"
                    or request.headers.get("X-Confirm-High-Risk-Download", "").lower() in {"1", "true", "yes"}
                )
                if not confirmed:
                    return json_resp({
                        "ok": False,
                        "requires_confirmation": True,
                        "msg": "此分享檔案為高風險或無法完整掃描，請確認信任來源後再下載。",
                        "risk_level": row["risk_level"],
                        "scan_status": row["scan_status"],
                    }), 409
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            mark_album_share_link_accessed(conn, share["id"])
            log_share_access_event(conn, share_type="album", share_id=share["id"], ip=get_client_ip(), user_agent=get_ua())
            log_file_access(conn, file_id=row["id"], actor_user_id=None, action="album_share_download", result="allowed", reason="album_share_link", ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            inline = request.args.get("inline") == "1"
            response = _send_readable_file(row, as_attachment=not inline, download_name=row["display_name"] or row["original_filename_plain_for_public"] or "download.bin")
            if response is None:
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            return response
        finally:
            conn.close()
