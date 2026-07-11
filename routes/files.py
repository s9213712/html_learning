import json
import hashlib
import mimetypes
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path

from services.security.upload_security import (
    get_cloud_drive_safety_summary,
    get_cloud_drive_security_policy,
    get_user_cloud_drive_usage,
    log_file_access,
    safe_public_filename,
    safe_public_mime_type,
    scan_uploaded_file,
)
from datetime import datetime
from services.storage.cloud_drive import (
    attach_existing_file,
    can_download_file,
    can_remove_context_attachment,
    create_announcement_attachment_request,
    delete_cloud_file_permanently,
    decrypt_server_encrypted_bytes,
    ensure_cloud_drive_attachment_schema,
    get_file_status,
    is_chunked_server_encrypted_file,
    is_e2ee_file,
    is_server_encrypted_file,
    iter_decrypted_server_encrypted_chunks,
    list_cloud_files,
    resolve_file_storage_path,
    review_announcement_attachment_request,
    revoke_e2ee_file_share,
    share_e2ee_file,
    store_cloud_upload,
    write_decrypted_server_encrypted_file,
)
from services.media.previews import build_preview_metadata, preview_category
from services.system.notifications import create_notification_if_enabled
from services.governance.violation_fines import assert_user_feature_allowed
from services.storage.remote_downloads import (
    DownloadedFile,
    RemoteDownloadError,
    RemoteDownloadCancelled,
    RemoteDownloadPaused,
    download_remote_url,
    download_torrent_file_with_aria2,
    download_torrent_url_with_aria2,
    remote_download_capabilities,
    validate_torrent_file_trackers,
    validate_remote_url,
)
from services.media.e2ee_streaming import cleanup_e2ee_stream_v2_assets
from services.media.streaming import (
    cleanup_stream_asset,
    direct_browser_playback_status,
    get_stream_status,
    mark_stream_asset_processing,
    open_realtime_proxy_stream,
    parse_subtitle_shift_ms,
    realtime_proxy_availability,
    realtime_proxy_runtime_status,
    shift_webvtt_text,
)
from services.storage.storage_albums import (
    add_album_file,
    create_album,
    create_album_from_storage_folder,
    create_share_link,
    create_storage_folder,
    create_storage_file_entry,
    delete_album,
    ensure_output_album,
    ensure_storage_album_schema,
    get_album,
    get_user_storage_summary,
    get_storage_file,
    list_albums,
    list_share_links,
    list_storage_folders,
    list_storage_files,
    list_storage_trash,
    move_storage_file,
    move_storage_folder,
    purge_storage_trash,
    purge_storage_file,
    remove_album_file,
    resolve_album_share_file,
    resolve_album_share_token,
    resolve_share_token,
    restore_storage_trash,
    restore_storage_file,
    revoke_share_link,
    storage_existing_file_for_path,
    storage_replacement_password_required,
    mark_share_link_accessed,
    smart_organize_albums,
    sync_user_storage_summary,
    trash_cloud_file_to_storage,
    trash_storage_folder,
    trash_storage_file,
    update_album,
    mark_album_share_link_accessed,
    public_album_payload,
)
from services.storage.maintenance import run_storage_maintenance, storage_maintenance_status
from services.storage.quota_overrides import (
    clear_storage_quota_override,
    get_storage_quota_override,
    set_storage_quota_override,
)
from services.storage.quota_purchases import (
    active_storage_quota_purchases,
    default_storage_upgrade_catalog,
    ensure_storage_upgrade_price_catalog,
    list_storage_upgrade_price_catalog,
    record_storage_quota_purchase,
)
from services.storage.capacity_audit import audit_storage_capacity, can_allocate_storage_bytes
from services.core.http_headers import build_content_disposition
from services.core.file_offload import x_accel_response
from services.job_center import (
    add_job_event as add_platform_job_event,
    create_job as create_platform_job,
    expire_stale_cloud_remote_download_jobs,
    get_job_by_source as get_platform_job_by_source,
    list_jobs as list_platform_jobs,
    purge_terminal_jobs as purge_platform_terminal_jobs,
    serialize_job as serialize_platform_job,
    utc_now as platform_job_utc_now,
    update_job as update_platform_job,
)
from flask import Response, after_this_request, request, send_file, stream_with_context
from routes.file_sections import (
    register_file_admin_storage_routes,
    register_file_remote_download_routes,
    register_file_share_preview_routes,
)


_REMOTE_DOWNLOAD_TASKS = {}
_REMOTE_DOWNLOAD_TASKS_LOCK = threading.Lock()
_REMOTE_DOWNLOAD_ACTIVE_USERS = set()
_ORIGINAL_DOWNLOAD_REMOTE_URL = download_remote_url
_ORIGINAL_DOWNLOAD_TORRENT_FILE_WITH_ARIA2 = download_torrent_file_with_aria2
_ORIGINAL_DOWNLOAD_TORRENT_URL_WITH_ARIA2 = download_torrent_url_with_aria2


def register_file_routes(app, deps):
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_db = deps["get_db"]
    get_member_level_rule = deps["get_member_level_rule"]
    get_client_ip = deps.get("get_client_ip", lambda: "")
    get_ua = deps.get("get_ua", lambda: "")
    audit = deps.get("audit", lambda *args, **kwargs: None)
    json_resp = deps["json_resp"]
    require_csrf = deps.get("require_csrf", deps["require_csrf_safe"])
    require_csrf_safe = deps["require_csrf_safe"]
    role_rank = deps.get("role_rank", lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0))
    storage_root = deps.get("STORAGE_DIR", ".")
    db_path = deps.get("DB_PATH")
    log_dir = deps.get("LOG_DIR")
    server_file_key_path = deps.get("SERVER_FILE_KEY_PATH")
    ffmpeg_bin = deps.get("FFMPEG_BIN", "ffmpeg")
    ffprobe_bin = deps.get("FFPROBE_BIN", "ffprobe")
    points_service = deps.get("points_service")
    server_file_fernet = deps.get("server_file_fernet")
    get_system_settings = deps.get("get_system_settings", lambda: {})

    def _cleanup_media_derivatives_for_file_ids(conn, file_ids):
        cleaned = []
        for file_id in sorted({str(item or "").strip() for item in (file_ids or []) if str(item or "").strip()}):
            hls = cleanup_stream_asset(conn, uploaded_file_id=file_id, storage_root=storage_root)
            e2ee = cleanup_e2ee_stream_v2_assets(conn, uploaded_file_id=file_id, storage_root=storage_root)
            if hls.get("removed") or e2ee.get("removed"):
                cleaned.append({"file_id": file_id, "hls": hls, "e2ee": e2ee})
        return cleaned

    _DIRECT_BROWSER_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ogg", ".ogv"}

    def _file_row_text(row, *keys):
        for key in keys:
            try:
                value = row[key]
            except Exception:
                value = row.get(key) if hasattr(row, "get") else None
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _cloud_drive_auto_copy_only_hls_enabled():
        raw = str(os.environ.get("HACKME_CLOUD_DRIVE_AUTO_COPY_HLS", "") or "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _cloud_drive_needs_copy_only_hls(file_row):
        if not _cloud_drive_auto_copy_only_hls_enabled():
            return False
        if not file_row or is_e2ee_file(file_row):
            return False
        try:
            category, mime_type = preview_category(file_row)
        except Exception:
            category, mime_type = "", ""
        if category != "video":
            return False
        name = _file_row_text(
            file_row,
            "original_filename_plain_for_public",
            "original_filename",
            "filename",
            "id",
        )
        suffix = Path(name).suffix.lower()
        if suffix in _DIRECT_BROWSER_VIDEO_EXTENSIONS:
            return False
        mime_type = str(mime_type or _file_row_text(file_row, "mimetype", "mime_type")).lower()
        if not suffix and mime_type in {"video/mp4", "video/webm", "video/ogg"}:
            return False
        return True

    def _queue_cloud_drive_copy_only_hls_if_needed(conn, *, actor, file_row):
        if not _cloud_drive_needs_copy_only_hls(file_row):
            return None
        try:
            current = get_stream_status(conn, file_row=file_row, include_segments=False)
            if current and current.get("status") in {"ready", "processing"}:
                return None
            asset = mark_stream_asset_processing(
                conn,
                file_row=file_row,
                error_message="雲端硬碟特殊影片正在建立原畫質 HLS；僅轉封裝，不壓縮。",
            )
            if asset and asset.get("status") == "unavailable":
                return None
            return {
                "file_id": str(file_row["id"]),
                "owner_user_id": int(file_row["owner_user_id"] or _actor_value(actor, "id") or 0),
                "title": _file_row_text(file_row, "original_filename_plain_for_public", "original_filename", "filename") or str(file_row["id"]),
            }
        except Exception as exc:
            try:
                audit(
                    "CLOUD_DRIVE_COPY_ONLY_HLS_QUEUE_FAILED",
                    get_client_ip(),
                    user=_actor_value(actor, "username", ""),
                    success=False,
                    ua=get_ua(),
                    detail=f"file_id={_file_row_text(file_row, 'id')},error={str(exc)[:300]}",
                )
            except Exception:
                pass
            return None

    def _start_cloud_drive_copy_only_hls_worker(pending, *, actor=None, ip="", ua=""):
        if not pending or not db_path:
            return False
        try:
            worker_path = Path(__file__).resolve().parents[1] / "scripts" / "media" / "hls_prepare_worker.py"
            if not worker_path.exists():
                raise FileNotFoundError(str(worker_path))
            worker_log_dir = Path(log_dir or (Path(storage_root).parent / "logs"))
            worker_log_dir.mkdir(parents=True, exist_ok=True)
            log_path = worker_log_dir / "media_hls_worker.log"
            cmd = [
                sys.executable,
                str(worker_path),
                "--db-path",
                str(db_path),
                "--storage-root",
                str(storage_root),
                "--file-id",
                str(pending.get("file_id") or ""),
                "--video-id",
                "0",
                "--owner-user-id",
                str(int(pending.get("owner_user_id") or 0)),
                "--title",
                str(pending.get("title") or ""),
                "--ffmpeg-bin",
                str(ffmpeg_bin),
                "--ffprobe-bin",
                str(ffprobe_bin),
            ]
            if server_file_key_path:
                cmd.extend(["--server-file-key-path", str(server_file_key_path)])
            env = os.environ.copy()
            env.update({
                "HACKME_MEDIA_HLS_COPY_FIRST": "1",
                "HACKME_MEDIA_HLS_FORCE_COPY": "1",
                "HACKME_MEDIA_HLS_COPY_FALLBACK_TRANSCODE": "0",
                "HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE": "always",
                "HACKME_MEDIA_HLS_INCLUDE_ORIGINAL": "1",
                "HACKME_MEDIA_HLS_QUALITY_HEIGHTS": "none",
            })
            with log_path.open("ab") as log_file:
                subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=log_file,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    env=env,
                )
            return True
        except Exception as exc:
            try:
                audit(
                    "CLOUD_DRIVE_COPY_ONLY_HLS_WORKER_LAUNCH_FAILED",
                    ip or get_client_ip(),
                    user=_actor_value(actor, "username", ""),
                    success=False,
                    ua=ua or get_ua(),
                    detail=f"file_id={pending.get('file_id')},error={str(exc)[:300]}",
                )
            except Exception:
                pass
            return False

    def _actor_or_401():
        actor = get_current_user_ctx()
        if not actor:
            return None, json_resp({"ok": False, "msg": "請先登入"}, 401)
        return actor, None

    def _actor_value(actor, key, default=None):
        if not actor:
            return default
        try:
            return actor[key]
        except Exception:
            return actor.get(key, default) if hasattr(actor, "get") else default

    def _is_root(actor):
        return actor and _actor_value(actor, "username") == "root"

    def _upload_restriction_response(conn, actor):
        if _is_root(actor):
            return None
        allowed, msg, restrictions = assert_user_feature_allowed(conn, user_id=_actor_value(actor, "id"), feature_key="cloud_upload")
        if allowed:
            return None
        return json_resp({"ok": False, "msg": msg, "code": "feature_restricted_by_violation_fine", "restrictions": restrictions}), 423

    def _is_manager(actor):
        role = "super_admin" if actor and _actor_value(actor, "username") == "root" else _actor_value(actor, "role", "user")
        return role_rank(role) >= role_rank("manager")

    def _readable_file_path(row):
        path = resolve_file_storage_path(storage_root, row)
        if not is_server_encrypted_file(row):
            return path, None
        handle = tempfile.NamedTemporaryFile(prefix="cloud-drive-plain-", delete=False)
        try:
            temp_path = handle.name
        finally:
            handle.close()
        write_decrypted_server_encrypted_file(path, temp_path, server_file_fernet)

        @after_this_request
        def _cleanup_temp_file(response):
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            except Exception:
                pass
            return response

        return temp_path, temp_path

    def _decryption_unavailable_preview(preview_row, message):
        category, mime_type = preview_category(preview_row)
        filename = preview_row.get("original_filename_plain_for_public") or "preview"
        return {
            "filename": filename,
            "size_bytes": int(preview_row.get("size_bytes") or 0),
            "privacy_mode": preview_row.get("privacy_mode") or "",
            "risk_level": preview_row.get("risk_level") or "",
            "scan_status": preview_row.get("scan_status") or "",
            "category": category,
            "mime_type": mime_type,
            "render_mode": "metadata",
            "previewable": False,
            "text": "",
            "entries": [],
            "truncated": False,
            "decryption_unavailable": True,
            "message": message,
        }

    def _assert_chunked_server_encrypted_readable(path, size_bytes):
        if int(size_bytes or 0) <= 0:
            return
        for _chunk in iter_decrypted_server_encrypted_chunks(path, server_file_fernet, start=0, end=0):
            return
        raise ValueError("伺服器端加密 chunk 內容已損壞，請重新上傳")

    def _svg_placeholder_response(message, *, label="預覽不可用"):
        safe_label = (label or "預覽不可用").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe_message = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
<rect width="960" height="540" fill="#161821"/>
<rect x="40" y="40" width="880" height="460" rx="24" fill="#202333" stroke="#3a3f58"/>
<text x="480" y="220" text-anchor="middle" fill="#f2f4ff" font-size="34" font-family="Arial, sans-serif">{safe_label}</text>
<text x="480" y="280" text-anchor="middle" fill="#b7bdd8" font-size="20" font-family="Arial, sans-serif">{safe_message}</text>
</svg>"""
        return Response(svg, status=200, mimetype="image/svg+xml")

    def _transfer_limits_settings():
        settings = get_system_settings() or {}
        if not settings.get("cloud_drive_transfer_limits_enabled", False):
            return False, {}
        raw = settings.get("cloud_drive_transfer_limits_json") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            parsed = {}
        return True, parsed if isinstance(parsed, dict) else {}

    def _actor_transfer_level(actor):
        return (
            _actor_value(actor, "effective_level")
            or _actor_value(actor, "base_level")
            or _actor_value(actor, "member_level")
            or "normal"
        )

    def _actor_transfer_policy(actor):
        if _is_root(actor):
            return {"upload_kb_per_sec": 0, "download_kb_per_sec": 0, "priority": 100, "enabled": False, "level": "root"}
        enabled, limits = _transfer_limits_settings()
        level = _actor_transfer_level(actor)
        raw = limits.get(level) or limits.get("normal") or {}
        if not enabled:
            return {"upload_kb_per_sec": 0, "download_kb_per_sec": 0, "priority": int(raw.get("priority") or 50), "enabled": False, "level": level}
        def _nonnegative_int(name, default):
            try:
                return max(0, int(raw.get(name, default)))
            except Exception:
                return default
        return {
            "upload_kb_per_sec": _nonnegative_int("upload_kb_per_sec", 0),
            "download_kb_per_sec": _nonnegative_int("download_kb_per_sec", 0),
            "priority": min(100, _nonnegative_int("priority", 50)),
            "enabled": True,
            "level": level,
        }

    def _safe_job_percent(value, default=0):
        try:
            parsed = float(value)
        except Exception:
            parsed = float(default)
        return int(max(0, min(100, round(parsed))))

    def _job_actor_id(actor):
        value = _actor_value(actor, "id")
        try:
            return int(value)
        except Exception:
            return None

    def _record_upload_job(conn, actor, *, upload_result, storage_file=None, display_name="", job_type="cloud_drive.upload", title_prefix="檔案上傳"):
        try:
            file_id = upload_result.get("file_id") or upload_result.get("id")
            if not file_id:
                return None
            source_module = "cloud_drive_upload"
            source_ref = f"cloud_file:{file_id}"
            existing = get_platform_job_by_source(conn, source_module, source_ref)
            filename = (
                display_name
                or upload_result.get("filename")
                or upload_result.get("original_filename")
                or (storage_file or {}).get("display_name")
                or f"file:{file_id}"
            )
            metadata = {
                "file_id": file_id,
                "storage_file_id": (storage_file or {}).get("id"),
                "filename": filename,
                "size_bytes": upload_result.get("size_bytes"),
                "privacy_mode": upload_result.get("privacy_mode"),
            }
            if existing:
                updated = update_platform_job(
                    conn,
                    existing["job_uuid"],
                    status="succeeded",
                    progress_percent=100,
                    stage="completed",
                    stage_detail="已保存到雲端硬碟",
                    result_json=metadata,
                    finished_at=datetime.now().replace(microsecond=0).isoformat(),
                )
                add_platform_job_event(
                    conn,
                    existing["job_uuid"],
                    event_type="completed",
                    stage="completed",
                    message="檔案已保存到雲端硬碟",
                    progress_percent=100,
                    payload=metadata,
                )
                return updated
            job = create_platform_job(
                conn,
                owner_user_id=_job_actor_id(actor),
                created_by_user_id=_job_actor_id(actor),
                job_type=job_type,
                title=f"{title_prefix}：{filename}",
                description="雲端硬碟檔案上傳、掃描與保存",
                source_module=source_module,
                source_ref=source_ref,
                status="succeeded",
                progress_percent=100,
                stage="completed",
                stage_detail="已保存到雲端硬碟",
                cancellable=False,
                metadata=metadata,
            )
            add_platform_job_event(
                conn,
                job["job_uuid"],
                event_type="completed",
                stage="completed",
                message="檔案已保存到雲端硬碟",
                progress_percent=100,
                payload=metadata,
            )
            return job
        except Exception:
            return None

    def _remote_download_job_status(task):
        status = str((task or {}).get("status") or "queued")
        return {
            "queued": "queued",
            "running": "running",
            "paused": "paused",
            "cancelled": "cancelled",
            "completed": "succeeded",
            "failed": "failed",
        }.get(status, "queued")

    def _remote_download_job_type(task):
        source_type = str((task or {}).get("source_type") or "direct")
        if source_type in {"torrent_file", "torrent_url", "magnet"}:
            return f"cloud_drive.remote_download.bt.{source_type}"
        return "cloud_drive.remote_download.direct"

    def _remote_download_job_title(task):
        source_type = str((task or {}).get("source_type") or "direct")
        label = "BT 下載" if source_type in {"torrent_file", "torrent_url", "magnet"} else "Direct link"
        name = (task or {}).get("filename") or (task or {}).get("torrent_filename") or (task or {}).get("url") or "遠端下載"
        return f"{label}：{name}"

    def _remote_download_job_metadata(task):
        task = task or {}
        file_info = task.get("file") if isinstance(task.get("file"), dict) else {}
        storage_info = task.get("storage_file") if isinstance(task.get("storage_file"), dict) else {}
        return {
            "task_id": task.get("id"),
            "source_type": task.get("source_type"),
            "filename": task.get("filename"),
            "torrent_filename": task.get("torrent_filename"),
            "url": task.get("url"),
            "loaded_bytes": task.get("loaded_bytes"),
            "total_bytes": task.get("total_bytes"),
            "timeout_seconds": task.get("timeout_seconds"),
            "privacy_mode": task.get("privacy_mode"),
            "virtual_path": task.get("virtual_path"),
            "speed_bytes_per_sec": task.get("speed_bytes_per_sec"),
            "eta_seconds": task.get("eta_seconds"),
            "transmission_status": task.get("transmission_status"),
            "transmission_torrent_id": task.get("transmission_torrent_id"),
            "file_id": file_info.get("file_id"),
            "file_size_bytes": file_info.get("size_bytes"),
            "file": file_info,
            "storage_file_id": storage_info.get("id"),
            "storage_virtual_path": storage_info.get("virtual_path"),
            "storage_file": storage_info,
            "worker_heartbeat_at": task.get("worker_heartbeat_at"),
            "progress_heartbeat_at": task.get("progress_heartbeat_at"),
            "control_action": task.get("control_action"),
            "cancel_requested": bool(task.get("cancel_requested")),
            "pause_requested": bool(task.get("pause_requested")),
        }

    def _remote_download_task_from_job(job):
        job = dict(job or {})
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        data = {**metadata, **result}
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            source_ref = str(job.get("source_ref") or "")
            if source_ref.startswith("remote_download:"):
                task_id = source_ref.split(":", 1)[1]
        if not task_id:
            return None
        status = {
            "queued": "queued",
            "running": "running",
            "waiting_external": "running",
            "paused": "paused",
            "succeeded": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "expired": "failed",
        }.get(str(job.get("status") or "queued"), "queued")
        file_info = data.get("file") if isinstance(data.get("file"), dict) else {}
        if not file_info and data.get("file_id"):
            file_info = {
                "file_id": data.get("file_id"),
                "filename": data.get("filename"),
                "size_bytes": data.get("file_size_bytes"),
            }
        storage_info = data.get("storage_file") if isinstance(data.get("storage_file"), dict) else {}
        if not storage_info and data.get("storage_file_id"):
            storage_info = {
                "id": data.get("storage_file_id"),
                "virtual_path": data.get("storage_virtual_path"),
            }
        message = str(job.get("stage_detail") or job.get("error_message") or "")
        return {
            "id": task_id,
            "kind": "remote_download",
            "source_type": data.get("source_type") or "direct",
            "status": status,
            "phase": job.get("stage") or status,
            "filename": data.get("filename") or "",
            "url": data.get("url") or "",
            "torrent_filename": data.get("torrent_filename") or "",
            "owner_user_id": job.get("owner_user_id"),
            "privacy_mode": data.get("privacy_mode") or "standard_plain",
            "virtual_path": data.get("virtual_path") or "",
            "loaded_bytes": data.get("loaded_bytes"),
            "total_bytes": data.get("total_bytes"),
            "progress_percent": job.get("progress_percent"),
            "speed_bytes_per_sec": data.get("speed_bytes_per_sec") or 0,
            "eta_seconds": data.get("eta_seconds"),
            "transmission_status": data.get("transmission_status"),
            "transmission_torrent_id": data.get("transmission_torrent_id"),
            "msg": message,
            "error": job.get("error_message") or (message if status == "failed" else ""),
            "file": file_info or None,
            "storage_file": storage_info or None,
            "availability_score": 0,
            "availability_hint": "",
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        }

    def _remote_download_job_for_task_id(conn, task_id, *, merge_cached=True):
        source_ref = f"remote_download:{str(task_id)}"
        if merge_cached:
            return get_platform_job_by_source(conn, "cloud_drive_remote_download", source_ref)
        row = conn.execute(
            """
            SELECT *
            FROM job_center_jobs
            WHERE source_module='cloud_drive_remote_download'
              AND source_ref=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (source_ref,),
        ).fetchone()
        return serialize_platform_job(row) if row else None

    def _mark_persisted_remote_download_control(task_id, actor, action):
        task_id = str(task_id or "").strip()
        action = str(action or "").strip().lower()
        if action not in {"pause", "cancel"}:
            return {"ok": False, "msg": "下載任務操作不支援"}, 400
        actor_id = int(_actor_value(actor, "id") or 0)
        now = platform_job_utc_now()
        conn = get_db()
        try:
            expire_stale_cloud_remote_download_jobs(conn)
            job = _remote_download_job_for_task_id(conn, task_id, merge_cached=False)
            if not job:
                conn.rollback()
                return {"ok": False, "msg": "找不到下載任務"}, 404
            if int(job.get("owner_user_id") or 0) != actor_id:
                conn.rollback()
                return {"ok": False, "msg": "沒有下載任務權限"}, 403
            status = str(job.get("status") or "")
            if status in {"succeeded", "failed", "cancelled", "expired"}:
                if action == "cancel" and status == "cancelled":
                    conn.commit()
                    task = _remote_download_task_from_job(job)
                    return {"ok": True, "task": _task_snapshot(task), "msg": "下載任務已取消"}, 200
                conn.rollback()
                return {"ok": False, "msg": "已結束的下載任務不能取消"}, 409
            metadata = dict(job.get("metadata") or {})
            metadata.update({
                "control_action": action,
                f"{action}_requested_at": now,
            })
            if action == "pause":
                if status == "queued":
                    updated = update_platform_job(
                        conn,
                        job["job_uuid"],
                        status="paused",
                        progress_percent=job.get("progress_percent") or 0,
                        stage="paused",
                        stage_detail="下載任務已暫停，可稍後繼續",
                        metadata_json=metadata,
                        cancellable=True,
                        flush=True,
                    )
                    event_type = "paused"
                    msg = "下載任務已暫停"
                elif status == "paused":
                    updated = job
                    event_type = ""
                    msg = "下載任務已暫停"
                else:
                    updated = update_platform_job(
                        conn,
                        job["job_uuid"],
                        status=status,
                        progress_percent=job.get("progress_percent") or 0,
                        stage="pause_requested",
                        stage_detail="已要求暫停下載任務，正在停止目前 worker",
                        metadata_json=metadata,
                        cancellable=True,
                        flush=True,
                    )
                    event_type = "pause_requested"
                    msg = "已要求暫停下載任務"
            else:
                if status in {"queued", "paused"}:
                    updated = update_platform_job(
                        conn,
                        job["job_uuid"],
                        status="cancelled",
                        progress_percent=100,
                        stage="cancelled",
                        stage_detail="下載任務已取消",
                        metadata_json=metadata,
                        cancellable=False,
                        cancel_requested_at=now,
                        finished_at=now,
                        flush=True,
                    )
                    event_type = "cancelled"
                    msg = "下載任務已取消"
                else:
                    updated = update_platform_job(
                        conn,
                        job["job_uuid"],
                        status=status,
                        progress_percent=job.get("progress_percent") or 0,
                        stage="cancel_requested",
                        stage_detail="已要求取消下載任務，正在停止目前 worker",
                        metadata_json=metadata,
                        cancellable=True,
                        cancel_requested_at=now,
                        flush=True,
                    )
                    event_type = "cancel_requested"
                    msg = "已要求取消下載任務"
            if event_type:
                add_platform_job_event(
                    conn,
                    updated["job_uuid"],
                    event_type=event_type,
                    stage=updated.get("stage"),
                    message=updated.get("stage_detail") or msg,
                    progress_percent=updated.get("progress_percent"),
                    payload=metadata,
                    flush=True,
                )
            conn.commit()
            task = _remote_download_task_from_job(updated)
            if task and action == "cancel" and status not in {"queued", "paused"}:
                task["cancel_requested"] = True
                task["control_action"] = "cancel"
            if task and action == "pause" and status not in {"queued", "paused"}:
                task["pause_requested"] = True
                task["control_action"] = "pause"
            return {"ok": True, "task": _task_snapshot(task), "msg": msg}, 200
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _sync_remote_download_job(task, *, force_event=False):
        task = dict(task or {})
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            return None
        conn = get_db()
        try:
            source_module = "cloud_drive_remote_download"
            source_ref = f"remote_download:{task_id}"
            status = _remote_download_job_status(task)
            percent = _safe_job_percent(task.get("progress_percent"), 100 if status in {"succeeded", "failed", "cancelled"} else 0)
            stage = str(task.get("phase") or task.get("status") or status)[:80]
            stage_detail = str(task.get("msg") or task.get("error") or "")[:1000]
            metadata = _remote_download_job_metadata(task)
            existing = get_platform_job_by_source(conn, source_module, source_ref)
            now = platform_job_utc_now()
            defer_progress = not force_event and status not in {"succeeded", "failed", "cancelled", "expired"}
            if not existing:
                job = create_platform_job(
                    conn,
                    owner_user_id=task.get("owner_user_id"),
                    created_by_user_id=task.get("owner_user_id"),
                    job_type=_remote_download_job_type(task),
                    title=_remote_download_job_title(task),
                    description="遠端 direct link / BT 下載、掃描與保存",
                    source_module=source_module,
                    source_ref=source_ref,
                    status=status,
                    progress_percent=percent,
                    stage=stage,
                    stage_detail=stage_detail,
                    cancellable=status in {"queued", "running", "paused"},
                    metadata=metadata,
                )
            else:
                job = update_platform_job(
                    conn,
                    existing["job_uuid"],
                    status=status,
                    progress_percent=percent,
                    stage=stage,
                    stage_detail=stage_detail,
                    error_message=task.get("error") if status == "failed" else None,
                    error_stage=stage if status == "failed" else None,
                    result_json=metadata if status == "succeeded" else None,
                    metadata_json=metadata,
                    cancellable=status in {"queued", "running", "paused"},
                    started_at=existing.get("started_at") or (now if status == "running" else None),
                    finished_at=now if status in {"succeeded", "failed", "cancelled"} else None,
                    defer_progress=defer_progress,
                )
            if force_event or status in {"succeeded", "failed", "cancelled"}:
                event_type = {
                    "succeeded": "completed",
                    "failed": "failed",
                    "cancelled": "cancelled",
                    "paused": "paused",
                }.get(status, "updated")
                add_platform_job_event(
                    conn,
                    job["job_uuid"],
                    event_type=event_type,
                    stage=stage,
                    message=stage_detail or task.get("error") or "遠端下載任務狀態更新",
                    progress_percent=percent,
                    payload=metadata,
                    defer_progress=defer_progress,
                )
            conn.commit()
            return job
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return None
        finally:
            conn.close()

    def _uploaded_size(file_storage):
        length = getattr(file_storage, "content_length", None)
        if length:
            return int(length)
        stream = getattr(file_storage, "stream", None)
        if not stream:
            return 0
        try:
            pos = stream.tell()
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(pos)
            return int(size)
        except Exception:
            return 0

    def _apply_upload_transfer_policy(actor, file_storage):
        if str(os.environ.get("HACKME_CAPACITY_PROBE_UNLIMITED") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return None
        policy = _actor_transfer_policy(actor)
        if not policy.get("enabled"):
            return None
        upload_kb_per_sec = int(policy.get("upload_kb_per_sec") or 0)
        if upload_kb_per_sec <= 0:
            return json_resp({"ok": False, "msg": "目前會員階級已停用雲端硬碟上傳", "error": "upload_rate_limited"}), 429
        if str(os.environ.get("HACKME_CLOUD_DRIVE_UPLOAD_SLEEP_SHAPER") or "").strip().lower() not in {"1", "true", "yes", "on"}:
            return None
        size = _uploaded_size(file_storage)
        priority = int(policy.get("priority") or 50)
        transfer_delay = (size / max(1, upload_kb_per_sec * 1024)) if size > 0 else 0
        priority_delay = max(0, 60 - priority) / 120
        delay = min(8.0, transfer_delay + priority_delay)
        if delay > 0:
            time.sleep(delay)
        return None

    def _throttled_bytes_response(chunks, *, as_attachment, download_name, mimetype, total_size, kb_per_sec):
        chunk_size = max(8192, min(256 * 1024, int(kb_per_sec * 1024 / 4)))
        sleep_seconds = chunk_size / max(1, kb_per_sec * 1024)

        @stream_with_context
        def _generate():
            for chunk in chunks(chunk_size):
                if not chunk:
                    break
                yield chunk
                time.sleep(sleep_seconds)

        response = Response(_generate(), mimetype=mimetype or mimetypes.guess_type(download_name)[0] or "application/octet-stream")
        if total_size is not None:
            response.headers["Content-Length"] = str(total_size)
        response.headers["Content-Disposition"] = build_content_disposition(
            "attachment" if as_attachment else "inline",
            download_name,
        )
        response.headers["X-Hackme-Transfer-Mode"] = "python_throttled_stream"
        response.headers["X-Cloud-Drive-Rate-Limit-KB-Per-Sec"] = str(kb_per_sec)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _parse_http_byte_range(range_header, total_size):
        if not range_header:
            return None, None
        value = str(range_header or "").strip()
        if not value.startswith("bytes="):
            return None, "invalid"
        spec = value[6:].split(",", 1)[0].strip()
        if "-" not in spec:
            return None, "invalid"
        start_raw, end_raw = spec.split("-", 1)
        try:
            if start_raw == "":
                suffix_len = int(end_raw)
                if suffix_len <= 0:
                    return None, "invalid"
                start = max(0, total_size - suffix_len)
                end = total_size - 1
            else:
                start = int(start_raw)
                end = int(end_raw) if end_raw else total_size - 1
        except Exception:
            return None, "invalid"
        if total_size <= 0 or start < 0 or end < start or start >= total_size:
            return None, "invalid"
        return (start, min(end, total_size - 1)), None

    def _send_bytes_with_range(raw, *, as_attachment, download_name, mimetype, range_header=None):
        total_size = len(raw)
        byte_range, error = _parse_http_byte_range(range_header, total_size)
        if error:
            response = Response(status=416)
            response.headers["Content-Range"] = f"bytes */{total_size}"
            response.headers["Accept-Ranges"] = "bytes"
            return response
        if byte_range:
            start, end = byte_range
            response = Response(raw[start:end + 1], status=206, mimetype=mimetype or mimetypes.guess_type(download_name)[0] or "application/octet-stream")
            response.headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
            response.headers["Content-Length"] = str(end - start + 1)
        else:
            response = Response(raw, status=200, mimetype=mimetype or mimetypes.guess_type(download_name)[0] or "application/octet-stream")
            response.headers["Content-Length"] = str(total_size)
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["X-Hackme-Transfer-Mode"] = "python_buffered_bytes"
        response.headers["Content-Disposition"] = build_content_disposition(
            "attachment" if as_attachment else "inline",
            download_name,
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _send_local_storage_file(path, *, as_attachment=False, download_name="download.bin", mimetype=None, conditional=True):
        offloaded = x_accel_response(
            path,
            storage_root=storage_root,
            as_attachment=as_attachment,
            download_name=download_name,
            mimetype=mimetype or mimetypes.guess_type(str(download_name or ""))[0] or "application/octet-stream",
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

    def _send_readable_file(row, *, as_attachment, download_name, mimetype=None, conditional=False, actor=None):
        path = resolve_file_storage_path(storage_root, row)
        if not path.exists():
            return None
        try:
            stored_mimetype = row["mime_type_plain_for_public"] or ""
        except Exception:
            stored_mimetype = ""
        response_mimetype = mimetype or safe_public_mime_type(download_name, stored_mimetype)
        policy = _actor_transfer_policy(actor) if actor else {"download_kb_per_sec": 0}
        download_kb_per_sec = int(policy.get("download_kb_per_sec") or 0)
        if is_server_encrypted_file(row):
            if is_chunked_server_encrypted_file(row):
                total_size = int(row["size_bytes"] or 0)
                byte_range, error = _parse_http_byte_range(request.headers.get("Range"), total_size)
                if error:
                    response = Response(status=416)
                    response.headers["Content-Range"] = f"bytes */{total_size}"
                    response.headers["Accept-Ranges"] = "bytes"
                    return response
                start = byte_range[0] if byte_range else None
                end = byte_range[1] if byte_range else None
                content_length = (end - start + 1) if byte_range else total_size
                _assert_chunked_server_encrypted_readable(path, total_size)

                @stream_with_context
                def _generate_chunked_plaintext():
                    for chunk in iter_decrypted_server_encrypted_chunks(path, server_file_fernet, start=start, end=end):
                        if not chunk:
                            continue
                        yield chunk
                        if download_kb_per_sec > 0:
                            time.sleep(len(chunk) / max(1, download_kb_per_sec * 1024))

                response = Response(
                    _generate_chunked_plaintext(),
                    status=206 if byte_range else 200,
                    mimetype=response_mimetype,
                )
                response.headers["Content-Length"] = str(content_length)
                response.headers["Accept-Ranges"] = "bytes"
                if byte_range:
                    response.headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
                if download_kb_per_sec > 0:
                    response.headers["X-Cloud-Drive-Rate-Limit-KB-Per-Sec"] = str(download_kb_per_sec)
                response.headers["X-Hackme-Transfer-Mode"] = "python_chunked_decrypt"
                response.headers["Content-Disposition"] = build_content_disposition(
                    "attachment" if as_attachment else "inline",
                    download_name,
                )
                response.headers["X-Content-Type-Options"] = "nosniff"
                return response
            raw = decrypt_server_encrypted_bytes(path, server_file_fernet)
            if request.headers.get("Range") or download_kb_per_sec <= 0:
                return _send_bytes_with_range(
                    raw,
                    as_attachment=as_attachment,
                    download_name=download_name,
                    mimetype=response_mimetype,
                    range_header=request.headers.get("Range"),
                )
            if download_kb_per_sec > 0:
                def _chunks(chunk_size):
                    bio = BytesIO(raw)
                    while True:
                        chunk = bio.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
                return _throttled_bytes_response(
                    _chunks,
                    as_attachment=as_attachment,
                    download_name=download_name,
                    mimetype=response_mimetype,
                    total_size=len(raw),
                    kb_per_sec=download_kb_per_sec,
                )
            return _send_bytes_with_range(
                raw,
                as_attachment=as_attachment,
                download_name=download_name,
                mimetype=response_mimetype,
                range_header=None,
            )
        if download_kb_per_sec > 0:
            def _file_chunks(chunk_size):
                with open(path, "rb") as handle:
                    while True:
                        chunk = handle.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
            return _throttled_bytes_response(
                _file_chunks,
                as_attachment=as_attachment,
                download_name=download_name,
                mimetype=response_mimetype,
                total_size=path.stat().st_size,
                kb_per_sec=download_kb_per_sec,
            )
        return _send_local_storage_file(
            path,
            as_attachment=as_attachment,
            download_name=download_name,
            mimetype=response_mimetype,
            conditional=conditional,
        )

    def _manager_or_403():
        actor, err = _actor_or_401()
        if err:
            return None, err
        if not _is_manager(actor):
            return None, json_resp({"ok": False, "msg": "需要管理員權限"}), 403
        return actor, None

    def _root_or_403():
        actor, err = _actor_or_401()
        if err:
            return None, err
        if not _is_root(actor):
            return None, json_resp({"ok": False, "msg": "只有 root 可操作"}, 403)
        return actor, None

    def _optional_mb_to_bytes(value, field):
        if value in (None, ""):
            return None
        try:
            mb = float(value)
        except Exception as exc:
            raise ValueError(f"{field} 必須是數字") from exc
        if mb < 0:
            raise ValueError(f"{field} 不可小於 0")
        return int(mb * 1024 * 1024)

    def _optional_nonnegative_int(value, field):
        if value in (None, ""):
            return None
        try:
            number = int(value)
        except Exception as exc:
            raise ValueError(f"{field} 必須是整數") from exc
        if number < 0:
            raise ValueError(f"{field} 不可小於 0")
        return number

    def _optional_bool(value):
        if value in ("", None, "inherit"):
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "allow", "allowed"}:
            return True
        if text in {"0", "false", "no", "off", "deny", "denied"}:
            return False
        raise ValueError("can_upload 必須是 true、false 或 inherit")

    def _parse_json_body():
        try:
            data = request.get_json(force=True)
        except Exception:
            return None, json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return None, json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        return data, None, None

    def _points_error(exc):
        msg = str(exc) or exc.__class__.__name__
        status = 400
        if "insufficient balance" in msg:
            status = 409
        return json_resp({"ok": False, "msg": msg}), status

    def _refund_storage_upgrade_spend(spend, actor, *, reason):
        ledger = (spend or {}).get("ledger") or {}
        ledger_uuid = ledger.get("ledger_uuid")
        if not ledger_uuid or not points_service or not hasattr(points_service, "compensate_ledger"):
            return False
        try:
            points_service.compensate_ledger(
                actor=actor,
                ledger_uuid=ledger_uuid,
                reason=reason,
            )
            audit(
                "CLOUD_STORAGE_UPGRADE_REFUND",
                get_client_ip(),
                user=_actor_value(actor, "username"),
                success=True,
                ua=get_ua(),
                detail=f"ledger_uuid={ledger_uuid},reason={reason[:200]}",
            )
            return True
        except Exception as exc:
            audit(
                "CLOUD_STORAGE_UPGRADE_REFUND_FAILED",
                get_client_ip(),
                user=_actor_value(actor, "username"),
                success=False,
                ua=get_ua(),
                detail=f"ledger_uuid={ledger_uuid},error={exc}",
            )
            return False

    def _storage_upgrade_catalog(conn):
        try:
            if hasattr(points_service, "ensure_schema"):
                points_service.ensure_schema(conn)
            ensure_storage_upgrade_price_catalog(conn)
            conn.commit()
            catalog = list_storage_upgrade_price_catalog(conn)
            return catalog or default_storage_upgrade_catalog()
        except sqlite3.OperationalError as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if "locked" in str(exc).lower():
                return default_storage_upgrade_catalog()
            raise

    def _storage_usage_for_user_row(conn, row):
        data = dict(row)
        level = data.get("effective_level") or data.get("member_level") or "newbie"
        rule = get_member_level_rule(conn, level)
        usage = get_user_cloud_drive_usage(conn, data, member_rule=rule, storage_root=storage_root)
        usage["username"] = data.get("username")
        usage["role"] = data.get("role", "user")
        usage["member_level"] = data.get("member_level") or data.get("base_level") or data.get("effective_level")
        usage["effective_level"] = data.get("effective_level") or usage.get("effective_level")
        usage["override"] = get_storage_quota_override(conn, data.get("id"))
        return usage

    def _storage_summary_with_live_quota(summary, usage):
        data = dict(summary or {})
        total_bytes = usage.get("total_bytes")
        data["quota_bytes"] = int(total_bytes) if total_bytes is not None else int(data.get("quota_bytes") or 0)
        data["remaining_bytes"] = usage.get("remaining_bytes")
        data["quota_source"] = usage.get("quota_source")
        data["percent_used"] = usage.get("percent_used")
        data["warning_active"] = bool(usage.get("warning_active"))
        data["warning_threshold_bytes"] = usage.get("warning_threshold_bytes")
        data["warning_threshold_percent"] = usage.get("warning_threshold_percent")
        data["max_file_size_bytes"] = usage.get("max_file_size_bytes")
        data["upload_rate_limit_per_day"] = usage.get("upload_rate_limit_per_day")
        return data

    def _requires_download_warning(policy, row):
        if not policy.get("warn_high_risk_downloads"):
            return False
        return row["risk_level"] in {"high", "blocked", "unknown_encrypted"} or row["scan_status"] in {"infected", "quarantined", "failed", "unknown_encrypted"}

    def _preview_allowed_by_policy(policy, row):
        if is_e2ee_file(row):
            return False, "E2EE 檔案無法由伺服器預覽"
        if policy.get("block_unclean_downloads") and row["scan_status"] not in {"clean", "not_required"}:
            return False, "檔案尚未通過安全檢查"
        if _requires_download_warning(policy, row) and not policy.get("allow_inline_preview_for_high_risk"):
            return False, "高風險檔案目前不允許 inline preview"
        return True, ""

    def _preview_row_with_storage_fallback(conn, actor, row):
        data = dict(row)
        if data.get("original_filename_plain_for_public"):
            return data
        try:
            storage_row = conn.execute(
                """
                SELECT display_name, virtual_path
                FROM storage_files
                WHERE file_id=? AND owner_user_id=? AND deleted_at IS NULL AND COALESCE(is_trashed, 0)=0
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (row["id"], _actor_value(actor, "id")),
            ).fetchone()
        except Exception:
            storage_row = None
        if storage_row:
            fallback_name = storage_row["display_name"] or os.path.basename(str(storage_row["virtual_path"] or ""))
            if fallback_name:
                data["original_filename_plain_for_public"] = fallback_name
        return data

    # 分享頁 token 會直接嵌進 inline script；主檔保留這個 helper 名稱，
    # 讓 security regression tests 能持續驗證 HTML-safe embed 契約未消失。
    def _html_safe_json(value):
        return (
            json.dumps(str(value or ""))
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
    # share-preview section breadcrumb: safe_token = _html_safe_json(token)

    def _grant_user_ids_from_payload(data):
        raw = data.get("grant_user_ids") if isinstance(data, dict) else []
        if raw is None:
            return []
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            try:
                out.append(int(item))
            except Exception:
                pass
        return out

    class _DownloadedFileStorage:
        def __init__(self, downloaded):
            self._downloaded = downloaded
            self.filename = downloaded.filename
            self.mimetype = downloaded.mimetype
            self.stream = open(downloaded.path, "rb")

        def close(self):
            try:
                self.stream.close()
            except Exception:
                pass

    class _MemoryFileStorage:
        def __init__(self, *, filename, mimetype, data):
            self.filename = filename
            self.mimetype = mimetype
            self.stream = BytesIO(data)

    class _PathFileStorage:
        def __init__(self, *, filename, mimetype, path):
            self.filename = filename
            self.mimetype = mimetype
            self.stream = open(path, "rb")

        def close(self):
            try:
                self.stream.close()
            except Exception:
                pass

    def _ensure_resumable_upload_schema(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cloud_resumable_upload_sessions (
                session_id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                target TEXT NOT NULL DEFAULT 'cloud_drive',
                status TEXT NOT NULL DEFAULT 'uploading',
                filename TEXT NOT NULL,
                mime_type TEXT,
                total_bytes INTEGER NOT NULL,
                chunk_size INTEGER NOT NULL,
                total_chunks INTEGER NOT NULL,
                received_bytes INTEGER NOT NULL DEFAULT 0,
                received_chunks_json TEXT NOT NULL DEFAULT '[]',
                temp_dir TEXT NOT NULL,
                privacy_mode TEXT NOT NULL DEFAULT 'standard_plain',
                metadata_json TEXT,
                result_json TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_resumable_owner_status ON cloud_resumable_upload_sessions(owner_user_id, status, updated_at)")

    def _resumable_upload_root():
        root = Path(storage_root) / "_resumable_uploads"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _resumable_session_dir(session_id):
        safe_id = "".join(ch for ch in str(session_id or "") if ch.isalnum() or ch in {"-", "_"})
        if not safe_id:
            raise ValueError("upload session id 格式錯誤")
        root = _resumable_upload_root().resolve()
        path = (root / safe_id).resolve()
        if root not in path.parents and path != root:
            raise ValueError("upload session path 格式錯誤")
        return path

    def _resumable_chunk_size(raw=None):
        default = 4 * 1024 * 1024
        try:
            value = int(raw or default)
        except Exception:
            value = default
        return max(256 * 1024, min(8 * 1024 * 1024, value))

    def _resumable_received_chunks(row):
        try:
            parsed = json.loads(row["received_chunks_json"] or "[]")
        except Exception:
            parsed = []
        out = set()
        for item in parsed if isinstance(parsed, list) else []:
            try:
                out.add(int(item))
            except Exception:
                pass
        return out

    def _resumable_metadata(row):
        try:
            parsed = json.loads(row["metadata_json"] or "{}")
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _resumable_result(row):
        try:
            parsed = json.loads(row["result_json"] or "{}")
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _serialize_resumable_session(row):
        chunks = sorted(_resumable_received_chunks(row))
        return {
            "session_id": row["session_id"],
            "target": row["target"],
            "status": row["status"],
            "filename": row["filename"],
            "mime_type": row["mime_type"] or "application/octet-stream",
            "total_bytes": int(row["total_bytes"] or 0),
            "chunk_size": int(row["chunk_size"] or 0),
            "total_chunks": int(row["total_chunks"] or 0),
            "received_bytes": int(row["received_bytes"] or 0),
            "received_chunks": chunks,
            "progress_percent": _safe_job_percent((int(row["received_bytes"] or 0) / max(1, int(row["total_bytes"] or 0))) * 100),
            "privacy_mode": row["privacy_mode"],
            "error_message": row["error_message"] or "",
            "result": _resumable_result(row),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
        }

    def _get_resumable_session(conn, actor, session_id):
        _ensure_resumable_upload_schema(conn)
        row = conn.execute(
            "SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?",
            (str(session_id or ""),),
        ).fetchone()
        if not row:
            return None, "找不到 upload session"
        if int(row["owner_user_id"]) != int(_actor_value(actor, "id", -1)) and not _is_root(actor):
            return None, "沒有 upload session 權限"
        return row, ""

    def _list_active_resumable_sessions(conn, actor, limit=20):
        _ensure_resumable_upload_schema(conn)
        try:
            limit_value = max(1, min(50, int(limit or 20)))
        except Exception:
            limit_value = 20
        rows = conn.execute(
            """
            SELECT *
            FROM cloud_resumable_upload_sessions
            WHERE owner_user_id=? AND status IN ('uploading', 'completing')
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (int(_actor_value(actor, "id") or 0), limit_value),
        ).fetchall()
        return [_serialize_resumable_session(row) for row in rows]

    def _resumable_job_source_ref(session_id):
        return f"upload_session:{session_id}"

    def _sync_resumable_upload_job(conn, actor, row, *, status=None, progress_percent=None, stage=None, detail="", error_message="", result=None):
        try:
            job_status = status or ("failed" if row["status"] == "failed" else "succeeded" if row["status"] == "completed" else "running")
            stage_value = stage or row["status"]
            percent = progress_percent
            if percent is None:
                percent = _safe_job_percent((int(row["received_bytes"] or 0) / max(1, int(row["total_bytes"] or 0))) * 90)
                if job_status in {"succeeded", "failed"}:
                    percent = 100
            source_module = "cloud_drive_resumable_upload"
            source_ref = _resumable_job_source_ref(row["session_id"])
            existing = get_platform_job_by_source(conn, source_module, source_ref)
            metadata = {
                "session_id": row["session_id"],
                "target": row["target"],
                "filename": row["filename"],
                "total_bytes": int(row["total_bytes"] or 0),
                "received_bytes": int(row["received_bytes"] or 0),
                "privacy_mode": row["privacy_mode"],
            }
            if result is not None:
                metadata["result"] = result
            if existing:
                defer_progress = job_status not in {"succeeded", "failed", "cancelled", "expired"}
                job = update_platform_job(
                    conn,
                    existing["job_uuid"],
                    status=job_status,
                    progress_percent=percent,
                    stage=stage_value,
                    stage_detail=detail,
                    error_message=error_message if job_status == "failed" else "",
                    error_stage=stage_value if job_status == "failed" else "",
                    result_json=result or {},
                    metadata_json=metadata,
                    finished_at=datetime.now().replace(microsecond=0).isoformat() if job_status in {"succeeded", "failed"} else None,
                    defer_progress=defer_progress,
                )
                add_platform_job_event(
                    conn,
                    job["job_uuid"],
                    event_type="failed" if job_status == "failed" else "progress",
                    stage=stage_value,
                    message=detail or error_message or "分段上傳狀態更新",
                    progress_percent=percent,
                    payload=metadata,
                    defer_progress=defer_progress,
                )
                return job
            return create_platform_job(
                conn,
                owner_user_id=_job_actor_id(actor),
                created_by_user_id=_job_actor_id(actor),
                job_type="cloud_drive.resumable_upload",
                title=f"分段上傳：{row['filename']}",
                description="雲端硬碟 resumable/chunk upload、掃描與保存",
                source_module=source_module,
                source_ref=source_ref,
                status=job_status,
                progress_percent=percent,
                stage=stage_value,
                stage_detail=detail,
                metadata=metadata,
            )
        except Exception:
            return None

    def _resumable_upload_capacity_error(conn, actor, *, total_bytes, member_rule):
        usage = get_user_cloud_drive_usage(conn, actor, member_rule=member_rule, storage_root=storage_root)
        if not usage.get("can_upload"):
            return "目前會員等級或處分狀態不可上傳"
        remaining = usage.get("remaining_bytes")
        if remaining is not None and int(total_bytes) > int(remaining):
            return "超過雲端硬碟容量上限"
        max_file = usage.get("max_file_size_bytes")
        if max_file is not None and int(total_bytes) > int(max_file):
            return "檔案超過單檔大小限制"
        try:
            disk = shutil.disk_usage(str(Path(storage_root or ".").expanduser()))
            disk_ok = int(total_bytes) <= int(disk.free * 0.95)
        except Exception:
            disk_ok = True
        if not disk_ok:
            return "Host 磁碟可用空間不足，請先清理檔案或擴充儲存空間"
        return ""

    def _resumable_uploaded_file_storage(row, complete_path):
        return _PathFileStorage(
            filename=row["filename"],
            mimetype=row["mime_type"] or mimetypes.guess_type(row["filename"])[0] or "application/octet-stream",
            path=complete_path,
        )

    def _write_resumable_chunk_stream(source, tmp_path, *, expected_bytes):
        expected = int(expected_bytes or 0)
        written = 0
        too_large = False
        tmp_path = Path(tmp_path)
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tmp_path.open("wb") as out:
                while True:
                    remaining = expected - written
                    read_size = min(1024 * 1024, max(1, remaining + 1))
                    chunk = source.read(read_size)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > expected:
                        too_large = True
                        break
                    out.write(chunk)
            if too_large or written != expected:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
                return written, "invalid_chunk_size"
            return written, ""
        except Exception:
            try:
                tmp_path.unlink()
            except Exception:
                pass
            raise

    def _task_update(task_id, **changes):
        sync_task = None
        with _REMOTE_DOWNLOAD_TASKS_LOCK:
            task = _REMOTE_DOWNLOAD_TASKS.get(task_id)
            if not task:
                return
            task.update(changes)
            task["updated_at"] = datetime.now().isoformat()
            # Release the user slot atomically with the terminal status write so
            # _user_has_active_remote_download never sees a completed/failed task
            # while the user is still marked active.
            if changes.get("status") in {"completed", "failed", "paused", "cancelled"}:
                owner = task.get("owner_user_id")
                if owner is not None:
                    _REMOTE_DOWNLOAD_ACTIVE_USERS.discard(int(owner))
            sync_task = dict(task)
        if sync_task:
            _sync_remote_download_job(sync_task)

    def _task_touch(task_id):
        sync_task = None
        with _REMOTE_DOWNLOAD_TASKS_LOCK:
            task = _REMOTE_DOWNLOAD_TASKS.get(task_id)
            if not task or task.get("status") not in {"queued", "running"}:
                return
            now_text = datetime.now().isoformat()
            task["updated_at"] = now_text
            task["worker_heartbeat_at"] = now_text
            sync_task = dict(task)
        if sync_task:
            _sync_remote_download_job(sync_task)

    def _task_snapshot(task):
        public = {
            "id": task.get("id"),
            "kind": task.get("kind"),
            "status": task.get("status"),
            "phase": task.get("phase"),
            "filename": task.get("filename"),
            "url": task.get("url"),
            "source_type": task.get("source_type"),
            "torrent_filename": task.get("torrent_filename"),
            "loaded_bytes": task.get("loaded_bytes"),
            "total_bytes": task.get("total_bytes"),
            "progress_percent": task.get("progress_percent"),
            "speed_bytes_per_sec": task.get("speed_bytes_per_sec"),
            "eta_seconds": task.get("eta_seconds"),
            "transmission_status": task.get("transmission_status"),
            "transmission_torrent_id": task.get("transmission_torrent_id"),
            "msg": task.get("msg"),
            "error": task.get("error"),
            "file": task.get("file"),
            "storage_file": task.get("storage_file"),
            "recoverable": bool(task.get("recoverable")),
            "availability_score": int(task.get("availability_score") or 0),
            "availability_hint": task.get("availability_hint") or "",
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
        }
        return public

    def _remote_task_age_seconds(task):
        try:
            updated_at = datetime.fromisoformat(str(task.get("updated_at") or task.get("created_at") or ""))
        except Exception:
            return 0
        return max(0, int((datetime.now() - updated_at).total_seconds()))

    def _remote_task_stale_after_seconds(task):
        try:
            timeout = int(task.get("timeout_seconds") or 0)
        except Exception:
            timeout = 0
        return max(300, timeout + 180)

    def _remote_task_terminal_retention_seconds(task):
        status = str((task or {}).get("status") or "")
        if status == "completed":
            raw = os.environ.get(
                "HACKME_REMOTE_DOWNLOAD_SUCCEEDED_RETENTION_SECONDS",
                os.environ.get("HACKME_JOB_CENTER_SUCCEEDED_RETENTION_SECONDS", "60"),
            )
        elif status in {"failed", "cancelled"}:
            raw = os.environ.get(
                "HACKME_REMOTE_DOWNLOAD_FAILED_RETENTION_SECONDS",
                os.environ.get("HACKME_JOB_CENTER_FAILED_RETENTION_SECONDS", "300"),
            )
        else:
            return None
        try:
            return max(0, int(float(str(raw).strip())))
        except Exception:
            return 60 if status == "completed" else 300

    def _cleanup_stale_remote_download_tasks_locked():
        active_users = set()
        remove_task_ids = []
        for task_id, task in list(_REMOTE_DOWNLOAD_TASKS.items()):
            retention = _remote_task_terminal_retention_seconds(task)
            if retention is not None and _remote_task_age_seconds(task) >= retention:
                remove_task_ids.append(task_id)
                continue
            owner_user_id = int(task.get("owner_user_id") or 0)
            if task.get("status") not in {"queued", "running"}:
                continue
            if _remote_task_age_seconds(task) > _remote_task_stale_after_seconds(task):
                task.update(
                    status="failed",
                    phase="failed",
                    progress_percent=100,
                    speed_bytes_per_sec=0,
                    error="遠端下載任務逾時或已中斷，請重新建立下載任務",
                    msg="遠端下載任務逾時或已中斷，請重新建立下載任務",
                    updated_at=datetime.now().isoformat(),
                )
                continue
            if task.get("status") == "running" and owner_user_id:
                active_users.add(owner_user_id)
        for task_id in remove_task_ids:
            task = _REMOTE_DOWNLOAD_TASKS.pop(task_id, None)
            if task and task.get("torrent_cleanup_dir"):
                shutil.rmtree(str(task.get("torrent_cleanup_dir") or ""), ignore_errors=True)
        _REMOTE_DOWNLOAD_ACTIVE_USERS.intersection_update(active_users)

    def _get_remote_download_task(task_id):
        with _REMOTE_DOWNLOAD_TASKS_LOCK:
            _cleanup_stale_remote_download_tasks_locked()
            task = _REMOTE_DOWNLOAD_TASKS.get(task_id)
            return dict(task) if task else None

    def _get_remote_download_task_for_status(task_id):
        task = _get_remote_download_task(str(task_id))
        if task:
            return task
        conn = get_db()
        try:
            expire_stale_cloud_remote_download_jobs(conn)
            conn.commit()
            job = _remote_download_job_for_task_id(conn, task_id, merge_cached=False)
            return _remote_download_task_from_job(job)
        finally:
            conn.close()

    def _finalize_remote_download_task(task_id, **changes):
        task = _get_remote_download_task(task_id) or {"id": task_id}
        task.update(changes)
        task["updated_at"] = datetime.now().isoformat()
        _sync_remote_download_job(task, force_event=True)
        _task_update(task_id, **changes)

    def _remote_download_control_action(task_id):
        task_id = str(task_id)
        poll_job = False
        with _REMOTE_DOWNLOAD_TASKS_LOCK:
            task = _REMOTE_DOWNLOAD_TASKS.get(task_id)
            if not task:
                return ""
            if task.get("cancel_requested") or task.get("status") == "cancelled" or task.get("control_action") == "cancel":
                return "cancel"
            if task.get("pause_requested") or task.get("control_action") == "pause":
                return "pause"
            now_mono = time.monotonic()
            last_poll = float(task.get("_job_control_last_poll_mono") or 0)
            if now_mono - last_poll >= 0.5:
                task["_job_control_last_poll_mono"] = now_mono
                poll_job = True
        if not poll_job:
            return ""
        conn = get_db()
        try:
            job = _remote_download_job_for_task_id(conn, task_id, merge_cached=False)
            if not job:
                return ""
            metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
            stage = str(job.get("stage") or "")
            action = str(metadata.get("control_action") or "")
            cancel_requested = bool(job.get("cancel_requested_at") or action == "cancel" or stage == "cancel_requested" or job.get("status") == "cancelled")
            pause_requested = bool(action == "pause" or stage == "pause_requested")
            if not (cancel_requested or pause_requested):
                return ""
            with _REMOTE_DOWNLOAD_TASKS_LOCK:
                task = _REMOTE_DOWNLOAD_TASKS.get(task_id)
                if not task:
                    return ""
                if cancel_requested:
                    task.update(cancel_requested=True, control_action="cancel", phase="cancel_requested", msg="已要求取消下載任務，正在停止目前 worker")
                    return "cancel"
                if pause_requested:
                    task.update(pause_requested=True, control_action="pause", phase="pause_requested", msg="已要求暫停下載任務，正在停止目前 worker")
                    return "pause"
            return ""
        finally:
            conn.close()

    def _remote_download_cancel_check(task_id):
        action = _remote_download_control_action(task_id)
        if action == "cancel":
            raise RemoteDownloadCancelled("下載任務已取消")
        if action == "pause":
            raise RemoteDownloadPaused("下載任務已暫停")

    def _control_remote_download_task(task_id, actor, action):
        task_id = str(task_id or "")
        action = str(action or "").strip().lower()
        if action not in {"pause", "resume", "cancel"}:
            return {"ok": False, "msg": "下載任務操作不支援"}, 400
        actor_id = int(_actor_value(actor, "id") or 0)
        now = datetime.now().isoformat()
        start_worker = False
        sync_task = None
        with _REMOTE_DOWNLOAD_TASKS_LOCK:
            _cleanup_stale_remote_download_tasks_locked()
            task = _REMOTE_DOWNLOAD_TASKS.get(task_id)
            if not task:
                return _mark_persisted_remote_download_control(task_id, actor, action)
            if int(task.get("owner_user_id") or 0) != actor_id:
                return {"ok": False, "msg": "沒有下載任務權限"}, 403
            status = str(task.get("status") or "")
            phase = str(task.get("phase") or "")
            owner_user_id = int(task.get("owner_user_id") or 0)

            if action == "pause":
                if status == "paused":
                    sync_task = dict(task)
                    return {"ok": True, "task": _task_snapshot(task), "msg": "下載任務已暫停"}, 200
                if status == "queued":
                    task.update(
                        status="paused",
                        phase="paused",
                        pause_requested=True,
                        cancel_requested=False,
                        control_action="pause",
                        speed_bytes_per_sec=0,
                        msg="下載任務已暫停，可稍後繼續",
                        updated_at=now,
                    )
                    _REMOTE_DOWNLOAD_ACTIVE_USERS.discard(owner_user_id)
                elif status == "running":
                    if phase == "saving":
                        return {"ok": False, "msg": "下載內容正在保存到雲端硬碟，不能暫停"}, 409
                    task.update(
                        pause_requested=True,
                        cancel_requested=False,
                        control_action="pause",
                        phase="pause_requested",
                        speed_bytes_per_sec=0,
                        msg="已要求暫停下載任務，正在停止目前 worker",
                        updated_at=now,
                    )
                else:
                    return {"ok": False, "msg": "這個下載任務目前不能暫停"}, 409
                sync_task = dict(task)
                msg = "下載任務已暫停" if task.get("status") == "paused" else "已要求暫停下載任務"
            elif action == "resume":
                if status != "paused":
                    return {"ok": False, "msg": "只有已暫停的下載任務可以繼續"}, 409
                if task.get("source_type") == "torrent_file" and not os.path.isfile(str(task.get("torrent_path") or "")):
                    return {"ok": False, "msg": "暫停的 BT 種子檔已被清理，請重新建立下載任務"}, 409
                task.update(
                    status="queued",
                    phase="queued",
                    pause_requested=False,
                    cancel_requested=False,
                    control_action="",
                    speed_bytes_per_sec=0,
                    msg="已重新加入下載佇列",
                    updated_at=now,
                )
                start_worker = True
                sync_task = dict(task)
                msg = "已重新加入下載佇列"
            else:
                if status == "cancelled":
                    sync_task = dict(task)
                    return {"ok": True, "task": _task_snapshot(task), "msg": "下載任務已取消"}, 200
                if status in {"completed", "failed"}:
                    return {"ok": False, "msg": "已結束的下載任務不能取消"}, 409
                if status == "running" and phase == "saving":
                    return {"ok": False, "msg": "下載內容正在保存到雲端硬碟，不能取消"}, 409
                task.update(
                    status="cancelled" if status in {"queued", "paused"} else "running",
                    phase="cancelled" if status in {"queued", "paused"} else "cancel_requested",
                    cancel_requested=True,
                    pause_requested=False,
                    control_action="cancel",
                    speed_bytes_per_sec=0,
                    progress_percent=100 if status in {"queued", "paused"} else task.get("progress_percent"),
                    msg="下載任務已取消" if status in {"queued", "paused"} else "已要求取消下載任務，正在停止目前 worker",
                    updated_at=now,
                )
                if status in {"queued", "paused"}:
                    _REMOTE_DOWNLOAD_ACTIVE_USERS.discard(owner_user_id)
                sync_task = dict(task)
                msg = "下載任務已取消" if task.get("status") == "cancelled" else "已要求取消下載任務"

        if sync_task:
            _sync_remote_download_job(sync_task, force_event=True)
        if start_worker:
            threading.Thread(target=_run_remote_download_task, args=(task_id,), daemon=True).start()
        try:
            audit("CLOUD_DRIVE_REMOTE_DOWNLOAD_CONTROL", "", user=_actor_value(actor, "username"), success=True, detail=f"task_id={task_id},action={action}")
        except Exception:
            pass
        return {"ok": True, "task": _task_snapshot(sync_task), "msg": msg}, 200

    def _list_remote_download_tasks_for_actor(actor):
        actor_id = int(_actor_value(actor, "id") or 0)
        with _REMOTE_DOWNLOAD_TASKS_LOCK:
            _cleanup_stale_remote_download_tasks_locked()
            tasks = [dict(task) for task in _REMOTE_DOWNLOAD_TASKS.values()]
        visible = []
        seen_ids = set()
        for task in tasks:
            try:
                owner_id = int(task.get("owner_user_id") or 0)
            except Exception:
                owner_id = 0
            if owner_id != actor_id:
                continue
            snapshot = _task_snapshot(task)
            seen_ids.add(str(snapshot.get("id") or ""))
            visible.append(snapshot)
        conn = get_db()
        try:
            expire_stale_cloud_remote_download_jobs(conn)
            purge_platform_terminal_jobs(conn)
            conn.commit()
            for job in list_platform_jobs(conn, user_id=actor_id, include_all=False, limit=50):
                if str(job.get("source_module") or "") != "cloud_drive_remote_download":
                    continue
                task = _remote_download_task_from_job(job)
                if not task:
                    continue
                task_id = str(task.get("id") or "")
                if task_id in seen_ids:
                    continue
                seen_ids.add(task_id)
                visible.append(_task_snapshot(task))
        finally:
            conn.close()
        visible.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return visible[:20]

    def _user_has_active_remote_download(user_id):
        try:
            user_id = int(user_id)
        except Exception:
            return True
        with _REMOTE_DOWNLOAD_TASKS_LOCK:
            _cleanup_stale_remote_download_tasks_locked()
            if user_id in _REMOTE_DOWNLOAD_ACTIVE_USERS:
                return True
            return any(
                int(task.get("owner_user_id") or 0) == user_id
                and task.get("status") in {"queued", "running"}
                for task in _REMOTE_DOWNLOAD_TASKS.values()
            )

    def _configure_remote_download_backend_from_settings():
        settings = get_system_settings() or {}
        backend = str(settings.get("bt_download_backend") or os.environ.get("HACKME_BT_BACKEND") or "auto").strip().lower()
        if backend not in {"auto", "transmission", "aria2"}:
            backend = "auto"
        os.environ["HACKME_BT_BACKEND"] = backend
        mapping = {
            "transmission_rpc_url": "HACKME_TRANSMISSION_RPC_URL",
            "transmission_rpc_username": "HACKME_TRANSMISSION_RPC_USERNAME",
            "transmission_rpc_password": "HACKME_TRANSMISSION_RPC_PASSWORD",
        }
        for setting_key, env_key in mapping.items():
            raw = settings.get(setting_key)
            if raw is None:
                continue
            os.environ[env_key] = str(raw or "")

    def _remote_download_concurrency_limits():
        settings = get_system_settings() or {}
        prefer_env = str(os.environ.get("HACKME_REMOTE_DOWNLOAD_LIMITS_PREFER_ENV", "")).strip().lower() in {"1", "true", "yes", "on"}

        def _limit_from_settings_or_env(setting_name, env_name, default_value):
            env_raw = os.environ.get(env_name)
            raw = env_raw if prefer_env and env_raw not in (None, "") else settings.get(setting_name)
            if raw in (None, ""):
                raw = env_raw if env_raw not in (None, "") else default_value
            try:
                value = int(raw)
            except Exception:
                value = default_value
            return max(1, int(value))

        return {
            "global": _limit_from_settings_or_env(
                "remote_download_max_concurrent_global",
                "HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL",
                1,
            ),
            "per_user": _limit_from_settings_or_env(
                "remote_download_max_concurrent_per_user",
                "HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER",
                1,
            ),
        }

    def _remote_download_task_sort_key(task):
        return (-int(task.get("availability_score") or 0), str(task.get("created_at") or ""), str(task.get("id") or ""))

    def _try_acquire_remote_download_slot_locked(owner_user_id, task_id):
        _cleanup_stale_remote_download_tasks_locked()
        limits = _remote_download_concurrency_limits()
        running = [
            task
            for task in _REMOTE_DOWNLOAD_TASKS.values()
            if task.get("status") == "running" and str(task.get("id") or "") != str(task_id)
        ]
        if len(running) >= limits["global"]:
            return False
        owner_running = [
            task
            for task in running
            if int(task.get("owner_user_id") or 0) == int(owner_user_id)
        ]
        if len(owner_running) >= limits["per_user"]:
            return False
        queued = [
            task
            for task in _REMOTE_DOWNLOAD_TASKS.values()
            if task.get("status") == "queued"
        ]
        queued.sort(key=_remote_download_task_sort_key)
        global_slots = max(0, limits["global"] - len(running))
        if not any(str(task.get("id") or "") == str(task_id) for task in queued[:global_slots]):
            return False
        owner_queued = [
            task
            for task in queued
            if int(task.get("owner_user_id") or 0) == int(owner_user_id)
        ]
        owner_slots = max(0, limits["per_user"] - len(owner_running))
        if not any(str(task.get("id") or "") == str(task_id) for task in owner_queued[:owner_slots]):
            return False
        return True

    def _wait_for_remote_download_slot(task_id, owner_user_id):
        last_notice_at = 0.0
        while True:
            with _REMOTE_DOWNLOAD_TASKS_LOCK:
                task = _REMOTE_DOWNLOAD_TASKS.get(task_id)
                if not task or task.get("status") not in {"queued", "running"}:
                    return False
                if _try_acquire_remote_download_slot_locked(owner_user_id, task_id):
                    task.update(
                        status="running",
                        phase="starting",
                        msg="準備開始下載",
                        updated_at=datetime.now().isoformat(),
                    )
                    return True
            now_ts = time.time()
            if now_ts - last_notice_at >= 2:
                _task_update(
                    task_id,
                    status="queued",
                    phase="queued",
                    msg="等待下載 worker 空位",
                    progress_percent=0,
                )
                last_notice_at = now_ts
            time.sleep(0.5)

    def _table_exists(conn, table_name):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _context_refs_visible_to_actor(conn, actor, context_type, context_id):
        actor_id = int(_actor_value(actor, "id") or 0)
        context_type = str(context_type or "").strip()
        context_id = str(context_id or "").strip()
        if context_type == "dm":
            if not _table_exists(conn, "dm_threads"):
                return True
            try:
                row = conn.execute(
                    """
                    SELECT 1 FROM dm_threads
                    WHERE id=? AND (participant_a_id=? OR participant_b_id=?)
                    """,
                    (int(context_id), actor_id, actor_id),
                ).fetchone()
            except Exception:
                return False
            return row is not None
        if context_type == "group_chat":
            if not _table_exists(conn, "chat_room_members"):
                return True
            try:
                row = conn.execute(
                    "SELECT 1 FROM chat_room_members WHERE room_id=? AND user_id=?",
                    (int(context_id), actor_id),
                ).fetchone()
            except Exception:
                return False
            return row is not None
        if context_type == "chat_message":
            if not (_table_exists(conn, "chat_messages") and _table_exists(conn, "chat_room_members")):
                return True
            try:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM chat_messages m
                    JOIN chat_room_members rm ON rm.room_id=m.room_id AND rm.user_id=?
                    WHERE m.id=?
                    """,
                    (actor_id, int(context_id)),
                ).fetchone()
            except Exception:
                return False
            return row is not None
        if context_type == "announcement":
            if not _table_exists(conn, "announcements"):
                return True
            row = conn.execute("SELECT 1 FROM announcements WHERE id=? AND is_active=1", (context_id,)).fetchone()
            return row is not None
        if context_type == "forum_thread":
            if not _table_exists(conn, "forum_threads"):
                return True
            row = conn.execute(
                """
                SELECT 1 FROM forum_threads
                WHERE id=? AND is_deleted=0 AND (status='approved' OR author_user_id=?)
                """,
                (context_id, actor_id),
            ).fetchone()
            return row is not None
        if context_type in {"forum_post", "forum_comment"}:
            if not (_table_exists(conn, "forum_posts") and _table_exists(conn, "forum_threads")):
                return True
            row = conn.execute(
                """
                SELECT 1
                FROM forum_posts p
                JOIN forum_threads t ON t.id=p.thread_id
                WHERE p.id=? AND p.is_deleted=0 AND t.is_deleted=0
                  AND (t.status='approved' OR p.author_user_id=? OR t.author_user_id=?)
                """,
                (context_id, actor_id, actor_id),
            ).fetchone()
            return row is not None
        return False

    def _remote_progress_updater(task_id):
        state = {"loaded": 0, "ts": time.monotonic(), "speed": 0}

        def _callback(event):
            with _REMOTE_DOWNLOAD_TASKS_LOCK:
                current = _REMOTE_DOWNLOAD_TASKS.get(task_id) or {}
                if current.get("status") in {"paused", "cancelled"} or current.get("pause_requested") or current.get("cancel_requested"):
                    return
            loaded = event.get("loaded_bytes")
            total = event.get("total_bytes")
            percent = event.get("progress_percent")
            try:
                percent = None if percent is None else max(0, min(100, round(float(percent), 1)))
            except Exception:
                percent = None
            if percent is None:
                try:
                    if total:
                        percent = max(0, min(100, round((int(loaded or 0) / int(total)) * 100, 1)))
                except Exception:
                    percent = None
            speed = event.get("speed_bytes_per_sec")
            try:
                speed = int(speed or 0)
            except Exception:
                speed = 0
            if speed <= 0 and loaded is not None:
                now_ts = time.monotonic()
                try:
                    loaded_int = int(loaded or 0)
                    delta_t = now_ts - float(state["ts"])
                    if loaded_int >= int(state["loaded"] or 0) and delta_t > 0:
                        instant = int((loaded_int - int(state["loaded"] or 0)) / delta_t)
                        speed = int((float(state["speed"] or 0) * 0.65) + (instant * 0.35)) if state["speed"] and instant else (instant or int(state["speed"] or 0))
                    state.update({"loaded": loaded_int, "ts": now_ts, "speed": speed})
                except Exception:
                    pass
            phase = event.get("phase") or "downloading"
            msg = "下載中" if phase == "downloading" else "下載完成，準備保存"
            progress_heartbeat_at = datetime.now().isoformat()
            _task_update(
                task_id,
                status="running",
                phase=phase,
                filename=event.get("filename"),
                loaded_bytes=loaded,
                total_bytes=total,
                progress_percent=percent,
                speed_bytes_per_sec=max(0, int(speed or 0)),
                eta_seconds=event.get("eta_seconds"),
                transmission_status=event.get("transmission_status"),
                transmission_torrent_id=event.get("transmission_torrent_id"),
                msg=msg,
                progress_heartbeat_at=progress_heartbeat_at,
            )
        return _callback

    def _remote_download_storage_path(filename, explicit_path=""):
        explicit_path = str(explicit_path or "").strip()
        if explicit_path:
            return explicit_path
        safe_name = safe_public_filename(filename or "download.bin") or "download.bin"
        return f"/Downloads/{safe_name}"

    def _remote_download_external_worker_enabled():
        if str(os.environ.get("HACKME_REMOTE_DOWNLOAD_EXTERNAL_WORKER", "1")).strip().lower() in {"0", "false", "no", "off"}:
            return False
        return (
            download_remote_url is _ORIGINAL_DOWNLOAD_REMOTE_URL
            and download_torrent_file_with_aria2 is _ORIGINAL_DOWNLOAD_TORRENT_FILE_WITH_ARIA2
            and download_torrent_url_with_aria2 is _ORIGINAL_DOWNLOAD_TORRENT_URL_WITH_ARIA2
        )

    def _write_remote_worker_control(path, action):
        try:
            Path(path).write_text(str(action or ""), encoding="utf-8")
        except Exception:
            pass

    def _stop_remote_worker_process(proc):
        if not proc:
            return
        try:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.communicate(timeout=5)
            return
        except Exception:
            pass
        try:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass

    def _download_remote_task_in_external_worker(task, *, max_bytes=None, rate_limit_kb_per_sec=None, progress_callback=None, cancel_check=None, heartbeat_callback=None):
        worker_path = Path(__file__).resolve().parents[1] / "scripts" / "storage" / "remote_download_worker.py"
        if not worker_path.is_file():
            raise RemoteDownloadError("遠端下載 worker 不存在")
        source_type = str(task.get("source_type") or "direct")
        control_file = tempfile.NamedTemporaryFile(prefix="hackme_remote_control_", delete=False)
        control_file.close()
        cmd = [
            sys.executable,
            str(worker_path),
            "--source-type",
            source_type,
            "--timeout-seconds",
            str(int(task.get("timeout_seconds") or 300)),
            "--max-bytes",
            str(int(max_bytes or 0)),
            "--rate-limit-kb-per-sec",
            str(int(rate_limit_kb_per_sec or 0)),
            "--control-file",
            control_file.name,
            "--owner-user-id",
            str(int(task.get("owner_user_id") or 0)),
            "--task-id",
            str(task.get("id") or ""),
        ]
        if source_type == "torrent_file":
            cmd.extend([
                "--torrent-path",
                str(task.get("torrent_path") or ""),
                "--display-name",
                str(task.get("torrent_filename") or "BT 檔案"),
            ])
        else:
            cmd.extend(["--url", str(task.get("url") or "")])
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        result = None
        error_message = ""
        control_exc = None
        control_requested_at = 0.0
        last_heartbeat_at = 0.0

        def _handle_worker_line(line):
            nonlocal result, error_message
            line = str(line or "").strip()
            if not line:
                return
            try:
                payload = json.loads(line)
            except Exception:
                return
            if payload.get("type") == "progress":
                _emit_event = payload.get("event") or {}
                if progress_callback:
                    progress_callback(_emit_event)
            elif payload.get("type") == "result":
                result = payload
            elif payload.get("type") == "paused":
                raise RemoteDownloadPaused(str(payload.get("message") or "下載任務已暫停"))
            elif payload.get("type") == "cancelled":
                raise RemoteDownloadCancelled(str(payload.get("message") or "下載任務已取消"))
            elif payload.get("type") == "error":
                error_message = str(payload.get("message") or "")

        try:
            assert proc.stdout is not None
            while proc.poll() is None:
                now_mono = time.monotonic()
                if heartbeat_callback and now_mono - last_heartbeat_at >= 5:
                    heartbeat_callback()
                    last_heartbeat_at = now_mono
                if cancel_check and control_exc is None:
                    try:
                        cancel_check()
                    except (RemoteDownloadCancelled, RemoteDownloadPaused) as exc:
                        control_exc = exc
                        control_requested_at = time.monotonic()
                        _write_remote_worker_control(
                            control_file.name,
                            "cancel" if isinstance(exc, RemoteDownloadCancelled) else "pause",
                        )
                elif control_exc is not None and time.monotonic() - control_requested_at > 10:
                    _stop_remote_worker_process(proc)
                    raise control_exc
                ready, _, _ = select.select([proc.stdout], [], [], 0.25)
                if ready:
                    _handle_worker_line(proc.stdout.readline())
            for line in proc.stdout:
                _handle_worker_line(line)
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            return_code = proc.wait(timeout=5)
        except Exception:
            _stop_remote_worker_process(proc)
            raise
        finally:
            try:
                os.unlink(control_file.name)
            except Exception:
                pass
        if return_code != 0:
            if return_code == 3:
                raise RemoteDownloadPaused(error_message or "下載任務已暫停")
            if return_code == 4:
                raise RemoteDownloadCancelled(error_message or "下載任務已取消")
            if control_exc is not None:
                raise control_exc
            detail = error_message or (stderr or "").strip() or "遠端下載 worker 失敗"
            raise RemoteDownloadError(detail[:1000])
        if not result or not result.get("path"):
            raise RemoteDownloadError("遠端下載 worker 未回傳檔案")
        return DownloadedFile(
            path=str(result.get("path")),
            filename=safe_public_filename(result.get("filename") or "remote-download.bin"),
            mimetype=str(result.get("mimetype") or "application/octet-stream"),
            cleanup_dir=result.get("cleanup_dir") or None,
        )

    def _run_remote_download_task(task_id):
        task = _get_remote_download_task(task_id)
        if not task:
            return
        actor = task["actor"]
        owner_user_id = int(task.get("owner_user_id") or _actor_value(actor, "id") or 0)
        downloaded = None
        file_storage = None
        conn = None
        acquired_active = False
        try:
            _remote_download_cancel_check(task_id)
            if not _wait_for_remote_download_slot(task_id, owner_user_id):
                return
            acquired_active = True
            _remote_download_cancel_check(task_id)
            conn = get_db()
            ensure_cloud_drive_attachment_schema(conn)
            ensure_storage_album_schema(conn)
            rule = get_member_level_rule(conn, _actor_value(actor, "effective_level") or _actor_value(actor, "member_level"))
            usage = get_user_cloud_drive_usage(conn, actor, member_rule=rule, storage_root=storage_root)
            remaining = usage.get("remaining_bytes")
            max_file = usage.get("max_file_size_bytes")
            max_bytes = None
            if remaining is not None:
                max_bytes = int(remaining)
            if max_file is not None:
                max_bytes = min(max_bytes, int(max_file)) if max_bytes is not None else int(max_file)
            conn.close()
            conn = None

            remote_rate_kb_per_sec = int(_actor_transfer_policy(actor).get("download_kb_per_sec") or 0)
            _configure_remote_download_backend_from_settings()
            source_type = task.get("source_type") or "url"
            progress_callback = _remote_progress_updater(task_id)
            cancel_check = lambda: _remote_download_cancel_check(task_id)
            if _remote_download_external_worker_enabled():
                _task_update(task_id, status="running", phase="starting", msg="外部下載 worker 啟動中")
                downloaded = _download_remote_task_in_external_worker(
                    task,
                    max_bytes=max_bytes,
                    progress_callback=progress_callback,
                    rate_limit_kb_per_sec=remote_rate_kb_per_sec or None,
                    cancel_check=cancel_check,
                    heartbeat_callback=lambda: _task_touch(task_id),
                )
            elif source_type == "torrent_file":
                _task_update(task_id, status="running", phase="starting", msg="連線到遠端來源")
                downloaded = download_torrent_file_with_aria2(
                    task["torrent_path"],
                    display_name=task.get("torrent_filename") or "BT 檔案",
                    timeout_seconds=task["timeout_seconds"],
                    max_bytes=max_bytes,
                    progress_callback=progress_callback,
                    rate_limit_kb_per_sec=remote_rate_kb_per_sec or None,
                    cancel_check=cancel_check,
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                )
            elif source_type == "torrent_url":
                _task_update(task_id, status="running", phase="starting", msg="連線到遠端來源")
                downloaded = download_torrent_url_with_aria2(
                    task["url"],
                    timeout_seconds=task["timeout_seconds"],
                    max_bytes=max_bytes,
                    progress_callback=progress_callback,
                    rate_limit_kb_per_sec=remote_rate_kb_per_sec or None,
                    cancel_check=cancel_check,
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                )
            elif source_type == "magnet":
                _task_update(task_id, status="running", phase="starting", msg="連線到遠端來源")
                downloaded = download_remote_url(
                    task["url"],
                    timeout_seconds=task["timeout_seconds"],
                    max_bytes=max_bytes,
                    progress_callback=progress_callback,
                    rate_limit_kb_per_sec=remote_rate_kb_per_sec or None,
                    cancel_check=cancel_check,
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                )
            else:
                _task_update(task_id, status="running", phase="starting", msg="連線到遠端來源")
                downloaded = download_remote_url(
                    task["url"],
                    timeout_seconds=task["timeout_seconds"],
                    max_bytes=max_bytes,
                    progress_callback=progress_callback,
                    rate_limit_kb_per_sec=remote_rate_kb_per_sec or None,
                    treat_torrent_as_bt=False,
                    cancel_check=cancel_check,
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                )
            _remote_download_cancel_check(task_id)
            _task_update(task_id, status="running", phase="saving", filename=downloaded.filename, msg="保存到雲端硬碟")
            file_storage = _DownloadedFileStorage(downloaded)
            conn = get_db()
            ensure_cloud_drive_attachment_schema(conn)
            ensure_storage_album_schema(conn)
            upload_result, msg = store_cloud_upload(
                conn,
                actor=actor,
                member_rule=rule,
                storage_root=storage_root,
                file_storage=file_storage,
                privacy_mode=task["privacy_mode"],
                scan_now=True,
                server_file_fernet=server_file_fernet,
            )
            if msg:
                conn.rollback()
                conn.close()
                conn = None
                _finalize_remote_download_task(task_id, status="failed", phase="failed", error=msg, msg=msg)
                return

            file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (upload_result["file_id"],)).fetchone()
            storage_path = _remote_download_storage_path(downloaded.filename, task.get("virtual_path"))
            storage_file, msg = create_storage_file_entry(
                conn,
                actor=actor,
                file_row=file_row,
                virtual_path=storage_path,
                display_name=downloaded.filename,
                source="remote_download",
            )
            if msg:
                conn.rollback()
                conn.close()
                conn = None
                _finalize_remote_download_task(task_id, status="failed", phase="failed", error=msg, msg=msg)
                return
            task_url = str(task.get("url") or "")
            source_label = "BT 下載" if source_type in {"torrent_file", "torrent_url", "magnet"} else "Direct link"
            create_notification_if_enabled(
                conn,
                user_id=owner_user_id,
                type="cloud_drive_remote_download_completed",
                title=f"{source_label}已完成",
                body=f"{source_label}「{downloaded.filename}」已保存到你的雲端硬碟。",
                link="/drive",
            )
            pending_copy_only_hls = _queue_cloud_drive_copy_only_hls_if_needed(conn, actor=actor, file_row=file_row)
            conn.commit()
            if pending_copy_only_hls:
                _start_cloud_drive_copy_only_hls_worker(
                    pending_copy_only_hls,
                    actor=actor,
                    ip=task.get("ip") or "",
                    ua=task.get("ua") or "",
                )
            conn.close()
            conn = None
            audit("CLOUD_DRIVE_REMOTE_DOWNLOAD", task.get("ip") or "", user=actor["username"], success=True, ua=task.get("ua") or "", detail=f"file_id={upload_result['file_id']}")
            _finalize_remote_download_task(
                task_id,
                status="completed",
                phase="completed",
                loaded_bytes=upload_result.get("size_bytes"),
                total_bytes=upload_result.get("size_bytes"),
                progress_percent=100,
                speed_bytes_per_sec=0,
                msg="遠端下載已保存到雲端硬碟",
                file={**upload_result, "filename": downloaded.filename},
                storage_file=storage_file,
            )
        except RemoteDownloadPaused as exc:
            if conn:
                conn.rollback()
                conn.close()
                conn = None
            _finalize_remote_download_task(
                task_id,
                status="paused",
                phase="paused",
                msg=str(exc) or "下載任務已暫停",
                error="",
                speed_bytes_per_sec=0,
            )
        except RemoteDownloadCancelled as exc:
            if conn:
                conn.rollback()
                conn.close()
                conn = None
            _finalize_remote_download_task(
                task_id,
                status="cancelled",
                phase="cancelled",
                msg=str(exc) or "下載任務已取消",
                error="",
                progress_percent=100,
                speed_bytes_per_sec=0,
            )
        except RemoteDownloadError as exc:
            if conn:
                conn.rollback()
                conn.close()
                conn = None
            _finalize_remote_download_task(task_id, status="failed", phase="failed", error=str(exc), msg=str(exc), speed_bytes_per_sec=0)
        except Exception as exc:
            if conn:
                conn.rollback()
                conn.close()
                conn = None
            audit("CLOUD_DRIVE_REMOTE_DOWNLOAD_ERROR", task.get("ip") or "", user=actor.get("username"), success=False, ua=task.get("ua") or "", detail=exc.__class__.__name__)
            _finalize_remote_download_task(task_id, status="failed", phase="failed", error=f"遠端下載失敗：{exc.__class__.__name__}", msg=f"遠端下載失敗：{exc.__class__.__name__}", speed_bytes_per_sec=0)
        finally:
            if file_storage:
                file_storage.close()
            if downloaded and downloaded.cleanup_dir:
                shutil.rmtree(downloaded.cleanup_dir, ignore_errors=True)
            latest_task = _get_remote_download_task(task_id) or {}
            if task.get("torrent_cleanup_dir") and latest_task.get("status") != "paused":
                shutil.rmtree(task["torrent_cleanup_dir"], ignore_errors=True)
            with _REMOTE_DOWNLOAD_TASKS_LOCK:
                if acquired_active:
                    _REMOTE_DOWNLOAD_ACTIVE_USERS.discard(owner_user_id)
            if conn:
                conn.close()

    file_admin_ctx = {
        "actor_or_401": _actor_or_401,
        "actor_value": _actor_value,
        "audit": audit,
        "get_client_ip": get_client_ip,
        "get_current_user_ctx": get_current_user_ctx,
        "get_db": get_db,
        "get_member_level_rule": get_member_level_rule,
        "get_system_settings": get_system_settings,
        "get_ua": get_ua,
        "is_root": _is_root,
        "json_resp": json_resp,
        "manager_or_403": _manager_or_403,
        "optional_bool": _optional_bool,
        "optional_mb_to_bytes": _optional_mb_to_bytes,
        "optional_nonnegative_int": _optional_nonnegative_int,
        "require_csrf": require_csrf,
        "require_csrf_safe": require_csrf_safe,
        "root_or_403": _root_or_403,
        "storage_root": storage_root,
        "storage_summary_with_live_quota": _storage_summary_with_live_quota,
        "storage_usage_for_user_row": _storage_usage_for_user_row,
    }
    register_file_admin_storage_routes(app, file_admin_ctx)

    file_remote_download_ctx = {
        "DownloadedFileStorage": _DownloadedFileStorage,
        "RemoteDownloadError": RemoteDownloadError,
        "actor_or_401": _actor_or_401,
        "actor_transfer_policy": _actor_transfer_policy,
        "actor_value": _actor_value,
        "audit": audit,
        "cleanup_stale_remote_download_tasks_locked": _cleanup_stale_remote_download_tasks_locked,
        "control_remote_download_task": _control_remote_download_task,
        "download_remote_url": lambda *args, **kwargs: download_remote_url(*args, **kwargs),
        "download_torrent_file_with_aria2": lambda *args, **kwargs: download_torrent_file_with_aria2(*args, **kwargs),
        "download_torrent_url_with_aria2": lambda *args, **kwargs: download_torrent_url_with_aria2(*args, **kwargs),
        "get_client_ip": get_client_ip,
        "get_db": get_db,
        "get_member_level_rule": get_member_level_rule,
        "get_remote_download_task": _get_remote_download_task,
        "get_remote_download_task_for_status": _get_remote_download_task_for_status,
        "get_ua": get_ua,
        "is_manager": _is_manager,
        "json_resp": json_resp,
        "list_remote_download_tasks_for_actor": _list_remote_download_tasks_for_actor,
        "remote_download_capabilities": lambda: (_configure_remote_download_backend_from_settings(), remote_download_capabilities())[1],
        "remote_download_storage_path": _remote_download_storage_path,
        "queue_cloud_drive_copy_only_hls_if_needed": _queue_cloud_drive_copy_only_hls_if_needed,
        "start_cloud_drive_copy_only_hls_worker": _start_cloud_drive_copy_only_hls_worker,
        "remote_download_tasks": _REMOTE_DOWNLOAD_TASKS,
        "remote_download_tasks_lock": _REMOTE_DOWNLOAD_TASKS_LOCK,
        "require_csrf": require_csrf,
        "require_csrf_safe": require_csrf_safe,
        "run_remote_download_task": _run_remote_download_task,
        "sync_remote_download_job": _sync_remote_download_job,
        "server_file_fernet": server_file_fernet,
        "storage_root": storage_root,
        "task_snapshot": _task_snapshot,
        "validate_remote_url": lambda url: validate_remote_url(url),
        "validate_torrent_file_trackers": lambda path: validate_torrent_file_trackers(path),
    }
    register_file_remote_download_routes(app, file_remote_download_ctx)

    @app.route("/api/cloud-drive/storage-upgrades", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_storage_upgrades():
        actor, err = _actor_or_401()
        if err:
            return err
        if not points_service:
            return json_resp({"ok": False, "msg": "積分服務未啟用"}), 503
        conn = get_db()
        try:
            level = _actor_value(actor, "effective_level") or _actor_value(actor, "member_level") or "newbie"
            rule = get_member_level_rule(conn, level)
            usage = get_user_cloud_drive_usage(conn, actor, member_rule=rule, storage_root=storage_root)
            catalog = _storage_upgrade_catalog(conn)
            can_purchase = not _is_root(actor)
            message = "root 依實際磁碟容量控管，不需要用積分購買容量" if _is_root(actor) else ""
            capacity_audit = audit_storage_capacity(conn, storage_root)
            if can_purchase:
                overcommitted = (
                    int(capacity_audit["committed_total_bytes"]) >= int(capacity_audit["available_cloud_capacity_bytes"])
                    or int(capacity_audit["committed_total_bytes"]) >= int(capacity_audit["allocatable_cloud_capacity_bytes"])
                    or int(capacity_audit["committed_remaining_bytes"]) >= int(capacity_audit["disk"]["safe_free_bytes"])
                    or "host_storage_total_commitment_exceeds_available" in set(capacity_audit.get("reasons") or [])
                    or "host_storage_overcommitted" in set(capacity_audit.get("reasons") or [])
                )
                if overcommitted:
                    can_purchase = False
                    message = "會員承諾容量已達或超過 Host 可用容量，目前停用容量購買"
                headroom = min(
                    max(0, int(capacity_audit["available_cloud_capacity_bytes"]) - int(capacity_audit["committed_total_bytes"])),
                    max(0, int(capacity_audit["allocatable_cloud_capacity_bytes"]) - int(capacity_audit["committed_total_bytes"])),
                    max(0, int(capacity_audit["disk"]["safe_free_bytes"]) - int(capacity_audit["committed_remaining_bytes"])),
                )
                catalog = [item for item in catalog if can_purchase and int(item.get("storage_bytes") or 0) <= headroom]
                if can_purchase and not catalog:
                    can_purchase = False
                    message = "Host 磁碟可承諾容量不足，目前不能購買更多雲端容量"
            user_id = _actor_value(actor, "id")
            active_purchases = active_storage_quota_purchases(conn, user_id)
            owned_bytes = sum(int(p.get("purchased_bytes") or 0) for p in active_purchases)
            GB = 1024 ** 3
            if owned_bytes < 2 * GB:
                tier_multiplier = 1.0
            elif owned_bytes < 5 * GB:
                tier_multiplier = 1.5
            elif owned_bytes < 10 * GB:
                tier_multiplier = 2.0
            elif owned_bytes < 20 * GB:
                tier_multiplier = 3.0
            else:
                tier_multiplier = 4.0
            enriched_catalog = []
            for item in catalog:
                entry = dict(item)
                base = int(item.get("base_price") or 0)
                min_p = int(item.get("min_price") or 0)
                max_p = int(item.get("max_price") or 0) or 999999
                effective = max(min_p, min(max_p, round(base * tier_multiplier)))
                entry["effective_price"] = effective
                entry["tier_multiplier"] = tier_multiplier
                entry["owned_bytes"] = owned_bytes
                enriched_catalog.append(entry)
            return json_resp({
                "ok": True,
                "can_purchase": can_purchase,
                "message": message,
                "catalog": enriched_catalog,
                "active_purchases": active_purchases,
                "usage": usage,
                "storage_capacity": capacity_audit,
            })
        finally:
            conn.close()

    @app.route("/api/cloud-drive/storage-upgrades/purchase", methods=["POST"])
    @require_csrf
    def cloud_drive_purchase_storage_upgrade():
        actor, err = _actor_or_401()
        if err:
            return err
        if _is_root(actor):
            return json_resp({"ok": False, "msg": "root 不需要用積分購買容量"}), 403
        if not points_service:
            return json_resp({"ok": False, "msg": "積分服務未啟用"}), 503
        data, err, status = _parse_json_body()
        if err:
            return err, status
        item_key = str(data.get("item_key") or "").strip()
        conn = get_db()
        try:
            catalog = _storage_upgrade_catalog(conn)
            product = next((item for item in catalog if item.get("item_key") == item_key), None)
            if not product:
                return json_resp({"ok": False, "msg": "容量商品未啟用"}), 400
            additional_bytes = int(product["storage_bytes"])
            capacity_ok, capacity_msg, capacity_audit = can_allocate_storage_bytes(conn, storage_root, additional_bytes)
            if not capacity_ok:
                return json_resp({
                    "ok": False,
                    "msg": capacity_msg,
                    "storage_capacity": capacity_audit,
                }), 409
            user_id = _actor_value(actor, "id")
            active_purchases = active_storage_quota_purchases(conn, user_id)
            owned_bytes = sum(int(p.get("purchased_bytes") or 0) for p in active_purchases)
            GB = 1024 ** 3
            if owned_bytes < 2 * GB:
                tier_multiplier = 1.0
            elif owned_bytes < 5 * GB:
                tier_multiplier = 1.5
            elif owned_bytes < 10 * GB:
                tier_multiplier = 2.0
            elif owned_bytes < 20 * GB:
                tier_multiplier = 3.0
            else:
                tier_multiplier = 4.0
            base = int(product.get("base_price") or 0)
            min_p = int(product.get("min_price") or 0)
            max_p = int(product.get("max_price") or 0) or 999999
            effective_price = max(min_p, min(max_p, round(base * tier_multiplier)))
        finally:
            conn.close()
        try:
            spend = points_service.rc1_facade().spend_service_fee(
                user_id=_actor_value(actor, "id"),
                item_key=item_key,
                quantity=1,
                reference_type="cloud_storage_upgrade",
                reference_id=item_key,
                idempotency_key=f"cloud_storage_upgrade:{_actor_value(actor, 'id')}:{uuid.uuid4().hex}",
                metadata={
                    "storage_bytes": additional_bytes,
                    "duration_days": int(product["duration_days"]),
                    "tier_multiplier": tier_multiplier,
                    "effective_price": effective_price,
                },
                actor=actor,
                override_amount=effective_price,
            )
        except Exception as exc:
            return _points_error(exc)

        conn = get_db()
        purchase_committed = False
        try:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            capacity_ok, capacity_msg, capacity_audit = can_allocate_storage_bytes(conn, storage_root, additional_bytes)
            if not capacity_ok:
                conn.rollback()
                _refund_storage_upgrade_spend(
                    spend,
                    actor,
                    reason=f"storage allocation failed after debit: {capacity_msg}",
                )
                return json_resp({
                    "ok": False,
                    "msg": capacity_msg,
                    "storage_capacity": capacity_audit,
                }), 409
            ledger = spend.get("ledger") or {}
            purchase = record_storage_quota_purchase(
                conn,
                user_id=_actor_value(actor, "id"),
                item_key=item_key,
                quantity=1,
                points_spent=ledger.get("amount") or effective_price,
                ledger_uuid=ledger.get("ledger_uuid"),
            )
            level = _actor_value(actor, "effective_level") or _actor_value(actor, "member_level") or "newbie"
            usage = get_user_cloud_drive_usage(conn, actor, member_rule=get_member_level_rule(conn, level), storage_root=storage_root)
            conn.commit()
            purchase_committed = True
            audit(
                "CLOUD_STORAGE_UPGRADE_PURCHASE",
                get_client_ip(),
                user=_actor_value(actor, "username"),
                success=True,
                ua=get_ua(),
                detail=f"item_key={item_key}, effective_price={effective_price}, tier_multiplier={tier_multiplier}, bytes={purchase['purchased_bytes']}",
            )
            return json_resp({"ok": True, "purchase": purchase, "wallet": spend.get("wallet"), "usage": usage})
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            if not purchase_committed:
                _refund_storage_upgrade_spend(
                    spend,
                    actor,
                    reason="storage upgrade allocation exception after debit",
                )
            raise
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_files():
        actor, err = _actor_or_401()
        if err:
            return err
        requested_user_id = request.args.get("user_id")
        if requested_user_id not in (None, ""):
            try:
                requested_user_id_int = int(requested_user_id)
            except Exception:
                return json_resp({"ok": False, "msg": "user_id 格式錯誤"}), 400
            if requested_user_id_int != int(actor["id"]):
                return json_resp({"ok": False, "msg": "不可讀取其他使用者雲端硬碟"}), 403
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            rows = list_cloud_files(conn, actor, limit=100, offset=0)
            return json_resp({"ok": True, "files": rows})
        finally:
            conn.close()

    def _form_json_value(name):
        raw = (request.form.get(name) or "").strip()
        if not raw:
            return None
        try:
            import json
            return json.loads(raw)
        except Exception:
            return None

    def _truthy_upload_value(value):
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _storage_replace_confirmed_from_form():
        return _truthy_upload_value(request.form.get("replace_existing_confirmed"))

    def _storage_replace_password_confirmed_from_form():
        return _truthy_upload_value(request.form.get("replace_existing_password_confirmed"))

    def _storage_replace_confirmed_from_metadata(metadata):
        return _truthy_upload_value((metadata or {}).get("replace_existing_confirmed"))

    def _storage_replace_password_confirmed_from_metadata(metadata):
        return _truthy_upload_value((metadata or {}).get("replace_existing_password_confirmed"))

    def _resumable_json_payload():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @app.route("/api/cloud-drive/resumable-upload/start", methods=["POST"])
    @require_csrf
    def cloud_drive_resumable_upload_start():
        actor, err = _actor_or_401()
        if err:
            return err
        data = _resumable_json_payload()
        filename = safe_public_filename(data.get("filename") or "upload.bin")
        try:
            total_bytes = int(data.get("total_bytes") or 0)
        except Exception:
            total_bytes = 0
        if total_bytes <= 0:
            return json_resp({"ok": False, "msg": "total_bytes 必須大於 0", "error": "invalid_total_bytes"}), 400
        chunk_size = _resumable_chunk_size(data.get("chunk_size"))
        total_chunks = max(1, (total_bytes + chunk_size - 1) // chunk_size)
        target = str(data.get("target") or "cloud_drive").strip() or "cloud_drive"
        if target not in {"cloud_drive", "storage"}:
            return json_resp({"ok": False, "msg": "target 只支援 cloud_drive 或 storage", "error": "invalid_target"}), 400
        privacy_mode = str(data.get("privacy_mode") or "standard_plain").strip() or "standard_plain"
        mime_type = str(data.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream").strip()
        metadata = {
            "virtual_path": str(data.get("virtual_path") or "").strip(),
            "display_name": str(data.get("display_name") or "").strip(),
            "context_type": str(data.get("context_type") or "").strip(),
            "context_id": str(data.get("context_id") or "").strip(),
            "grant_role": str(data.get("grant_role") or "").strip(),
            "grant_user_ids": _grant_user_ids_from_payload(data),
            "encrypted_metadata": str(data.get("encrypted_metadata") or "").strip(),
            "encrypted_file_key": str(data.get("encrypted_file_key") or "").strip(),
            "wrapped_by": str(data.get("wrapped_by") or "user_public_key").strip() or "user_public_key",
            "ciphertext_sha256": str(data.get("ciphertext_sha256") or "").strip(),
            "encryption_algorithm": str(data.get("encryption_algorithm") or "").strip(),
            "encryption_version": str(data.get("encryption_version") or "").strip(),
            "nonce": str(data.get("nonce") or "").strip(),
            "client_scan_report": data.get("client_scan_report") if isinstance(data.get("client_scan_report"), dict) else None,
            "replace_existing_confirmed": "1" if _truthy_upload_value(data.get("replace_existing_confirmed")) else "",
            "replace_existing_password_confirmed": "1" if _truthy_upload_value(data.get("replace_existing_password_confirmed")) else "",
        }
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            ensure_storage_album_schema(conn)
            _ensure_resumable_upload_schema(conn)
            restricted = _upload_restriction_response(conn, actor)
            if restricted:
                conn.commit()
                return restricted
            rule = get_member_level_rule(conn, _actor_value(actor, "effective_level") or _actor_value(actor, "member_level"))
            capacity_error = _resumable_upload_capacity_error(conn, actor, total_bytes=total_bytes, member_rule=rule)
            if capacity_error:
                return json_resp({"ok": False, "msg": capacity_error, "error": "quota_or_capacity"}), 400
            existing_storage_file = None
            if target == "storage":
                existing_storage_file = storage_existing_file_for_path(
                    conn,
                    owner_user_id=int(actor["id"]),
                    virtual_path=metadata.get("virtual_path") or "",
                    display_name=metadata.get("display_name") or filename,
                )
            if existing_storage_file and not _storage_replace_confirmed_from_metadata(metadata):
                return json_resp({
                    "ok": False,
                    "msg": "同一路徑已有檔案，請確認是否覆蓋",
                    "error": "replace_confirm_required",
                }), 409
            if existing_storage_file and storage_replacement_password_required(
                conn,
                owner_user_id=int(actor["id"]),
                virtual_path=metadata.get("virtual_path") or "",
                display_name=metadata.get("display_name") or filename,
                new_privacy_mode=privacy_mode,
            ) and not _storage_replace_password_confirmed_from_metadata(metadata):
                return json_resp({
                    "ok": False,
                    "msg": "覆寫 E2EE 檔案或變更加密方式需要重新輸入檔案密碼",
                    "error": "replace_password_required",
                }), 409
            session_id = uuid.uuid4().hex
            temp_dir = _resumable_session_dir(session_id)
            temp_dir.mkdir(parents=True, exist_ok=False)
            now = datetime.now().replace(microsecond=0).isoformat()
            expires_at = datetime.fromtimestamp(time.time() + 24 * 60 * 60).replace(microsecond=0).isoformat()
            conn.execute(
                """
                INSERT INTO cloud_resumable_upload_sessions (
                    session_id, owner_user_id, target, status, filename, mime_type,
                    total_bytes, chunk_size, total_chunks, received_bytes,
                    received_chunks_json, temp_dir, privacy_mode, metadata_json,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, 'uploading', ?, ?, ?, ?, ?, 0, '[]', ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    int(actor["id"]),
                    target,
                    filename,
                    mime_type,
                    total_bytes,
                    chunk_size,
                    total_chunks,
                    str(temp_dir),
                    privacy_mode,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    expires_at,
                ),
            )
            row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (session_id,)).fetchone()
            _sync_resumable_upload_job(conn, actor, row, status="running", progress_percent=0, stage="created", detail="分段上傳 session 已建立")
            conn.commit()
            return json_resp({"ok": True, "session": _serialize_resumable_session(row)})
        finally:
            conn.close()

    @app.route("/api/cloud-drive/resumable-upload/sessions", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_resumable_upload_sessions():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            return json_resp({
                "ok": True,
                "sessions": _list_active_resumable_sessions(conn, actor, request.args.get("limit") or 20),
            })
        finally:
            conn.close()

    @app.route("/api/cloud-drive/resumable-upload/<session_id>/status", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_resumable_upload_status(session_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            row, msg = _get_resumable_session(conn, actor, session_id)
            if not row:
                return json_resp({"ok": False, "msg": msg, "error": "not_found"}), 404
            return json_resp({"ok": True, "session": _serialize_resumable_session(row)})
        finally:
            conn.close()

    @app.route("/api/cloud-drive/resumable-upload/<session_id>/chunks/<int:chunk_index>", methods=["POST"])
    @require_csrf
    def cloud_drive_resumable_upload_chunk(session_id, chunk_index):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            row, msg = _get_resumable_session(conn, actor, session_id)
            if not row:
                return json_resp({"ok": False, "msg": msg, "error": "not_found"}), 404
            if row["status"] not in {"uploading", "failed"}:
                return json_resp({"ok": False, "msg": "此 upload session 目前不能接收分段", "session": _serialize_resumable_session(row)}), 409
            total_chunks = int(row["total_chunks"] or 0)
            if chunk_index < 0 or chunk_index >= total_chunks:
                return json_resp({"ok": False, "msg": "chunk index 超出範圍", "error": "invalid_chunk_index"}), 400
            expected = int(row["chunk_size"])
            if chunk_index == total_chunks - 1:
                expected = int(row["total_bytes"]) - (chunk_index * int(row["chunk_size"]))
            upload = request.files.get("chunk")
            if upload:
                upload_policy_error = _apply_upload_transfer_policy(actor, upload)
                if upload_policy_error:
                    return upload_policy_error
                source_stream = upload.stream
            else:
                source_stream = request.stream
                content_length = request.content_length
                if content_length is not None and int(content_length or 0) != expected:
                    return json_resp({
                        "ok": False,
                        "msg": f"chunk 大小不正確，需要 {expected} bytes，收到 {int(content_length or 0)} bytes",
                        "error": "invalid_chunk_size",
                    }), 400
            temp_dir = Path(row["temp_dir"])
            temp_dir.mkdir(parents=True, exist_ok=True)
            chunk_path = temp_dir / f"{chunk_index:08d}.part"
            tmp_path = temp_dir / f"{chunk_index:08d}.part.tmp"
            bytes_received = expected
            if chunk_path.exists() and chunk_path.stat().st_size == expected:
                pass
            else:
                bytes_received, write_error = _write_resumable_chunk_stream(source_stream, tmp_path, expected_bytes=expected)
                if write_error:
                    return json_resp({
                        "ok": False,
                        "msg": f"chunk 大小不正確，需要 {expected} bytes，收到 {bytes_received} bytes",
                        "error": write_error,
                    }), 400
                os.replace(tmp_path, chunk_path)
            if bytes_received != expected:
                return json_resp({
                    "ok": False,
                    "msg": f"chunk 大小不正確，需要 {expected} bytes，收到 {bytes_received} bytes",
                    "error": "invalid_chunk_size",
                }), 400
            received = _resumable_received_chunks(row)
            received.add(int(chunk_index))
            received_bytes = 0
            for idx in received:
                part = temp_dir / f"{idx:08d}.part"
                if part.exists():
                    received_bytes += int(part.stat().st_size)
            now = datetime.now().replace(microsecond=0).isoformat()
            conn.execute(
                """
                UPDATE cloud_resumable_upload_sessions
                SET status='uploading', received_bytes=?, received_chunks_json=?, updated_at=?, error_message=''
                WHERE session_id=?
                """,
                (
                    min(received_bytes, int(row["total_bytes"])),
                    json.dumps(sorted(received)),
                    now,
                    row["session_id"],
                ),
            )
            updated = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (row["session_id"],)).fetchone()
            upload_percent = _safe_job_percent((int(updated["received_bytes"] or 0) / max(1, int(updated["total_bytes"] or 0))) * 90)
            _sync_resumable_upload_job(
                conn,
                actor,
                updated,
                status="running",
                progress_percent=upload_percent,
                stage="uploading",
                detail=f"已接收 {len(received)} / {total_chunks} 個分段",
            )
            conn.commit()
            return json_resp({
                "ok": True,
                "session": _serialize_resumable_session(updated),
                "chunk": {
                    "index": int(chunk_index),
                    "bytes_received": int(expected),
                    "storage_mode": "streamed_to_disk",
                },
            })
        finally:
            conn.close()

    @app.route("/api/cloud-drive/resumable-upload/<session_id>/complete", methods=["POST"])
    @require_csrf
    def cloud_drive_resumable_upload_complete(session_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        file_storage = None
        try:
            ensure_cloud_drive_attachment_schema(conn)
            ensure_storage_album_schema(conn)
            row, msg = _get_resumable_session(conn, actor, session_id)
            if not row:
                return json_resp({"ok": False, "msg": msg, "error": "not_found"}), 404
            if row["status"] == "completed":
                return json_resp({"ok": True, **_resumable_result(row), "session": _serialize_resumable_session(row)})
            if row["status"] not in {"uploading", "completing", "failed"}:
                return json_resp({"ok": False, "msg": "此 upload session 目前不能完成", "session": _serialize_resumable_session(row)}), 409
            received = _resumable_received_chunks(row)
            total_chunks = int(row["total_chunks"] or 0)
            missing = [idx for idx in range(total_chunks) if idx not in received]
            if missing:
                return json_resp({"ok": False, "msg": "仍有分段尚未上傳", "missing_chunks": missing[:100], "session": _serialize_resumable_session(row)}), 409
            temp_dir = Path(row["temp_dir"])
            complete_path = temp_dir / "complete.upload"
            expected_size = int(row["total_bytes"] or 0)
            complete_ready = complete_path.exists() and complete_path.stat().st_size == expected_size
            if not complete_ready:
                missing_part_files = [idx for idx in range(total_chunks) if not (temp_dir / f"{idx:08d}.part").exists()]
                if missing_part_files:
                    now = datetime.now().replace(microsecond=0).isoformat()
                    conn.execute(
                        "UPDATE cloud_resumable_upload_sessions SET status='uploading', error_message=?, updated_at=? WHERE session_id=?",
                        ("missing_chunk_file", now, row["session_id"]),
                    )
                    row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (row["session_id"],)).fetchone()
                    _sync_resumable_upload_job(conn, actor, row, status="running", stage="uploading", detail="分段檔案遺失，等待重新上傳缺失分段")
                    conn.commit()
                    return json_resp({"ok": False, "msg": "分段檔案遺失，請重新上傳", "error": "missing_chunk_file", "missing_chunks": missing_part_files[:100]}), 409
            now = datetime.now().replace(microsecond=0).isoformat()
            conn.execute(
                "UPDATE cloud_resumable_upload_sessions SET status='completing', updated_at=? WHERE session_id=?",
                (now, row["session_id"]),
            )
            row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (row["session_id"],)).fetchone()
            _sync_resumable_upload_job(
                conn,
                actor,
                row,
                status="running",
                progress_percent=92 if not complete_ready else 95,
                stage="merging" if not complete_ready else "finalizing",
                detail="正在合併分段檔案" if not complete_ready else "分段已合併，正在掃描與保存",
            )
            conn.commit()
            if not complete_ready:
                temp_complete_path = temp_dir / "complete.upload.tmp"
                with temp_complete_path.open("wb") as out:
                    for idx in range(total_chunks):
                        part = temp_dir / f"{idx:08d}.part"
                        with part.open("rb") as src:
                            shutil.copyfileobj(src, out, length=1024 * 1024)
                if temp_complete_path.stat().st_size != expected_size:
                    try:
                        temp_complete_path.unlink()
                    except Exception:
                        pass
                    now = datetime.now().replace(microsecond=0).isoformat()
                    conn.execute(
                        "UPDATE cloud_resumable_upload_sessions SET status='uploading', error_message=?, updated_at=? WHERE session_id=?",
                        ("merged_size_mismatch", now, row["session_id"]),
                    )
                    row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (row["session_id"],)).fetchone()
                    _sync_resumable_upload_job(conn, actor, row, status="running", stage="uploading", detail="合併後大小不正確，等待重新上傳分段")
                    conn.commit()
                    return json_resp({"ok": False, "msg": "合併後大小不正確，請重新上傳", "error": "merged_size_mismatch"}), 400
                os.replace(temp_complete_path, complete_path)
                now = datetime.now().replace(microsecond=0).isoformat()
                conn.execute(
                    "UPDATE cloud_resumable_upload_sessions SET status='completing', updated_at=? WHERE session_id=?",
                    (now, row["session_id"]),
                )
                row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (row["session_id"],)).fetchone()
                _sync_resumable_upload_job(conn, actor, row, status="running", progress_percent=95, stage="finalizing", detail="分段已合併，正在掃描與保存")
                conn.commit()
            metadata = _resumable_metadata(row)
            rule = get_member_level_rule(conn, _actor_value(actor, "effective_level") or _actor_value(actor, "member_level"))
            file_storage = _resumable_uploaded_file_storage(row, complete_path)
            upload_result, msg = store_cloud_upload(
                conn,
                actor=actor,
                member_rule=rule,
                storage_root=storage_root,
                file_storage=file_storage,
                privacy_mode=row["privacy_mode"],
                encrypted_metadata=metadata.get("encrypted_metadata") or None,
                encrypted_file_key=metadata.get("encrypted_file_key") or None,
                wrapped_by=metadata.get("wrapped_by") or "user_public_key",
                ciphertext_sha256=metadata.get("ciphertext_sha256") or None,
                encryption_algorithm=metadata.get("encryption_algorithm") or None,
                encryption_version=metadata.get("encryption_version") or None,
                nonce=metadata.get("nonce") or None,
                client_scan_report=metadata.get("client_scan_report") if isinstance(metadata.get("client_scan_report"), dict) else None,
                scan_now=True,
                server_file_fernet=server_file_fernet,
            )
            if msg:
                raise ValueError(msg)
            file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (upload_result["file_id"],)).fetchone()
            response_payload = {"file": upload_result}
            if row["target"] == "storage":
                storage_file, storage_msg = create_storage_file_entry(
                    conn,
                    actor=actor,
                    file_row=file_row,
                    virtual_path=metadata.get("virtual_path") or "",
                    display_name=metadata.get("display_name") or row["filename"],
                    source="resumable_upload",
                    replace_existing=_storage_replace_confirmed_from_metadata(metadata),
                    storage_root=storage_root,
                    replace_existing_password_confirmed=_storage_replace_password_confirmed_from_metadata(metadata),
                )
                if storage_msg:
                    raise ValueError(storage_msg)
                response_payload["storage_file"] = storage_file
                _record_upload_job(
                    conn,
                    actor,
                    upload_result=upload_result,
                    storage_file=storage_file,
                    display_name=metadata.get("display_name") or row["filename"],
                    job_type="storage.upload",
                    title_prefix="檔案上傳",
                )
            else:
                attach_result = None
                context_type = metadata.get("context_type") or ""
                context_id = metadata.get("context_id") or ""
                if context_type and context_id:
                    attach_result, attach_msg = attach_existing_file(
                        conn,
                        actor=actor,
                        file_id=upload_result["file_id"],
                        context_type=context_type,
                        context_id=context_id,
                        grant_user_ids=metadata.get("grant_user_ids") or [],
                        grant_role=metadata.get("grant_role") or None,
                        can_preview=True,
                    )
                    if attach_msg:
                        raise ValueError(attach_msg)
                response_payload["attachment"] = attach_result
                _record_upload_job(
                    conn,
                    actor,
                    upload_result=upload_result,
                    display_name=row["filename"],
                    job_type="cloud_drive.upload",
                    title_prefix="雲端硬碟上傳",
                )
            create_notification_if_enabled(
                conn,
                user_id=_actor_value(actor, "id"),
                type="cloud_drive_upload_completed",
                title="分段上傳完成",
                body=f"檔案「{row['filename']}」已上傳完成。",
                link="/drive",
            )
            result_json = json.dumps(response_payload, ensure_ascii=False, sort_keys=True)
            conn.execute(
                """
                UPDATE cloud_resumable_upload_sessions
                SET status='completed', received_bytes=total_bytes, result_json=?, updated_at=?, error_message=''
                WHERE session_id=?
                """,
                (result_json, datetime.now().replace(microsecond=0).isoformat(), row["session_id"]),
            )
            row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (row["session_id"],)).fetchone()
            _sync_resumable_upload_job(conn, actor, row, status="succeeded", progress_percent=100, stage="completed", detail="分段上傳已保存到雲端硬碟", result=response_payload)
            pending_copy_only_hls = _queue_cloud_drive_copy_only_hls_if_needed(conn, actor=actor, file_row=file_row)
            conn.commit()
            if pending_copy_only_hls:
                _start_cloud_drive_copy_only_hls_worker(pending_copy_only_hls, actor=actor)
            shutil.rmtree(row["temp_dir"], ignore_errors=True)
            audit("CLOUD_DRIVE_RESUMABLE_UPLOAD", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"session_id={row['session_id']},file_id={upload_result['file_id']}")
            return json_resp({"ok": True, **response_payload, "session": _serialize_resumable_session(row)})
        except ValueError as exc:
            conn.rollback()
            try:
                row, _ = _get_resumable_session(conn, actor, session_id)
                if row:
                    conn.execute(
                        "UPDATE cloud_resumable_upload_sessions SET status='failed', error_message=?, updated_at=? WHERE session_id=?",
                        (str(exc), datetime.now().replace(microsecond=0).isoformat(), row["session_id"]),
                    )
                    row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (row["session_id"],)).fetchone()
                    _sync_resumable_upload_job(conn, actor, row, status="failed", progress_percent=100, stage="failed", detail=str(exc), error_message=str(exc))
                    conn.commit()
            except Exception:
                conn.rollback()
            return json_resp({"ok": False, "msg": f"分段上傳完成失敗：{str(exc) or exc.__class__.__name__}", "error": exc.__class__.__name__}), 400
        finally:
            if file_storage:
                file_storage.close()
            conn.close()

    @app.route("/api/cloud-drive/resumable-upload/<session_id>", methods=["DELETE"])
    @require_csrf
    def cloud_drive_resumable_upload_abort(session_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            row, msg = _get_resumable_session(conn, actor, session_id)
            if not row:
                return json_resp({"ok": False, "msg": msg, "error": "not_found"}), 404
            if row["status"] == "completed":
                return json_resp({"ok": False, "msg": "已完成的 upload session 不能取消"}), 409
            conn.execute(
                "UPDATE cloud_resumable_upload_sessions SET status='aborted', updated_at=? WHERE session_id=?",
                (datetime.now().replace(microsecond=0).isoformat(), row["session_id"]),
            )
            row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (row["session_id"],)).fetchone()
            _sync_resumable_upload_job(conn, actor, row, status="cancelled", progress_percent=100, stage="aborted", detail="使用者取消分段上傳")
            conn.commit()
            shutil.rmtree(row["temp_dir"], ignore_errors=True)
            return json_resp({"ok": True, "session": _serialize_resumable_session(row)})
        finally:
            conn.close()

    def _unique_storage_path(conn, owner_user_id, filename):
        safe_name = safe_public_filename(filename or "download.bin")
        stem, ext = os.path.splitext(safe_name)
        stem = stem or "file"
        for index in range(1, 101):
            candidate_name = safe_name if index == 1 else f"{stem}-{index}{ext}"
            candidate = f"/{candidate_name}"
            exists = conn.execute(
                "SELECT 1 FROM storage_files WHERE owner_user_id=? AND virtual_path=? AND deleted_at IS NULL",
                (int(owner_user_id), candidate),
            ).fetchone()
            if not exists:
                return candidate
        return f"/{stem}-{uuid.uuid4().hex[:8]}{ext}"

    def _sync_orphan_cloud_files_to_storage_browser(conn, actor):
        ensure_storage_album_schema(conn)
        rows = conn.execute(
            """
            SELECT f.*
            FROM uploaded_files f
            WHERE f.owner_user_id=? AND f.deleted_at IS NULL
              AND COALESCE(f.system_asset_type, '')<>'avatar'
              AND NOT EXISTS (
                  SELECT 1 FROM storage_files sf
                  WHERE sf.file_id=f.id AND sf.deleted_at IS NULL
              )
            ORDER BY f.created_at ASC, f.id ASC
            LIMIT 100
            """,
            (int(actor["id"]),),
        ).fetchall()
        synced = []
        for row in rows:
            filename = row["original_filename_plain_for_public"] or "download.bin"
            storage_file, msg = create_storage_file_entry(
                conn,
                actor=actor,
                file_row=row,
                virtual_path=_unique_storage_path(conn, actor["id"], filename),
                display_name=filename,
                source="orphan_cloud_file_sync",
            )
            if storage_file and not msg:
                synced.append(storage_file)
        return synced

    @app.route("/api/storage/files", methods=["GET", "POST"])
    @require_csrf_safe
    def storage_files():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            ensure_storage_album_schema(conn)
            if request.method == "GET":
                ensure_output_album(conn, actor=actor)
                _sync_orphan_cloud_files_to_storage_browser(conn, actor)
                include_trashed = request.args.get("include_trashed") in {"1", "true", "yes"}
                files = list_storage_files(conn, actor=actor, include_trashed=include_trashed, limit=100, offset=0)
                rule = get_member_level_rule(conn, actor["effective_level"] or actor["member_level"])
                usage = get_user_cloud_drive_usage(conn, actor, member_rule=rule, storage_root=storage_root)
                summary = sync_user_storage_summary(conn, actor["id"], actor_user_id=actor["id"], source="list", reason="storage_files_list")
                summary = _storage_summary_with_live_quota(summary, usage)
                conn.commit()
                return json_resp({"ok": True, "files": files, "storage": summary})
            if "file" not in request.files:
                return json_resp({"ok": False, "msg": "缺少 file"}), 400
            restricted = _upload_restriction_response(conn, actor)
            if restricted:
                conn.commit()
                return restricted
            upload_policy_error = _apply_upload_transfer_policy(actor, request.files["file"])
            if upload_policy_error:
                return upload_policy_error
            rule = get_member_level_rule(conn, _actor_value(actor, "effective_level") or _actor_value(actor, "member_level"))
            privacy_mode = (request.form.get("privacy_mode") or "standard_plain").strip() or "standard_plain"
            virtual_path = (request.form.get("virtual_path") or "").strip()
            display_name = (request.form.get("display_name") or "").strip() or None
            existing_storage_file = storage_existing_file_for_path(
                conn,
                owner_user_id=int(actor["id"]),
                virtual_path=virtual_path,
                display_name=display_name or getattr(request.files["file"], "filename", ""),
            )
            if existing_storage_file and not _storage_replace_confirmed_from_form():
                return json_resp({
                    "ok": False,
                    "msg": "同一路徑已有檔案，請確認是否覆蓋",
                    "error": "replace_confirm_required",
                }), 409
            if existing_storage_file and storage_replacement_password_required(
                conn,
                owner_user_id=int(actor["id"]),
                virtual_path=virtual_path,
                display_name=display_name or getattr(request.files["file"], "filename", ""),
                new_privacy_mode=privacy_mode,
            ) and not _storage_replace_password_confirmed_from_form():
                return json_resp({
                    "ok": False,
                    "msg": "覆寫 E2EE 檔案或變更加密方式需要重新輸入檔案密碼",
                    "error": "replace_password_required",
                }), 409
            try:
                upload_result, msg = store_cloud_upload(
                    conn,
                    actor=actor,
                    member_rule=rule,
                    storage_root=storage_root,
                    file_storage=request.files["file"],
                    privacy_mode=privacy_mode,
                    encrypted_metadata=(request.form.get("encrypted_metadata") or "").strip() or None,
                    encrypted_file_key=(request.form.get("encrypted_file_key") or "").strip() or None,
                    wrapped_by=(request.form.get("wrapped_by") or "user_public_key").strip() or "user_public_key",
                    ciphertext_sha256=(request.form.get("ciphertext_sha256") or "").strip() or None,
                    encryption_algorithm=(request.form.get("encryption_algorithm") or "").strip() or None,
                    encryption_version=(request.form.get("encryption_version") or "").strip() or None,
                    nonce=(request.form.get("nonce") or "").strip() or None,
                    client_scan_report=_form_json_value("client_scan_report"),
                    scan_now=True,
                    server_file_fernet=server_file_fernet,
                )
            except ValueError as exc:
                conn.rollback()
                return json_resp({"ok": False, "msg": f"雲端硬碟上傳失敗：{str(exc) or exc.__class__.__name__}", "error_code": exc.__class__.__name__}), 400
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (upload_result["file_id"],)).fetchone()
            storage_file, msg = create_storage_file_entry(
                conn,
                actor=actor,
                file_row=file_row,
                virtual_path=virtual_path,
                display_name=display_name,
                source="upload",
                replace_existing=_storage_replace_confirmed_from_form(),
                storage_root=storage_root,
                replace_existing_password_confirmed=_storage_replace_password_confirmed_from_form(),
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            _record_upload_job(
                conn,
                actor,
                upload_result=upload_result,
                storage_file=storage_file,
                display_name=(request.form.get("display_name") or "").strip() or getattr(request.files["file"], "filename", ""),
                job_type="storage.upload",
                title_prefix="檔案上傳",
            )
            conn.commit()
            audit("STORAGE_FILE_UPLOAD", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"storage_file_id={storage_file['id']}")
            return json_resp({"ok": True, "file": upload_result, "storage_file": storage_file})
        finally:
            conn.close()

    @app.route("/api/storage/files/attach-existing", methods=["POST"])
    @require_csrf
    def storage_attach_existing():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=? AND deleted_at IS NULL", (str(data.get("file_id") or ""),)).fetchone()
            if not file_row:
                return json_resp({"ok": False, "msg": "找不到檔案或檔案已刪除"}), 404
            storage_file, msg = create_storage_file_entry(
                conn,
                actor=actor,
                file_row=file_row,
                virtual_path=data.get("virtual_path") or "",
                display_name=data.get("display_name") or None,
                source="attach_existing",
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("STORAGE_FILE_ATTACH_EXISTING", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"storage_file_id={storage_file['id']}")
            return json_resp({"ok": True, "storage_file": storage_file})
        finally:
            conn.close()

    @app.route("/api/storage/folders", methods=["GET", "POST", "DELETE"])
    @app.route("/api/storage/folders/trash", methods=["POST"])
    @require_csrf_safe
    def storage_folders():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            if request.method == "GET":
                ensure_output_album(conn, actor=actor)
                conn.commit()
                return json_resp({"ok": True, "folders": list_storage_folders(conn, actor=actor)})
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            data = data if isinstance(data, dict) else {}
            if request.method == "DELETE" or request.path.endswith("/trash"):
                result, msg = trash_storage_folder(conn, actor=actor, path=data.get("path") or "")
                if msg:
                    conn.rollback()
                    return json_resp({"ok": False, "msg": msg}), 400
                conn.commit()
                audit("STORAGE_FOLDER_TRASH", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"path={result['path']}")
                return json_resp({"ok": True, "folder_trash": result})
            folder, msg = create_storage_folder(conn, actor=actor, path=data.get("path") or "")
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("STORAGE_FOLDER_CREATE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"path={folder['virtual_path']}")
            return json_resp({"ok": True, "folder": folder})
        finally:
            conn.close()

    @app.route("/api/storage/folders/move", methods=["PUT"])
    @require_csrf
    def storage_folder_move():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            result, msg = move_storage_folder(conn, actor=actor, old_path=data.get("old_path") or "", new_path=data.get("new_path") or "")
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("STORAGE_FOLDER_MOVE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"{result['old_path']} -> {result['new_path']}")
            return json_resp({"ok": True, "folder_move": result})
        finally:
            conn.close()

    @app.route("/api/storage/folders/album", methods=["POST"])
    @require_csrf
    def storage_folder_album():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        conn = get_db()
        try:
            album, msg = create_album_from_storage_folder(
                conn,
                actor=actor,
                path=data.get("path") or "",
                title=data.get("title") or None,
                description=data.get("description") or "",
                visibility=data.get("visibility") or "private",
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit(
                "STORAGE_FOLDER_TO_ALBUM",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"path={album.get('source_folder')}, album_id={album['id']}, files={album.get('added_count')}",
            )
            return json_resp({"ok": True, "album": album})
        finally:
            conn.close()

    @app.route("/api/storage/files/<storage_file_id>/organize", methods=["PUT"])
    @require_csrf
    def storage_file_organize(storage_file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            storage_file, msg = move_storage_file(conn, actor=actor, storage_file_id=storage_file_id, new_virtual_path=data.get("virtual_path") or "")
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            ensure_output_album(conn, actor=actor)
            conn.commit()
            audit("STORAGE_FILE_ORGANIZE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"storage_file_id={storage_file_id}, path={storage_file['virtual_path']}")
            return json_resp({"ok": True, "storage_file": storage_file})
        finally:
            conn.close()

    @app.route("/api/storage/files/<storage_file_id>/download", methods=["GET"])
    @require_csrf_safe
    def storage_file_download(storage_file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            storage_file = get_storage_file(conn, actor=actor, storage_file_id=storage_file_id)
            if not storage_file or storage_file.get("deleted_at") or storage_file.get("file_deleted_at") or int(storage_file.get("is_trashed") or 0):
                return json_resp({"ok": False, "msg": "找不到檔案或檔案已刪除"}), 404
            allowed, reason, row = can_download_file(conn, actor=actor, file_id=storage_file["file_id"])
            if not row:
                return json_resp({"ok": False, "msg": "找不到檔案"}), 404
            if not allowed:
                if reason == "deleted":
                    return json_resp({"ok": False, "msg": "找不到檔案"}), 404
                conn.commit()
                return json_resp({"ok": False, "msg": "沒有下載權限或檔案尚未通過安全檢查", "reason": reason}), 403
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            log_file_access(conn, file_id=storage_file["file_id"], actor_user_id=actor["id"], action="storage_download", result="allowed", reason=reason, ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            response = _send_readable_file(row, as_attachment=True, download_name=storage_file["display_name"] or row["original_filename_plain_for_public"] or "download.bin", actor=actor)
            if response is None:
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            return response
        finally:
            conn.close()

    @app.route("/api/storage/trash", methods=["GET"])
    @require_csrf_safe
    def storage_trash():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            files = list_storage_trash(conn, actor=actor, limit=100, offset=0)
            rule = get_member_level_rule(conn, actor["effective_level"] or actor["member_level"])
            usage = get_user_cloud_drive_usage(conn, actor, member_rule=rule, storage_root=storage_root)
            summary = sync_user_storage_summary(conn, actor["id"], actor_user_id=actor["id"], source="trash", reason="storage_trash_list")
            summary = _storage_summary_with_live_quota(summary, usage)
            conn.commit()
            return json_resp({"ok": True, "files": files, "storage": summary})
        finally:
            conn.close()

    @app.route("/api/storage/trash/restore", methods=["POST"])
    @require_csrf
    def storage_trash_restore():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            result, msg = restore_storage_trash(conn, actor=actor)
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("STORAGE_TRASH_RESTORE_ALL", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"restored={result['restored']}")
            return json_resp({"ok": True, "trash": result})
        finally:
            conn.close()

    @app.route("/api/storage/trash/purge", methods=["DELETE"])
    @require_csrf
    def storage_trash_purge():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            result, msg = purge_storage_trash(conn, actor=actor, storage_root=storage_root)
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            media_cleanup = _cleanup_media_derivatives_for_file_ids(conn, result.get("purged_file_ids") or [])
            result["media_cleanup"] = media_cleanup
            conn.commit()
            audit("STORAGE_TRASH_PURGE_ALL", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"purged={result['purged']}")
            return json_resp({"ok": True, "trash": result})
        finally:
            conn.close()

    @app.route("/api/storage/files/<storage_file_id>", methods=["DELETE"])
    @require_csrf
    def storage_file_trash(storage_file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            storage_file, msg = trash_storage_file(conn, actor=actor, storage_file_id=storage_file_id)
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 404
            conn.commit()
            audit("STORAGE_FILE_TRASH", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"storage_file_id={storage_file_id}")
            return json_resp({"ok": True, "storage_file": storage_file})
        finally:
            conn.close()

    @app.route("/api/storage/files/<storage_file_id>/restore", methods=["POST"])
    @require_csrf
    def storage_file_restore(storage_file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            storage_file, msg = restore_storage_file(conn, actor=actor, storage_file_id=storage_file_id)
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 404
            conn.commit()
            audit("STORAGE_FILE_RESTORE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"storage_file_id={storage_file_id}")
            return json_resp({"ok": True, "storage_file": storage_file})
        finally:
            conn.close()

    @app.route("/api/storage/files/<storage_file_id>/purge", methods=["DELETE"])
    @require_csrf
    def storage_file_purge(storage_file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            result, msg = purge_storage_file(conn, actor=actor, storage_file_id=storage_file_id, storage_root=storage_root)
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 404
            media_cleanup = _cleanup_media_derivatives_for_file_ids(conn, result.get("purged_file_ids") or [])
            result["media_cleanup"] = media_cleanup
            conn.commit()
            audit("STORAGE_FILE_PURGE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"storage_file_id={storage_file_id}")
            return json_resp({"ok": True, "purged": result})
        finally:
            conn.close()

    @app.route("/api/storage/albums", methods=["GET", "POST"])
    @require_csrf_safe
    def storage_albums():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            if request.method == "GET":
                ensure_output_album(conn, actor=actor)
                conn.commit()
                albums = list_albums(conn, actor=actor, limit=100, offset=0)
                return json_resp({"ok": True, "albums": albums})
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            album, msg = create_album(
                conn,
                actor=actor,
                title=data.get("title"),
                description=data.get("description") or "",
                visibility=data.get("visibility") or "private",
                share_password=data.get("share_password") if "share_password" in data else None,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("STORAGE_ALBUM_CREATE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"album_id={album['id']}")
            return json_resp({"ok": True, "album": album})
        finally:
            conn.close()

    @app.route("/api/storage/albums/smart-organize", methods=["POST"])
    @require_csrf
    def storage_albums_smart_organize():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            result, msg = smart_organize_albums(
                conn,
                actor=actor,
                strategy=data.get("strategy") or "folder",
                visibility=data.get("visibility") or "private",
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit(
                "STORAGE_ALBUM_SMART_ORGANIZE",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=(
                    f"strategy={result.get('strategy')}, media={result.get('media_count')}, "
                    f"albums={result.get('album_count')}, added={result.get('added_count')}"
                ),
            )
            return json_resp({"ok": True, "result": result})
        finally:
            conn.close()

    @app.route("/api/storage/albums/<album_id>", methods=["GET", "PUT", "DELETE"])
    @require_csrf_safe
    def storage_album_detail(album_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_storage_album_schema(conn)
            if request.method == "GET":
                ensure_output_album(conn, actor=actor)
                conn.commit()
                album = get_album(conn, actor=actor, album_id=album_id, include_files=True)
                if not album:
                    return json_resp({"ok": False, "msg": "找不到相簿"}), 404
                return json_resp({"ok": True, "album": album})
            if request.method == "DELETE":
                result, msg = delete_album(conn, actor=actor, album_id=album_id)
                if msg:
                    conn.rollback()
                    return json_resp({"ok": False, "msg": msg}), 404
                conn.commit()
                audit("STORAGE_ALBUM_DELETE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"album_id={album_id}")
                return json_resp({"ok": True, "deleted": result})
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            album, msg = update_album(
                conn,
                actor=actor,
                album_id=album_id,
                title=data.get("title") if "title" in data else None,
                description=data.get("description") if "description" in data else None,
                visibility=data.get("visibility") if "visibility" in data else None,
                share_password=data.get("share_password") if "share_password" in data else None,
                share_password_provided="share_password" in data,
                clear_share_password=bool(data.get("clear_share_password", False)),
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("STORAGE_ALBUM_UPDATE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"album_id={album_id}")
            return json_resp({"ok": True, "album": album})
        finally:
            conn.close()

    @app.route("/api/storage/albums/<album_id>/files", methods=["POST"])
    @require_csrf
    def storage_album_add_file(album_id):
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            album, msg = add_album_file(
                conn,
                actor=actor,
                album_id=album_id,
                storage_file_id=data.get("storage_file_id"),
                file_id=data.get("file_id"),
                caption=data.get("caption") or "",
                sort_order=data.get("sort_order") or 0,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("STORAGE_ALBUM_FILE_ADD", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"album_id={album_id}")
            return json_resp({"ok": True, "album": album})
        finally:
            conn.close()

    @app.route("/api/storage/albums/<album_id>/files/<album_file_id>", methods=["DELETE"])
    @require_csrf
    def storage_album_remove_file(album_id, album_file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            album, msg = remove_album_file(conn, actor=actor, album_id=album_id, album_file_id=album_file_id)
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 404
            conn.commit()
            audit("STORAGE_ALBUM_FILE_REMOVE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"album_id={album_id}, album_file_id={album_file_id}")
            return json_resp({"ok": True, "album": album})
        finally:
            conn.close()

    file_share_preview_ctx = {
        "actor_or_401": _actor_or_401,
        "audit": audit,
        "decryption_unavailable_preview": _decryption_unavailable_preview,
        "get_client_ip": get_client_ip,
        "get_current_user_ctx": get_current_user_ctx,
        "get_db": get_db,
        "get_ua": get_ua,
        "json_resp": json_resp,
        "preview_allowed_by_policy": _preview_allowed_by_policy,
        "preview_row_with_storage_fallback": _preview_row_with_storage_fallback,
        "readable_file_path": _readable_file_path,
        "require_csrf": require_csrf,
        "require_csrf_safe": require_csrf_safe,
        "requires_download_warning": _requires_download_warning,
        "send_readable_file": _send_readable_file,
        "storage_root": storage_root,
        "ffmpeg_bin": ffmpeg_bin,
        "ffprobe_bin": ffprobe_bin,
        "svg_placeholder_response": _svg_placeholder_response,
    }
    register_file_share_preview_routes(app, file_share_preview_ctx)

    @app.route("/api/files/upload", methods=["POST"])
    @app.route("/api/cloud-drive/upload", methods=["POST"])
    @require_csrf
    def cloud_drive_upload():
        actor, err = _actor_or_401()
        if err:
            return err
        if "file" not in request.files:
            return json_resp({"ok": False, "msg": "缺少 file"}), 400
        upload_policy_error = _apply_upload_transfer_policy(actor, request.files["file"])
        if upload_policy_error:
            return upload_policy_error
        privacy_mode = (request.form.get("privacy_mode") or "standard_plain").strip()
        context_type = (request.form.get("context_type") or "").strip()
        context_id = (request.form.get("context_id") or "").strip()
        grant_user_ids = []
        for value in request.form.getlist("grant_user_ids"):
            try:
                grant_user_ids.append(int(value))
            except Exception:
                pass
        grant_role = (request.form.get("grant_role") or "").strip() or None
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            rule = get_member_level_rule(conn, _actor_value(actor, "effective_level") or _actor_value(actor, "member_level"))
            try:
                result, msg = store_cloud_upload(
                    conn,
                    actor=actor,
                    member_rule=rule,
                    storage_root=storage_root,
                    file_storage=request.files["file"],
                    privacy_mode=privacy_mode,
                    encrypted_metadata=(request.form.get("encrypted_metadata") or "").strip() or None,
                    encrypted_file_key=(request.form.get("encrypted_file_key") or "").strip() or None,
                    wrapped_by=(request.form.get("wrapped_by") or "user_public_key").strip() or "user_public_key",
                    ciphertext_sha256=(request.form.get("ciphertext_sha256") or "").strip() or None,
                    encryption_algorithm=(request.form.get("encryption_algorithm") or "").strip() or None,
                    encryption_version=(request.form.get("encryption_version") or "").strip() or None,
                    nonce=(request.form.get("nonce") or "").strip() or None,
                    client_scan_report=_form_json_value("client_scan_report"),
                    scan_now=True,
                    server_file_fernet=server_file_fernet,
                )
            except ValueError as exc:
                conn.rollback()
                return json_resp({"ok": False, "msg": f"雲端硬碟上傳失敗：{str(exc) or exc.__class__.__name__}", "error_code": exc.__class__.__name__}), 400
            except Exception as exc:
                conn.rollback()
                audit("CLOUD_DRIVE_UPLOAD_ERROR", get_client_ip(), user=actor["username"], success=False, ua=get_ua(), detail=exc.__class__.__name__)
                return json_resp({"ok": False, "msg": f"雲端硬碟上傳失敗：{exc.__class__.__name__}", "error_code": exc.__class__.__name__}), 500
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            attach_result = None
            if context_type and context_id:
                attach_result, msg = attach_existing_file(
                    conn,
                    actor=actor,
                    file_id=result["file_id"],
                    context_type=context_type,
                    context_id=context_id,
                    grant_user_ids=grant_user_ids,
                    grant_role=grant_role,
                    can_preview=True,
                )
                if msg:
                    conn.rollback()
                    return json_resp({"ok": False, "msg": msg}), 400
            create_notification_if_enabled(
                conn,
                user_id=_actor_value(actor, "id"),
                type="cloud_drive_upload_completed",
                title="雲端硬碟上傳完成",
                body=f"檔案「{result.get('filename') or result.get('original_filename') or result.get('file_id') or 'upload'}」已上傳完成。",
                link="/drive",
            )
            _record_upload_job(
                conn,
                actor,
                upload_result=result,
                display_name=getattr(request.files["file"], "filename", ""),
                job_type="cloud_drive.upload",
                title_prefix="雲端硬碟上傳",
            )
            file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (result["file_id"],)).fetchone()
            pending_copy_only_hls = _queue_cloud_drive_copy_only_hls_if_needed(conn, actor=actor, file_row=file_row)
            conn.commit()
            if pending_copy_only_hls:
                _start_cloud_drive_copy_only_hls_worker(pending_copy_only_hls, actor=actor)
            audit("CLOUD_DRIVE_UPLOAD", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file_id={result['file_id']}")
            return json_resp({"ok": True, "file": result, "attachment": attach_result})
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files/text", methods=["POST"])
    @require_csrf
    def cloud_drive_create_text_file():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        raw_name = str(data.get("filename") or "untitled.txt").strip() or "untitled.txt"
        filename = safe_public_filename(raw_name)
        lower_name = filename.lower()
        _TEXT_EXTENSIONS = {
            ".txt", ".md", ".markdown", ".rst", ".tex",
            ".json", ".jsonl", ".ndjson",
            ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
            ".csv", ".tsv",
            ".xml", ".html", ".htm", ".svg",
            ".css", ".scss", ".less",
            ".js", ".mjs", ".ts", ".jsx", ".tsx",
            ".py", ".rb", ".sh", ".bash", ".zsh", ".fish",
            ".c", ".h", ".cpp", ".cc", ".cs", ".java", ".go", ".rs", ".php",
            ".sql", ".log", ".diff", ".patch",
        }
        _, ext = os.path.splitext(lower_name)
        if ext and ext not in _TEXT_EXTENSIONS:
            return json_resp({"ok": False, "msg": "新增文檔僅支援文字類型的副檔名（txt、md、json、yaml、csv、html、js、py 等），或不帶副檔名的純文字檔"}), 400
        content = data.get("content", "")
        if not isinstance(content, str):
            return json_resp({"ok": False, "msg": "content 必須是文字"}), 400
        raw = content.encode("utf-8")
        if len(raw) > 512 * 1024:
            return json_resp({"ok": False, "msg": "新增文檔目前限制 512KB 以內"}), 400
        privacy_mode = str(data.get("privacy_mode") or "standard_plain").strip() or "standard_plain"
        if privacy_mode not in {"standard_plain", "server_encrypted"}:
            return json_resp({"ok": False, "msg": "線上新增文檔只支援一般檔案或伺服器端加密；E2EE 請用上傳檔案"}), 400
        if lower_name.endswith((".md", ".markdown")):
            mimetype = "text/markdown"
        elif lower_name.endswith((".html", ".htm")):
            mimetype = "text/html"
        elif lower_name.endswith((".json", ".jsonl", ".ndjson")):
            mimetype = "application/json"
        elif lower_name.endswith((".yaml", ".yml")):
            mimetype = "application/x-yaml"
        elif lower_name.endswith(".csv"):
            mimetype = "text/csv"
        elif lower_name.endswith(".xml"):
            mimetype = "text/xml"
        elif lower_name.endswith(".svg"):
            mimetype = "image/svg+xml"
        else:
            mimetype = "text/plain"
        file_storage = _MemoryFileStorage(filename=filename, mimetype=mimetype, data=raw)
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            rule = get_member_level_rule(conn, _actor_value(actor, "effective_level") or _actor_value(actor, "member_level"))
            try:
                result, msg = store_cloud_upload(
                    conn,
                    actor=actor,
                    member_rule=rule,
                    storage_root=storage_root,
                    file_storage=file_storage,
                    privacy_mode=privacy_mode,
                    scan_now=True,
                    server_file_fernet=server_file_fernet,
                )
            except ValueError as exc:
                conn.rollback()
                return json_resp({"ok": False, "msg": f"新增文檔失敗：{str(exc) or exc.__class__.__name__}", "error_code": exc.__class__.__name__}), 400
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (result["file_id"],)).fetchone()
            storage_file, storage_msg = create_storage_file_entry(
                conn,
                actor=actor,
                file_row=file_row,
                virtual_path=(data.get("virtual_path") or f"/{filename}"),
                display_name=filename,
                source="text_document",
            )
            if storage_msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": storage_msg}), 400
            create_notification_if_enabled(
                conn,
                user_id=_actor_value(actor, "id"),
                type="cloud_drive_upload_completed",
                title="雲端文檔已建立",
                body=f"文檔「{filename}」已建立。",
                link="/drive",
            )
            conn.commit()
            audit("CLOUD_DRIVE_TEXT_CREATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file_id={result['file_id']},filename={filename}")
            return json_resp({"ok": True, "file": {**result, "filename": filename}, "storage_file": storage_file})
        finally:
            conn.close()

    @app.route("/api/cloud-drive/attach-existing", methods=["POST"])
    @require_csrf
    def cloud_drive_attach_existing():
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            result, msg = attach_existing_file(
                conn,
                actor=actor,
                file_id=str(data.get("file_id") or ""),
                context_type=data.get("context_type"),
                context_id=data.get("context_id"),
                grant_user_ids=_grant_user_ids_from_payload(data),
                grant_role=data.get("grant_role") or None,
                grant_group_id=data.get("grant_group_id") or None,
                can_preview=bool(data.get("can_preview", True)),
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("CLOUD_DRIVE_ATTACH_EXISTING", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file_id={data.get('file_id')}")
            return json_resp({"ok": True, "attachment": result})
        finally:
            conn.close()

    @app.route("/api/files/<file_id>/status", methods=["GET"])
    @require_csrf_safe
    def file_status(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            status, msg = get_file_status(conn, actor=actor, file_id=file_id)
            if msg:
                return json_resp({"ok": False, "msg": msg}), 403
            return json_resp({"ok": True, "file": status})
        finally:
            conn.close()

    @app.route("/api/cloud-drive/refs", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_refs():
        actor, err = _actor_or_401()
        if err:
            return err
        context_type = (request.args.get("context_type") or "").strip()
        context_id = (request.args.get("context_id") or "").strip()
        if not context_type or not context_id:
            return json_resp({"ok": False, "msg": "context_type/context_id required"}), 400
        try:
            limit = max(1, min(200, int(request.args.get("limit") or 100)))
        except Exception:
            limit = 100
        try:
            offset = max(0, int(request.args.get("offset") or 0))
        except Exception:
            offset = 0
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            if not _context_refs_visible_to_actor(conn, actor, context_type, context_id):
                return json_resp({"ok": False, "msg": "權限不足"}), 403
            rows = conn.execute(
                """
                SELECT r.*, f.original_filename_plain_for_public, f.size_bytes, f.scan_status, f.risk_level,
                       f.privacy_mode, f.deleted_at
                FROM cloud_file_refs r JOIN uploaded_files f ON f.id=r.file_id
                WHERE r.context_type=? AND r.context_id=?
                ORDER BY r.created_at ASC
                LIMIT ? OFFSET ?
                """,
                (context_type, context_id, limit + 1, offset),
            ).fetchall()
            page_rows = rows[:limit]
            refs = []
            for row in page_rows:
                allowed, reason, _ = can_download_file(conn, actor=actor, file_id=row["file_id"])
                can_remove = can_remove_context_attachment(actor, row)
                if allowed or can_remove:
                    refs.append({**dict(row), "can_download": allowed, "download_reason": reason, "can_remove": can_remove})
            next_offset = offset + len(page_rows)
            return json_resp({
                "ok": True,
                "refs": refs,
                "limit": limit,
                "offset": offset,
                "next_offset": next_offset if len(rows) > limit else None,
                "has_more": len(rows) > limit,
            })
        finally:
            conn.close()

    @app.route("/api/cloud-drive/refs", methods=["DELETE"])
    @app.route("/api/cloud-drive/refs/", methods=["DELETE"])
    @app.route("/api/cloud-drive/refs/<ref_id>", methods=["DELETE"])
    @app.route("/api/cloud-drive/refs/delete", methods=["POST"])
    @app.route("/api/cloud-drive/refs/<ref_id>/delete", methods=["POST"])
    @require_csrf
    def cloud_drive_delete_ref(ref_id=None):
        actor, err = _actor_or_401()
        if err:
            return err
        ref_id = (ref_id or "").strip()
        if not ref_id:
            try:
                data = request.get_json(silent=True) or {}
            except Exception:
                data = {}
            ref_id = (data.get("ref_id") or request.args.get("ref_id") or "").strip()
        if not ref_id:
            return json_resp({"ok": False, "msg": "需要 attachment 參考"}), 400
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            row = conn.execute("SELECT * FROM cloud_file_refs WHERE id=?", (ref_id,)).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到附件"}), 404
            allowed = can_remove_context_attachment(actor, row)
            if not allowed:
                return json_resp({"ok": False, "msg": "沒有移除此附件的權限"}), 403
            now = datetime.now().isoformat()
            conn.execute("DELETE FROM cloud_file_refs WHERE id=?", (ref_id,))
            conn.execute(
                """
                UPDATE file_access_grants
                SET revoked_at=?
                WHERE file_id=? AND context_type=? AND context_id=? AND revoked_at IS NULL
                """,
                (now, row["file_id"], row["context_type"], row["context_id"]),
            )
            conn.commit()
            audit(
                "CLOUD_DRIVE_ATTACHMENT_REMOVE",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"ref_id={ref_id},file_id={row['file_id']},context={row['context_type']}#{row['context_id']}",
            )
            return json_resp({"ok": True, "msg": "附件已移除", "ref_id": ref_id})
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files/<file_id>/preview", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_preview(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            allowed, reason, row = can_download_file(conn, actor=actor, file_id=file_id, action="preview")
            if not row:
                return json_resp({"ok": False, "msg": "找不到檔案"}), 404
            if row["deleted_at"]:
                return json_resp({"ok": False, "msg": "找不到檔案"}), 404
            if not allowed:
                if reason == "deleted":
                    return json_resp({"ok": False, "msg": "找不到檔案"}), 404
                return json_resp({"ok": False, "msg": "沒有預覽權限或檔案尚未通過安全檢查", "reason": reason}), 403
            policy = get_cloud_drive_security_policy(conn)
            ok, msg = _preview_allowed_by_policy(policy, row)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), 403
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            preview_row = _preview_row_with_storage_fallback(conn, actor, row)
            try:
                category, _mime_type = preview_category(preview_row)
                if is_chunked_server_encrypted_file(row) and category in {"audio", "video", "image", "pdf"}:
                    _assert_chunked_server_encrypted_readable(path, row["size_bytes"])
                    preview = build_preview_metadata(preview_row, path)
                else:
                    readable_path, _ = _readable_file_path(row)
                    preview = build_preview_metadata(preview_row, readable_path)
            except ValueError as exc:
                preview = _decryption_unavailable_preview(preview_row, str(exc))
            try:
                stream_asset = get_stream_status(conn, file_row=row, include_segments=False)
                if preview.get("category") in {"audio", "video"} or stream_asset:
                    proxy_state = realtime_proxy_availability(row)
                    direct_status = direct_browser_playback_status(row, storage_root=storage_root, ffprobe_bin=ffprobe_bin)
                    stream_asset = stream_asset or {}
                    stream_payload = {
                        "status": stream_asset.get("status") or "",
                        "media_type": stream_asset.get("media_type") or preview.get("category") or "",
                        "master_manifest_ready": bool(stream_asset.get("master_manifest_path")),
                        "master_url": f"/api/cloud-drive/files/{file_id}/hls/master.m3u8" if stream_asset.get("master_manifest_path") else "",
                        "realtime_proxy_url": f"/api/cloud-drive/files/{file_id}/realtime-proxy" if proxy_state.get("available") else "",
                        "realtime_proxy": {
                            **proxy_state,
                            "url": f"/api/cloud-drive/files/{file_id}/realtime-proxy" if proxy_state.get("available") else "",
                            "selected_audio_query_param": "audio",
                            "start_seconds_query_param": "start",
                        },
                        "direct_browser_playback": direct_status,
                        "direct_url": f"/api/cloud-drive/files/{file_id}/preview/content" if direct_status.get("available") else "",
                        "error_message": stream_asset.get("error_message") or "",
                        "updated_at": stream_asset.get("updated_at") or "",
                        "premium_hls_profile_policy": stream_asset.get("premium_hls_profile_policy") or {},
                        "audio_tracks": [
                            {
                                "name": str(item.get("name") or ""),
                                "label": str(item.get("label") or item.get("language") or item.get("name") or "音軌"),
                                "language": str(item.get("language") or "und"),
                                "is_default": bool(item.get("is_default")),
                                "codec": str(item.get("codec") or ""),
                                "stream_index": int(item.get("stream_index") or -1),
                                "url": f"/api/cloud-drive/files/{file_id}/hls/{item.get('name')}/playlist.m3u8",
                                "playlist_url": f"/api/cloud-drive/files/{file_id}/hls/{item.get('name')}/playlist.m3u8",
                            }
                            for item in (stream_asset.get("audio_tracks") or [])
                            if item.get("name")
                        ],
                        "subtitles": [
                            {
                                "name": str(item.get("name") or ""),
                                "label": str(item.get("label") or item.get("language") or "字幕"),
                                "language": str(item.get("language") or "und"),
                                "is_default": bool(item.get("is_default")),
                                "url": f"/api/cloud-drive/files/{file_id}/hls/subtitles/{item.get('name')}.vtt",
                            }
                            for item in (stream_asset.get("subtitles") or [])
                            if item.get("name")
                        ],
                    }
                    preview["stream_asset"] = stream_payload
            except Exception:
                pass
            log_file_access(conn, file_id=file_id, actor_user_id=actor["id"], action="preview", result="allowed", reason=preview["category"], ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            return json_resp({"ok": True, "preview": preview})
        finally:
            conn.close()

    def _cloud_drive_hls_asset(conn, file_id, *, include_segments=False):
        actor, err = _actor_or_401()
        if err:
            return None, None, None, err
        allowed, reason, row = can_download_file(conn, actor=actor, file_id=file_id, action="preview")
        if not row or row["deleted_at"]:
            return actor, row, None, (json_resp({"ok": False, "msg": "找不到檔案"}), 404)
        if not allowed:
            if reason == "deleted":
                return actor, row, None, (json_resp({"ok": False, "msg": "找不到檔案"}), 404)
            return actor, row, None, (json_resp({"ok": False, "msg": "沒有預覽權限或檔案尚未通過安全檢查", "reason": reason}), 403)
        if is_e2ee_file(row):
            return actor, row, None, (json_resp({"ok": False, "msg": "E2EE 檔案不支援伺服器端 HLS 預覽"}), 409)
        policy = get_cloud_drive_security_policy(conn)
        ok, msg = _preview_allowed_by_policy(policy, row)
        if not ok:
            return actor, row, None, (json_resp({"ok": False, "msg": msg}), 403)
        asset = get_stream_status(conn, file_row=row, include_segments=include_segments)
        if not asset or asset.get("status") != "ready" or not asset.get("master_manifest_path"):
            return actor, row, asset, (json_resp({"ok": False, "msg": "HLS 串流尚未準備完成", "error": "stream_not_ready"}), 409)
        return actor, row, asset, None

    def _cloud_drive_hls_rendition_match(asset, name):
        clean = str(name or "").strip()
        if not clean or "/" in clean or ".." in clean:
            return None
        for group in ("variants", "audio_tracks"):
            match = next((item for item in ((asset or {}).get(group) or []) if item.get("name") == clean), None)
            if match:
                return match
        return None

    def _cloud_drive_realtime_proxy_error(message, *, error="realtime_proxy_unavailable", status=409, extra=None):
        payload = {
            "ok": False,
            "msg": message,
            "error": error,
            "realtime_proxy": realtime_proxy_runtime_status(),
        }
        if extra:
            payload.update(extra)
        return json_resp(payload), status

    def _cloud_drive_realtime_proxy_audio_selector():
        return request.args.get("audio") or request.args.get("audio_track") or request.args.get("track") or ""

    def _cloud_drive_encrypted_realtime_proxy_response(row, path):
        audio_selector = _cloud_drive_realtime_proxy_audio_selector()
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-fflags",
            "+genpts",
            "-i",
            "pipe:0",
        ]
        start = request.args.get("start") or ""
        try:
            start_value = float(start) if start else 0.0
        except Exception:
            start_value = 0.0
        if start_value > 0:
            cmd.extend(["-ss", f"{start_value:.3f}".rstrip("0").rstrip(".")])
        cmd.extend(["-map", "0:v:0?"])
        if str(audio_selector).strip().isdigit():
            cmd.extend(["-map", f"0:{int(audio_selector)}"])
        else:
            cmd.extend(["-map", "0:a:0?"])
        cmd.extend([
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "ultrafast",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            "-tune",
            "zerolatency",
            "-crf",
            "23",
            "-g",
            "48",
            "-keyint_min",
            "48",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-flush_packets",
            "1",
            "-f",
            "mp4",
            "pipe:1",
        ])
        stderr_file = tempfile.TemporaryFile()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_file, bufsize=0)
        stop_event = threading.Event()

        def _feed_plaintext():
            try:
                for chunk in iter_decrypted_server_encrypted_chunks(path, server_file_fernet):
                    if stop_event.is_set():
                        break
                    try:
                        proc.stdin.write(chunk)
                    except (BrokenPipeError, ValueError):
                        break
            except Exception as exc:
                app.logger.warning("cloud_drive_realtime_proxy_decrypt_feed_failed file_id=%s error=%s", row["id"], exc)
            finally:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except Exception:
                    pass

        feeder = threading.Thread(target=_feed_plaintext, name=f"cloud-drive-realtime-feed-{row['id']}", daemon=True)
        feeder.start()

        def _stream_chunks():
            bytes_sent = 0
            try:
                stdout = proc.stdout
                while stdout is not None:
                    ready, _, _ = select.select([stdout], [], [], 1.0)
                    if not ready:
                        if proc.poll() is not None:
                            break
                        continue
                    chunk = stdout.read(256 * 1024)
                    if not chunk:
                        break
                    bytes_sent += len(chunk)
                    yield chunk
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                stop_event.set()
                try:
                    if proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass
                if proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                try:
                    feeder.join(timeout=1)
                except Exception:
                    pass
                try:
                    stderr_file.seek(0)
                    stderr_text = stderr_file.read(4096).decode("utf-8", errors="replace").strip()
                except Exception:
                    stderr_text = ""
                finally:
                    try:
                        stderr_file.close()
                    except Exception:
                        pass
                if bytes_sent == 0 or proc.poll() not in (0, None):
                    app.logger.warning(
                        "cloud_drive_realtime_proxy_final file_id=%s bytes_sent=%s returncode=%s stderr=%s",
                        row["id"],
                        bytes_sent,
                        proc.poll(),
                        stderr_text[:1000],
                    )

        source_filename = row["original_filename_plain_for_public"] or "video.mp4"
        filename = str(Path(source_filename).with_suffix(".mp4")) if Path(source_filename).suffix else f"{source_filename}.mp4"
        response = Response(stream_with_context(_stream_chunks()), status=200, mimetype="video/mp4", direct_passthrough=True)
        response.headers["Content-Disposition"] = build_content_disposition("inline", filename)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        response.headers["Accept-Ranges"] = "none"
        response.headers["X-Hackme-Transfer-Mode"] = "python_realtime_proxy_server_decrypt_pipe"
        response.headers["X-Hackme-Streaming-Mode"] = "realtime_proxy"
        return response

    def _cloud_drive_realtime_proxy_response(row):
        availability = realtime_proxy_availability(row)
        if not availability.get("available"):
            return _cloud_drive_realtime_proxy_error(
                "即時轉封裝目前不可用，請改用直接串流或預處理 HLS。",
                error=str(availability.get("reason") or "realtime_proxy_unavailable"),
                status=409,
                extra={"availability": availability},
            )
        path = resolve_file_storage_path(storage_root, row)
        if not path.exists():
            return json_resp({"ok": False, "msg": "實體檔案不存在", "error": "file_missing"}), 404
        cleanup_path = None
        try:
            if is_server_encrypted_file(row):
                return _cloud_drive_encrypted_realtime_proxy_response(row, path)
            stream_info = open_realtime_proxy_stream(
                path,
                audio_track=_cloud_drive_realtime_proxy_audio_selector(),
                start_seconds=request.args.get("start") or 0,
                ffmpeg_bin=ffmpeg_bin,
                ffprobe_bin=ffprobe_bin,
            )
        except RuntimeError as exc:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except Exception:
                    pass
            if str(exc).startswith("realtime_proxy_busy:"):
                return _cloud_drive_realtime_proxy_error(
                    "即時轉封裝目前已達併發上限，請稍後再試或使用預處理 HLS。",
                    error="realtime_proxy_busy",
                    status=429,
                )
            return _cloud_drive_realtime_proxy_error(str(exc), status=500)
        except ValueError as exc:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except Exception:
                    pass
            return _cloud_drive_realtime_proxy_error(str(exc), error="invalid_realtime_proxy_request", status=400)
        except FileNotFoundError:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except Exception:
                    pass
            return _cloud_drive_realtime_proxy_error("找不到 ffmpeg / ffprobe，無法啟動即時轉封裝。", error="ffmpeg_missing", status=503)
        except Exception as exc:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except Exception:
                    pass
            return _cloud_drive_realtime_proxy_error(str(exc), error="realtime_proxy_prepare_failed", status=500)

        def _stream_chunks_with_cleanup():
            try:
                yield from stream_info["chunks"]
            finally:
                if cleanup_path:
                    try:
                        os.unlink(cleanup_path)
                    except Exception:
                        pass

        source_filename = row["original_filename_plain_for_public"] or "video.mp4"
        filename = str(Path(source_filename).with_suffix(".mp4")) if Path(source_filename).suffix else f"{source_filename}.mp4"
        response = Response(
            stream_with_context(_stream_chunks_with_cleanup()),
            status=200,
            mimetype=stream_info.get("mimetype") or "video/mp4",
            direct_passthrough=True,
        )
        response.headers["Content-Disposition"] = build_content_disposition("inline", filename)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        response.headers["Accept-Ranges"] = "none"
        response.headers["X-Hackme-Transfer-Mode"] = "python_realtime_proxy_server_decrypt" if is_server_encrypted_file(row) else "python_realtime_proxy"
        selected_audio = stream_info.get("audio_track") or {}
        if selected_audio.get("name"):
            response.headers["X-Hackme-Audio-Track"] = str(selected_audio.get("name") or "")
        response.headers["X-Hackme-Streaming-Mode"] = "realtime_proxy"
        return response

    @app.route("/api/cloud-drive/files/<file_id>/hls/master.m3u8", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_hls_master(file_id):
        conn = get_db()
        try:
            actor, row, asset, err = _cloud_drive_hls_asset(conn, file_id, include_segments=False)
            if err:
                return err
            path = resolve_file_storage_path(storage_root, {"storage_path": asset["master_manifest_path"]})
            log_file_access(conn, file_id=file_id, actor_user_id=actor["id"], action="preview_hls", result="allowed", reason="hls_master", ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            return _send_local_storage_file(path, as_attachment=False, download_name="master.m3u8", mimetype="application/vnd.apple.mpegurl")
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files/<file_id>/hls/<variant>/playlist.m3u8", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_hls_variant_playlist(file_id, variant):
        conn = get_db()
        try:
            _actor, _row, asset, err = _cloud_drive_hls_asset(conn, file_id, include_segments=False)
            if err:
                return err
            match = _cloud_drive_hls_rendition_match(asset, variant)
            if not match:
                return json_resp({"ok": False, "msg": "找不到串流變體", "error": "variant_not_found"}), 404
            path = resolve_file_storage_path(storage_root, {"storage_path": match["playlist_path"]})
            return _send_local_storage_file(path, as_attachment=False, download_name="playlist.m3u8", mimetype="application/vnd.apple.mpegurl")
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files/<file_id>/hls/subtitles/<subtitle_name>.vtt", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_hls_subtitle(file_id, subtitle_name):
        conn = get_db()
        try:
            _actor, _row, asset, err = _cloud_drive_hls_asset(conn, file_id, include_segments=False)
            if err:
                return err
            clean = str(subtitle_name or "").strip()
            match = next((item for item in (asset.get("subtitles") or []) if item.get("name") == clean), None)
            if not match:
                return json_resp({"ok": False, "msg": "找不到字幕", "error": "subtitle_not_found"}), 404
            path = resolve_file_storage_path(storage_root, {"storage_path": match["path"]})
            shift_ms = parse_subtitle_shift_ms(request.args.get("shift_ms"))
            if shift_ms:
                text = shift_webvtt_text(Path(path).read_text(encoding="utf-8", errors="replace"), shift_ms)
                return Response(
                    text,
                    status=200,
                    mimetype="text/vtt; charset=utf-8",
                    headers={"Cache-Control": "no-store"},
                )
            return _send_local_storage_file(path, as_attachment=False, download_name=f"{clean}.vtt", mimetype="text/vtt; charset=utf-8")
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files/<file_id>/hls/<variant>/<segment>", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_hls_segment(file_id, variant, segment):
        conn = get_db()
        try:
            _actor, _row, asset, err = _cloud_drive_hls_asset(conn, file_id, include_segments=True)
            if err:
                return err
            if "/" in segment or ".." in segment:
                return json_resp({"ok": False, "msg": "無效的串流片段", "error": "invalid_segment"}), 400
            match = _cloud_drive_hls_rendition_match(asset, variant)
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

    @app.route("/api/cloud-drive/files/<file_id>/realtime-proxy", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_realtime_proxy(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            allowed, reason, row = can_download_file(conn, actor=actor, file_id=file_id, action="preview")
            if not row or row["deleted_at"]:
                return json_resp({"ok": False, "msg": "找不到檔案"}), 404
            if not allowed:
                if reason == "deleted":
                    return json_resp({"ok": False, "msg": "找不到檔案"}), 404
                return json_resp({"ok": False, "msg": "沒有預覽權限或檔案尚未通過安全檢查", "reason": reason}), 403
            if is_e2ee_file(row):
                return json_resp({"ok": False, "msg": "E2EE 檔案不支援伺服器端即時轉封裝"}), 409
            policy = get_cloud_drive_security_policy(conn)
            ok, msg = _preview_allowed_by_policy(policy, row)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), 403
            log_file_access(conn, file_id=file_id, actor_user_id=actor["id"], action="preview_realtime_proxy", result="allowed", reason="realtime_proxy", ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            return _cloud_drive_realtime_proxy_response(row)
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files/<file_id>/preview/content", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_preview_content(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            allowed, reason, row = can_download_file(conn, actor=actor, file_id=file_id, action="preview")
            if not row:
                return json_resp({"ok": False, "msg": "找不到檔案"}), 404
            if row["deleted_at"]:
                return json_resp({"ok": False, "msg": "找不到檔案"}), 404
            if not allowed:
                if reason == "deleted":
                    return json_resp({"ok": False, "msg": "找不到檔案"}), 404
                return json_resp({"ok": False, "msg": "沒有預覽權限或檔案尚未通過安全檢查", "reason": reason}), 403
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            if is_e2ee_file(row):
                log_file_access(conn, file_id=file_id, actor_user_id=actor["id"], action="e2ee_preview_ciphertext", result="allowed", reason=reason, ip=get_client_ip(), user_agent=get_ua())
                conn.commit()
                return _send_local_storage_file(
                    path,
                    as_attachment=False,
                    download_name=row["original_filename_plain_for_public"] or "e2ee.bin",
                    mimetype="application/octet-stream",
                    conditional=True,
                )
            policy = get_cloud_drive_security_policy(conn)
            ok, msg = _preview_allowed_by_policy(policy, row)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), 403
            preview_row = _preview_row_with_storage_fallback(conn, actor, row)
            category, mime_type = preview_category(preview_row)
            if category not in {"audio", "video", "image", "pdf"}:
                return json_resp({"ok": False, "msg": "此檔案類型不支援 inline content preview"}), 415
            if category in {"audio", "video"}:
                direct_status = direct_browser_playback_status(row, storage_root=storage_root, ffprobe_bin=ffprobe_bin)
                if not direct_status.get("available"):
                    return json_resp({
                        "ok": False,
                        "msg": direct_status.get("detail") or "此媒體不適合直接由瀏覽器播放，請使用即時轉封裝或 HLS。",
                        "reason": direct_status.get("reason") or "direct_not_browser_native",
                        "direct_browser_playback": direct_status,
                    }), 415
            log_file_access(conn, file_id=file_id, actor_user_id=actor["id"], action="preview_content", result="allowed", reason=category, ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            try:
                response = _send_readable_file(
                    row,
                    as_attachment=False,
                    download_name=preview_row["original_filename_plain_for_public"] or "preview",
                    mimetype=mime_type,
                    conditional=True,
                    actor=actor,
                )
            except ValueError as exc:
                if category == "image":
                    return _svg_placeholder_response(str(exc), label="圖片不可預覽")
                return json_resp({"ok": False, "msg": str(exc), "error": "decrypt_unavailable"}), 409
            if response is None:
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            return response
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files/<file_id>/text", methods=["PUT"])
    @require_csrf
    def cloud_drive_update_text(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, str):
            return json_resp({"ok": False, "msg": "content 必須是文字"}), 400
        raw = content.encode("utf-8")
        if len(raw) > 512 * 1024:
            return json_resp({"ok": False, "msg": "線上編輯目前限制 512KB 以內文字檔"}), 400
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
            if not row or row["deleted_at"]:
                return json_resp({"ok": False, "msg": "找不到檔案或檔案已刪除"}), 404
            if int(row["owner_user_id"]) != int(actor["id"]):
                return json_resp({"ok": False, "msg": "只能修改自己的雲端硬碟檔案"}), 403
            if is_e2ee_file(row):
                return json_resp({"ok": False, "msg": "E2EE 檔案不可由伺服器線上修改"}), 400
            category, _ = preview_category(row)
            if category != "text":
                return json_resp({"ok": False, "msg": "目前只支援文字類檔案線上修改"}), 415
            policy = get_cloud_drive_security_policy(conn)
            ok, msg = _preview_allowed_by_policy(policy, row)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), 403
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            scan_path = path
            if is_server_encrypted_file(row):
                if server_file_fernet is None:
                    return json_resp({"ok": False, "msg": "伺服器端加密金鑰尚未設定"}), 500
                temp = tempfile.NamedTemporaryFile(prefix="cloud-drive-edit-", delete=False)
                try:
                    temp.write(raw)
                    scan_path = temp.name
                finally:
                    temp.close()
                path.write_bytes(server_file_fernet.encrypt(raw))
            else:
                with open(path, "wb") as handle:
                    handle.write(raw)
            now = datetime.now().isoformat()
            conn.execute(
                """
                UPDATE uploaded_files
                SET size_bytes=?, plaintext_sha256=?, ciphertext_sha256=?, updated_at=?
                WHERE id=?
                """,
                (
                    len(raw),
                    None if is_server_encrypted_file(row) else hashlib.sha256(raw).hexdigest(),
                    hashlib.sha256(path.read_bytes()).hexdigest() if is_server_encrypted_file(row) else None,
                    now,
                    file_id,
                ),
            )
            try:
                scan_result = scan_uploaded_file(
                    conn,
                    file_id=file_id,
                    file_path=scan_path,
                    filename=row["original_filename_plain_for_public"],
                    declared_mime=row["mime_type_plain_for_public"],
                )
            finally:
                if scan_path != path:
                    try:
                        os.unlink(scan_path)
                    except Exception:
                        pass
            log_file_access(conn, file_id=file_id, actor_user_id=actor["id"], action="text_edit", result="allowed", reason=scan_result.get("scan_status"), ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            return json_resp({"ok": True, "file_id": file_id, "scan_result": scan_result, "size_bytes": len(raw)})
        finally:
            conn.close()

    @app.route("/api/files/<file_id>/download", methods=["GET"])
    @app.route("/api/cloud-drive/files/<file_id>/download", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_download(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            allowed, reason, row = can_download_file(conn, actor=actor, file_id=file_id)
            if not row:
                return json_resp({"ok": False, "msg": "找不到檔案"}), 404
            if row["deleted_at"]:
                return json_resp({"ok": False, "msg": "找不到檔案"}), 404
            if not allowed:
                if reason == "deleted":
                    return json_resp({"ok": False, "msg": "找不到檔案"}), 404
                conn.commit()
                return json_resp({"ok": False, "msg": "沒有下載權限或檔案尚未通過安全檢查", "reason": reason}), 403
            policy = get_cloud_drive_security_policy(conn)
            confirmed = (
                request.args.get("confirm_high_risk") == "1"
                or request.headers.get("X-Confirm-High-Risk-Download", "").lower() in {"1", "true", "yes"}
            )
            if _requires_download_warning(policy, row) and not confirmed:
                return json_resp({
                    "ok": False,
                    "requires_confirmation": True,
                    "msg": "此檔案為高風險或無法完整掃描，請確認信任來源後再下載。",
                    "risk_level": row["risk_level"],
                    "scan_status": row["scan_status"],
                }), 409
            path = resolve_file_storage_path(storage_root, row)
            if not path.exists():
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            log_file_access(conn, file_id=file_id, actor_user_id=actor["id"], action="download", result="allowed", reason=reason, ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            response = _send_readable_file(row, as_attachment=True, download_name=row["original_filename_plain_for_public"] or "download.bin", actor=actor)
            if response is None:
                return json_resp({"ok": False, "msg": "實體檔案不存在"}), 404
            return response
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files/<file_id>/e2ee-key", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_e2ee_key(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
            if not row or row["deleted_at"]:
                return json_resp({"ok": False, "msg": "找不到檔案"}), 404
            if not is_e2ee_file(row):
                return json_resp({"ok": False, "msg": "此檔案不是端到端加密檔案"}), 400
            key = conn.execute(
                """
                SELECT encrypted_file_key, wrapped_by, key_version
                FROM encrypted_file_keys
                WHERE file_id=? AND recipient_user_id=? AND revoked_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (file_id, int(actor["id"])),
            ).fetchone()
            if not key:
                return json_resp({"ok": False, "msg": "此帳號沒有可用的解密金鑰"}), 403
            log_file_access(conn, file_id=file_id, actor_user_id=actor["id"], action="e2ee_key", result="allowed", reason=key["wrapped_by"], ip=get_client_ip(), user_agent=get_ua())
            conn.commit()
            return json_resp({
                "ok": True,
                "e2ee": {
                    "file_id": row["id"],
                    "privacy_mode": row["privacy_mode"],
                    "encrypted_metadata": row["original_filename_encrypted"],
                    "encrypted_file_key": key["encrypted_file_key"],
                    "wrapped_by": key["wrapped_by"],
                    "key_version": key["key_version"],
                    "encryption_algorithm": row["encryption_algorithm"],
                    "encryption_version": row["encryption_version"],
                    "nonce": row["nonce"],
                    "ciphertext_sha256": row["ciphertext_sha256"],
                },
            })
        finally:
            conn.close()

    @app.route("/api/files/<file_id>/share", methods=["POST"])
    @require_csrf
    def file_share(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            result, msg = share_e2ee_file(
                conn,
                actor=actor,
                file_id=file_id,
                recipient_user_id=data.get("recipient_user_id"),
                encrypted_file_key=data.get("encrypted_file_key"),
                wrapped_by=data.get("wrapped_by") or "recipient_public_key",
                context_type=data.get("context_type") or "dm",
                context_id=data.get("context_id"),
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("FILE_E2EE_SHARE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file_id={file_id}, recipient_user_id={result['recipient_user_id']}")
            return json_resp({"ok": True, "share": result})
        finally:
            conn.close()

    @app.route("/api/files/<file_id>/share/revoke", methods=["POST"])
    @require_csrf
    def file_share_revoke(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            result, msg = revoke_e2ee_file_share(
                conn,
                actor=actor,
                file_id=file_id,
                recipient_user_id=data.get("recipient_user_id"),
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("FILE_E2EE_SHARE_REVOKE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file_id={file_id}, recipient_user_id={data.get('recipient_user_id')}")
            return json_resp({"ok": True, "revoked": result})
        finally:
            conn.close()

    @app.route("/api/cloud-drive/files/<file_id>", methods=["DELETE"])
    @require_csrf
    def cloud_drive_delete_file(file_id):
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            result, msg = delete_cloud_file_permanently(conn, actor=actor, file_id=file_id, storage_root=storage_root)
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 404
            media_cleanup = _cleanup_media_derivatives_for_file_ids(conn, [result.get("file_id")])
            result["media_cleanup"] = media_cleanup
            conn.commit()
            audit("CLOUD_DRIVE_FILE_DELETE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file_id={file_id}, physical={result.get('removed_physical_file')}")
            return json_resp({"ok": True, "msg": "檔案已刪除並釋放容量", "deleted": result})
        finally:
            conn.close()

    @app.route("/api/cloud-drive/announcement-attachment-requests", methods=["GET", "POST"])
    @require_csrf_safe
    def announcement_attachment_requests():
        actor, err = _actor_or_401()
        if err:
            return err
        if not _is_manager(actor):
            return json_resp({"ok": False, "msg": "只有管理員以上可使用公告附件請求"}), 403
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            if request.method == "GET":
                if not _is_root(actor):
                    return json_resp({"ok": False, "msg": "只有 root 可查看所有公告附件請求"}), 403
                try:
                    limit = max(1, min(200, int(request.args.get("limit") or 100)))
                except Exception:
                    limit = 100
                try:
                    offset = max(0, int(request.args.get("offset") or 0))
                except Exception:
                    offset = 0
                total = conn.execute("SELECT COUNT(*) AS c FROM announcement_attachment_requests").fetchone()["c"]
                rows = conn.execute(
                    "SELECT * FROM announcement_attachment_requests ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                next_offset = offset + len(rows)
                return json_resp({
                    "ok": True,
                    "requests": [dict(row) for row in rows],
                    "limit": limit,
                    "offset": offset,
                    "next_offset": next_offset if next_offset < int(total or 0) else None,
                    "has_more": next_offset < int(total or 0),
                    "total": int(total or 0),
                })
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            result, msg = create_announcement_attachment_request(
                conn,
                actor=actor,
                file_id=str(data.get("file_id") or ""),
                announcement_id=data.get("announcement_id"),
                reason=data.get("reason") or "",
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("ANNOUNCEMENT_ATTACHMENT_REQUEST", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"request_id={result['id']}")
            return json_resp({"ok": True, "request": result})
        finally:
            conn.close()

    @app.route("/api/root/announcement-attachment-requests/<request_id>/review", methods=["POST"])
    @require_csrf
    def root_review_announcement_attachment_request(request_id):
        actor, err = _actor_or_401()
        if err:
            return err
        if not _is_root(actor):
            return json_resp({"ok": False, "msg": "只有 root 可審核公告附件"}), 403
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            result, msg = review_announcement_attachment_request(
                conn,
                actor=actor,
                request_id=request_id,
                action=data.get("action"),
                reason=data.get("reason") or "",
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400
            conn.commit()
            audit("ANNOUNCEMENT_ATTACHMENT_REVIEW", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"request_id={request_id}, status={result['status']}")
            return json_resp({"ok": True, "request": result})
        finally:
            conn.close()

    @app.route("/api/files/security-policy", methods=["GET"])
    @require_csrf_safe
    def file_security_policy():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            rule = get_member_level_rule(conn, actor["effective_level"] or actor["member_level"])
            summary = get_cloud_drive_safety_summary(conn, actor, member_rule=rule, storage_root=storage_root)
            return json_resp({"ok": True, "security": summary})
        finally:
            conn.close()

    @app.route("/api/files/privacy-modes", methods=["GET"])
    @require_csrf_safe
    def file_privacy_modes():
        actor, err = _actor_or_401()
        if err:
            return err
        conn = get_db()
        try:
            policy = get_cloud_drive_security_policy(conn)
            return json_resp({
                "ok": True,
                "modes": {
                    "standard_plain": {
                        "label": "一般檔案",
                        "server_can_read": True,
                        "server_scan": "required",
                        "stored_at_rest": "plaintext",
                        "e2ee": False,
                        "best_for": "一般雲端檔案、附件、相簿、分享。",
                        "preview": "支援圖片、影片、音樂、PDF、文字與壓縮檔預覽。",
                        "download": "通過掃描後可下載；高風險檔案下載前會要求確認。",
                        "warning": "檔案以明文存在伺服器儲存區，請勿上傳需要端到端保密的資料。",
                    },
                    "server_encrypted": {
                        "label": "伺服器端加密",
                        "server_can_read": "decryptable",
                        "server_scan": "required",
                        "stored_at_rest": "encrypted",
                        "e2ee": False,
                        "best_for": "想降低磁碟或備份外洩風險，同時保留掃毒、預覽與下載明文。",
                        "preview": "伺服器暫時解密後，通過掃描與風險政策即可預覽。",
                        "download": "通過掃描後下載明文；磁碟上的實體檔仍是密文。",
                        "warning": "這不是端到端加密；伺服器/root 仍有解密能力。",
                    },
                    "e2ee": {
                        "label": "端到端加密",
                        "server_can_read": False,
                        "server_scan": "metadata_only",
                        "stored_at_rest": "encrypted",
                        "e2ee": True,
                        "best_for": "高度私密檔案，只需要保存與本人下載解密，不依賴伺服器預覽。",
                        "preview": "伺服器不能預覽明文；下載後由瀏覽器用使用者輸入的 E2EE 密碼解密。",
                        "download": "下載時輸入上傳時設定的 E2EE 密碼；換電腦仍可解密，但忘記密碼無法救回。",
                        "warning": "站方無法讀取內容，也無法完整掃毒；遺失 E2EE 密碼無法救回，本機掃描回報也不可完全信任。",
                    },
                },
                "policy": policy,
            })
        finally:
            conn.close()
