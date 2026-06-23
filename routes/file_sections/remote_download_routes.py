import json
import mimetypes
import os
import shutil
import tempfile
import threading
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

from flask import request

from services.storage.cloud_drive import ensure_cloud_drive_attachment_schema, store_cloud_upload
from services.storage.remote_downloads import DownloadedFile
from services.storage.storage_albums import create_storage_file_entry, ensure_storage_album_schema
from services.job_center import TERMINAL_JOB_STATUSES, ensure_job_center_schema
from services.system.notifications import create_notification_if_enabled
from services.security.upload_security import get_user_cloud_drive_usage, safe_public_filename


def register_file_remote_download_routes(app, ctx):
    get_db = ctx["get_db"]
    get_member_level_rule = ctx["get_member_level_rule"]
    get_client_ip = ctx["get_client_ip"]
    get_ua = ctx["get_ua"]
    audit = ctx["audit"]
    json_resp = ctx["json_resp"]
    require_csrf = ctx["require_csrf"]
    require_csrf_safe = ctx["require_csrf_safe"]
    storage_root = ctx["storage_root"]
    server_file_fernet = ctx["server_file_fernet"]

    actor_or_401 = ctx["actor_or_401"]
    actor_value = ctx["actor_value"]
    is_manager = ctx["is_manager"]
    actor_transfer_policy = ctx["actor_transfer_policy"]

    DownloadedFileStorage = ctx["DownloadedFileStorage"]
    task_snapshot = ctx["task_snapshot"]
    get_remote_download_task = ctx["get_remote_download_task"]
    get_remote_download_task_for_status = ctx.get("get_remote_download_task_for_status", get_remote_download_task)
    list_remote_download_tasks_for_actor = ctx["list_remote_download_tasks_for_actor"]
    cleanup_stale_remote_download_tasks_locked = ctx["cleanup_stale_remote_download_tasks_locked"]
    control_remote_download_task = ctx["control_remote_download_task"]
    run_remote_download_task = ctx["run_remote_download_task"]
    remote_download_storage_path = ctx["remote_download_storage_path"]
    sync_remote_download_job = ctx.get("sync_remote_download_job", lambda *args, **kwargs: None)
    queue_cloud_drive_copy_only_hls_if_needed = ctx.get("queue_cloud_drive_copy_only_hls_if_needed", lambda *args, **kwargs: None)
    start_cloud_drive_copy_only_hls_worker = ctx.get("start_cloud_drive_copy_only_hls_worker", lambda *args, **kwargs: False)

    remote_download_tasks = ctx["remote_download_tasks"]
    remote_download_tasks_lock = ctx["remote_download_tasks_lock"]

    download_remote_url = ctx["download_remote_url"]
    download_torrent_file_with_aria2 = ctx["download_torrent_file_with_aria2"]
    download_torrent_url_with_aria2 = ctx["download_torrent_url_with_aria2"]
    remote_download_capabilities = ctx["remote_download_capabilities"]
    validate_remote_url = ctx["validate_remote_url"]
    validate_torrent_file_trackers = ctx.get("validate_torrent_file_trackers")

    def _availability_score(source_type, url="", tracker_report=None):
        source_type = str(source_type or "direct")
        if source_type == "direct":
            return 900, "direct link"
        if source_type == "torrent_file":
            trackers = tracker_report.get("trackers") if isinstance(tracker_report, dict) else []
            count = len([item for item in trackers if str(item or "").strip()])
            return min(850, 160 + count * 25), f"torrent trackers={count}"
        if source_type == "magnet":
            parsed = urllib.parse.urlparse(str(url or ""))
            params = urllib.parse.parse_qs(parsed.query)
            trackers = [item for item in params.get("tr", []) if str(item or "").strip()]
            score = 90 + min(12, len(set(trackers))) * 25
            if params.get("dn"):
                score += 20
            if params.get("xl"):
                score += 20
            return min(780, score), f"magnet trackers={len(set(trackers))}"
        if source_type == "torrent_url":
            return 130, "torrent url"
        return 80, source_type
    RemoteDownloadError = ctx["RemoteDownloadError"]

    def _actor_snapshot(actor):
        try:
            return dict(actor)
        except Exception:
            return {
                "id": actor_value(actor, "id"),
                "username": actor_value(actor, "username"),
                "role": actor_value(actor, "role"),
                "member_level": actor_value(actor, "member_level"),
                "effective_level": actor_value(actor, "effective_level"),
            }

    def _remote_download_job_for_task_id(task_id):
        source_ref = f"remote_download:{str(task_id)}"
        conn = get_db()
        try:
            ensure_job_center_schema(conn)
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
            return dict(row) if row else None
        finally:
            conn.close()

    def _json_dict(raw):
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _remote_download_staging_task_dir(owner_user_id, task_id):
        base = Path(os.environ.get("HACKME_BT_DOWNLOAD_STAGING_DIR") or Path(storage_root) / "_runtime" / "remote-downloads" / "transmission-staging").resolve()
        task_dir = (base / f"user-{int(owner_user_id)}" / f"task-{str(task_id)}").resolve()
        try:
            task_dir.relative_to(base)
        except ValueError:
            return None
        return task_dir

    def _find_recoverable_staging_file(owner_user_id, task_id, expected_size=None):
        task_dir = _remote_download_staging_task_dir(owner_user_id, task_id)
        if not task_dir or not task_dir.exists() or not task_dir.is_dir():
            return None
        candidates = []
        exact_candidates = []
        for path in task_dir.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            if name.endswith((".part", ".tmp", ".aria2")):
                continue
            if path.with_name(path.name + ".aria2").exists():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= 0:
                continue
            candidates.append((size, path))
            if expected_size and int(expected_size or 0) > 0 and size == int(expected_size):
                exact_candidates.append((size, path))
        if not candidates:
            return None
        pool = exact_candidates or candidates
        pool.sort(key=lambda item: item[0], reverse=True)
        return pool[0][1]

    def _task_recoverable_from_job(job, metadata=None):
        metadata = metadata if isinstance(metadata, dict) else _json_dict((job or {}).get("metadata_json"))
        if str((job or {}).get("status") or "") not in {"running", "queued"}:
            return False
        task_id = str(metadata.get("task_id") or "").strip()
        owner_user_id = int((job or {}).get("owner_user_id") or 0)
        if not task_id or not owner_user_id:
            return False
        if metadata.get("storage_file_id") or metadata.get("file_id"):
            return False
        return _find_recoverable_staging_file(owner_user_id, task_id, metadata.get("total_bytes")) is not None

    def _recover_interrupted_remote_download(task_id, actor):
        task_id = str(task_id or "").strip()
        if not task_id:
            return {"ok": False, "msg": "缺少下載任務 ID"}, 400
        job = _remote_download_job_for_task_id(task_id)
        actor_id = int(actor_value(actor, "id") or 0)
        if job:
            owner_user_id = int(job.get("owner_user_id") or 0)
            if owner_user_id != actor_id:
                return {"ok": False, "msg": "沒有下載任務權限"}, 403
            metadata = _json_dict(job.get("metadata_json"))
            result = _json_dict(job.get("result_json"))
            merged = {**metadata, **result}
        else:
            owner_user_id = actor_id
            staging_file = _find_recoverable_staging_file(owner_user_id, task_id)
            if not staging_file:
                return {"ok": False, "msg": "找不到下載任務或可恢復的 staging 檔案"}, 404
            merged = {
                "task_id": task_id,
                "source_type": "torrent_url",
                "filename": staging_file.name,
                "loaded_bytes": staging_file.stat().st_size,
                "total_bytes": staging_file.stat().st_size,
            }
            job = {"created_at": None}
        if merged.get("storage_file_id"):
            task = {
                "id": task_id,
                "kind": "remote_download",
                "status": "completed",
                "phase": "completed",
                "filename": merged.get("filename") or "",
                "owner_user_id": owner_user_id,
                "loaded_bytes": merged.get("file_size_bytes") or merged.get("loaded_bytes"),
                "total_bytes": merged.get("file_size_bytes") or merged.get("total_bytes"),
                "progress_percent": 100,
                "msg": "遠端下載已保存到雲端硬碟",
                "file": merged.get("file") if isinstance(merged.get("file"), dict) else {},
                "storage_file": merged.get("storage_file") if isinstance(merged.get("storage_file"), dict) else {"id": merged.get("storage_file_id")},
                "updated_at": datetime.now().isoformat(),
            }
            sync_remote_download_job(task, force_event=True)
            return {"ok": True, "task": task_snapshot(task), "msg": "遠端下載已保存到雲端硬碟"}, 200
        staging_file = _find_recoverable_staging_file(owner_user_id, task_id, merged.get("total_bytes"))
        if not staging_file:
            return {"ok": False, "msg": "找不到可恢復的 staging 檔案"}, 404
        filename = safe_public_filename(merged.get("filename") or staging_file.name or "remote-download.bin")
        if filename.startswith("_") and staging_file.name:
            filename = safe_public_filename(staging_file.name)
        mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        privacy_mode = str(merged.get("privacy_mode") or "standard_plain").strip() or "standard_plain"
        virtual_path = str(merged.get("virtual_path") or "").strip()
        downloaded = DownloadedFile(path=str(staging_file), filename=filename, mimetype=mimetype, cleanup_dir=None)
        file_storage = DownloadedFileStorage(downloaded)
        conn = get_db()
        try:
            ensure_cloud_drive_attachment_schema(conn)
            ensure_storage_album_schema(conn)
            rule = get_member_level_rule(conn, actor_value(actor, "effective_level") or actor_value(actor, "member_level"))
            upload_result, msg = store_cloud_upload(
                conn,
                actor=actor,
                member_rule=rule,
                storage_root=storage_root,
                file_storage=file_storage,
                privacy_mode=privacy_mode,
                scan_now=True,
                server_file_fernet=server_file_fernet,
            )
            if msg:
                conn.rollback()
                return {"ok": False, "msg": msg}, 400
            file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (upload_result["file_id"],)).fetchone()
            storage_path = remote_download_storage_path(filename, virtual_path)
            storage_file, storage_msg = create_storage_file_entry(
                conn,
                actor=actor,
                file_row=file_row,
                virtual_path=storage_path,
                display_name=filename,
                source="remote_download_recovery",
            )
            if storage_msg:
                conn.rollback()
                return {"ok": False, "msg": storage_msg}, 400
            create_notification_if_enabled(
                conn,
                user_id=owner_user_id,
                type="cloud_drive_remote_download_completed",
                title="BT 下載已恢復完成",
                body=f"BT 下載「{filename}」已從中斷的 staging 檔恢復並保存到你的雲端硬碟。",
                link="/drive",
            )
            pending_copy_only_hls = queue_cloud_drive_copy_only_hls_if_needed(conn, actor=actor, file_row=file_row)
            conn.commit()
            if pending_copy_only_hls:
                start_cloud_drive_copy_only_hls_worker(
                    pending_copy_only_hls,
                    actor=actor,
                    ip=get_client_ip(),
                    ua=get_ua(),
                )
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            return {"ok": False, "msg": f"遠端下載恢復失敗：{exc.__class__.__name__}"}, 500
        finally:
            file_storage.close()
            conn.close()
        task_dir = _remote_download_staging_task_dir(owner_user_id, task_id)
        if task_dir:
            shutil.rmtree(str(task_dir), ignore_errors=True)
        task = {
            "id": task_id,
            "kind": "remote_download",
            "source_type": merged.get("source_type") or "torrent_url",
            "status": "completed",
            "phase": "completed",
            "filename": filename,
            "url": merged.get("url") or "",
            "owner_user_id": owner_user_id,
            "loaded_bytes": upload_result.get("size_bytes"),
            "total_bytes": upload_result.get("size_bytes"),
            "progress_percent": 100,
            "speed_bytes_per_sec": 0,
            "msg": "遠端下載已從中斷狀態恢復並保存到雲端硬碟",
            "file": {**upload_result, "filename": filename},
            "storage_file": storage_file,
            "updated_at": datetime.now().isoformat(),
            "created_at": job.get("created_at"),
        }
        sync_remote_download_job(task, force_event=True)
        try:
            audit("CLOUD_DRIVE_REMOTE_DOWNLOAD_RECOVERED", get_client_ip(), user=actor_value(actor, "username"), success=True, ua=get_ua(), detail=f"task_id={task_id},file_id={upload_result['file_id']}")
        except Exception:
            pass
        return {"ok": True, "task": task_snapshot(task), "file": {**upload_result, "filename": filename}, "storage_file": storage_file, "msg": "遠端下載已恢復並保存到雲端硬碟"}, 200

    def _dismiss_persisted_remote_download_job(task_id, actor, *, allow_non_terminal=False):
        source_ref = f"remote_download:{str(task_id)}"
        actor_id = int(actor_value(actor, "id") or 0)
        conn = get_db()
        try:
            ensure_job_center_schema(conn)
            job = conn.execute(
                """
                SELECT job_uuid, owner_user_id, status
                FROM job_center_jobs
                WHERE source_module='cloud_drive_remote_download'
                  AND source_ref=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (source_ref,),
            ).fetchone()
            if not job:
                conn.commit()
                return {"ok": True, "removed": False}
            if int(job["owner_user_id"] or 0) != actor_id:
                conn.rollback()
                return {"ok": False, "msg": "沒有下載任務權限", "status": 403}
            if not allow_non_terminal and str(job["status"] or "") not in TERMINAL_JOB_STATUSES:
                conn.rollback()
                return {"ok": False, "msg": "下載任務仍在進行，不能移除紀錄", "status": 409}
            conn.execute("DELETE FROM job_center_events WHERE job_uuid=?", (job["job_uuid"],))
            conn.execute("DELETE FROM job_center_jobs WHERE job_uuid=?", (job["job_uuid"],))
            conn.commit()
            return {"ok": True, "removed": True}
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @app.route("/api/cloud-drive/remote-download/capabilities", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_remote_download_capabilities():
        actor, err = actor_or_401()
        if err:
            return err
        return json_resp({"ok": True, "capabilities": remote_download_capabilities()})

    @app.route("/api/cloud-drive/remote-download/tasks", methods=["POST"])
    @require_csrf
    def cloud_drive_remote_download_task_create():
        actor, err = actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        url = str(data.get("url") or "").strip()
        if not url:
            return json_resp({"ok": False, "msg": "請輸入下載網址"}), 400
        try:
            parsed_remote = validate_remote_url(url)
        except RemoteDownloadError as exc:
            return json_resp({"ok": False, "msg": str(exc)}), 400
        download_mode = str(data.get("download_mode") or "direct").strip().lower()
        if download_mode not in {"direct", "bt"}:
            return json_resp({"ok": False, "msg": "下載模式不正確"}), 400
        if download_mode == "bt":
            if parsed_remote["kind"] == "magnet":
                source_type = "magnet"
            elif parsed_remote["kind"] == "torrent_url":
                source_type = "torrent_url"
            else:
                return json_resp({"ok": False, "msg": "BT/torrent 按鈕只接受 magnet link 或 .torrent URL"}), 400
        else:
            if parsed_remote["kind"] == "magnet":
                return json_resp({"ok": False, "msg": "Direct link 不接受 magnet link，請使用 BT/torrent 按鈕"}), 400
            source_type = "direct"
        privacy_mode = str(data.get("privacy_mode") or "standard_plain").strip() or "standard_plain"
        virtual_path = str(data.get("virtual_path") or "").strip()
        availability_score, availability_hint = _availability_score(source_type, url)
        task_id = uuid.uuid4().hex
        now = datetime.now().isoformat()
        task = {
            "id": task_id,
            "kind": "remote_download",
            "source_type": source_type,
            "status": "queued",
            "phase": "queued",
            "pause_requested": False,
            "cancel_requested": False,
            "control_action": "",
            "filename": "",
            "url": url,
            "torrent_filename": "",
            "torrent_path": "",
            "torrent_cleanup_dir": "",
            "owner_user_id": int(actor_value(actor, "id")),
            "actor": _actor_snapshot(actor),
            "privacy_mode": privacy_mode,
            "virtual_path": virtual_path,
            "timeout_seconds": 1800 if source_type in {"magnet", "torrent_url"} else 120,
            "loaded_bytes": 0,
            "total_bytes": None,
            "progress_percent": 0,
            "speed_bytes_per_sec": 0,
            "msg": "已加入下載 worker 佇列",
            "error": "",
            "file": None,
            "storage_file": None,
            "availability_score": availability_score,
            "availability_hint": availability_hint,
            "ip": get_client_ip(),
            "ua": get_ua(),
            "created_at": now,
            "updated_at": now,
        }
        with remote_download_tasks_lock:
            remote_download_tasks[task_id] = task
        sync_remote_download_job(dict(task))
        worker = threading.Thread(target=run_remote_download_task, args=(task_id,), daemon=True)
        worker.start()
        return json_resp({"ok": True, "task": task_snapshot(task)}, 202)

    @app.route("/api/cloud-drive/remote-download/tasks", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_remote_download_task_list():
        actor, err = actor_or_401()
        if err:
            return err
        return json_resp({"ok": True, "tasks": list_remote_download_tasks_for_actor(actor)})

    @app.route("/api/cloud-drive/remote-download/torrent-tasks", methods=["POST"])
    @require_csrf
    def cloud_drive_remote_download_torrent_task_create():
        actor, err = actor_or_401()
        if err:
            return err
        uploaded = request.files.get("torrent_file") or request.files.get("torrent")
        if not uploaded or not uploaded.filename:
            return json_resp({"ok": False, "msg": "請上傳 .torrent BT 種子檔"}), 400
        filename = safe_public_filename(uploaded.filename)
        if not filename.lower().endswith(".torrent"):
            return json_resp({"ok": False, "msg": "只接受 .torrent BT 種子檔"}), 400

        tmpdir = tempfile.mkdtemp(prefix="hackme_torrent_")
        torrent_path = os.path.join(tmpdir, filename)
        try:
            uploaded.save(torrent_path)
            try:
                torrent_size = os.path.getsize(torrent_path)
            except OSError:
                torrent_size = 0
            if torrent_size <= 0:
                shutil.rmtree(tmpdir, ignore_errors=True)
                return json_resp({"ok": False, "msg": "BT 種子檔是空的"}), 400
            if torrent_size > 2 * 1024 * 1024:
                shutil.rmtree(tmpdir, ignore_errors=True)
                return json_resp({"ok": False, "msg": "BT 種子檔太大，請上傳 2MB 以內的 .torrent"}), 400
            tracker_report = None
            if validate_torrent_file_trackers:
                try:
                    tracker_report = validate_torrent_file_trackers(torrent_path)
                except RemoteDownloadError as exc:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    return json_resp({"ok": False, "msg": str(exc)}), 400
            privacy_mode = str(request.form.get("privacy_mode") or "standard_plain").strip() or "standard_plain"
            virtual_path = str(request.form.get("virtual_path") or "").strip()
            availability_score, availability_hint = _availability_score("torrent_file", f"BT 檔案：{filename}", tracker_report)
            task_id = uuid.uuid4().hex
            now = datetime.now().isoformat()
            task = {
                "id": task_id,
                "kind": "remote_download",
                "source_type": "torrent_file",
                "status": "queued",
                "phase": "queued",
                "pause_requested": False,
                "cancel_requested": False,
                "control_action": "",
                "filename": filename,
                "url": f"BT 檔案：{filename}",
                "torrent_filename": filename,
                "torrent_path": torrent_path,
                "torrent_cleanup_dir": tmpdir,
                "owner_user_id": int(actor_value(actor, "id")),
                "actor": _actor_snapshot(actor),
                "privacy_mode": privacy_mode,
                "virtual_path": virtual_path,
                "timeout_seconds": 1800,
                "loaded_bytes": 0,
                "total_bytes": None,
                "progress_percent": 0,
                "speed_bytes_per_sec": 0,
                "msg": "BT 種子檔已加入下載佇列",
                "error": "",
                "file": None,
                "storage_file": None,
                "availability_score": availability_score,
                "availability_hint": availability_hint,
                "ip": get_client_ip(),
                "ua": get_ua(),
                "created_at": now,
                "updated_at": now,
            }
            with remote_download_tasks_lock:
                remote_download_tasks[task_id] = task
            sync_remote_download_job(dict(task))
            worker = threading.Thread(target=run_remote_download_task, args=(task_id,), daemon=True)
            worker.start()
            return json_resp({"ok": True, "task": task_snapshot(task)}, 202)
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

    @app.route("/api/cloud-drive/remote-download/tasks/<task_id>", methods=["GET"])
    @require_csrf_safe
    def cloud_drive_remote_download_task_status(task_id):
        actor, err = actor_or_401()
        if err:
            return err
        task = get_remote_download_task_for_status(str(task_id))
        if not task:
            actor_id = int(actor_value(actor, "id") or 0)
            staging_file = _find_recoverable_staging_file(actor_id, str(task_id))
            if staging_file:
                filename = safe_public_filename(staging_file.name or "remote-download.bin")
                return json_resp({"ok": True, "task": {
                    "id": str(task_id),
                    "kind": "remote_download",
                    "status": "running",
                    "phase": "interrupted_saving",
                    "filename": filename,
                    "owner_user_id": actor_id,
                    "loaded_bytes": staging_file.stat().st_size,
                    "total_bytes": staging_file.stat().st_size,
                    "progress_percent": 100,
                    "speed_bytes_per_sec": 0,
                    "msg": "下載已完成但保存被中斷，可恢復保存",
                    "recoverable": True,
                    "updated_at": datetime.now().isoformat(),
                }})
            return json_resp({"ok": False, "msg": "找不到下載任務"}), 404
        if int(task.get("owner_user_id") or 0) != int(actor_value(actor, "id")):
            return json_resp({"ok": False, "msg": "沒有下載任務權限"}), 403
        snapshot = task_snapshot(task)
        if not snapshot.get("recoverable"):
            job = _remote_download_job_for_task_id(str(task_id))
            if job and int(job.get("owner_user_id") or 0) == int(actor_value(actor, "id")):
                metadata = _json_dict(job.get("metadata_json"))
                snapshot["recoverable"] = _task_recoverable_from_job(job, metadata)
        return json_resp({"ok": True, "task": snapshot})

    @app.route("/api/cloud-drive/remote-download/tasks/<task_id>/recover", methods=["POST"])
    @require_csrf
    def cloud_drive_remote_download_task_recover(task_id):
        actor, err = actor_or_401()
        if err:
            return err
        payload, status_code = _recover_interrupted_remote_download(task_id, actor)
        return json_resp(payload), status_code

    @app.route("/api/cloud-drive/remote-download/tasks/<task_id>/pause", methods=["POST"])
    @require_csrf
    def cloud_drive_remote_download_task_pause(task_id):
        actor, err = actor_or_401()
        if err:
            return err
        payload, status_code = control_remote_download_task(task_id, actor, "pause")
        return json_resp(payload), status_code

    @app.route("/api/cloud-drive/remote-download/tasks/<task_id>/resume", methods=["POST"])
    @require_csrf
    def cloud_drive_remote_download_task_resume(task_id):
        actor, err = actor_or_401()
        if err:
            return err
        payload, status_code = control_remote_download_task(task_id, actor, "resume")
        return json_resp(payload), status_code

    @app.route("/api/cloud-drive/remote-download/tasks/<task_id>/cancel", methods=["POST"])
    @require_csrf
    def cloud_drive_remote_download_task_cancel(task_id):
        actor, err = actor_or_401()
        if err:
            return err
        payload, status_code = control_remote_download_task(task_id, actor, "cancel")
        return json_resp(payload), status_code

    @app.route("/api/cloud-drive/remote-download/tasks/<task_id>", methods=["DELETE"])
    @require_csrf
    def cloud_drive_remote_download_task_dismiss(task_id):
        actor, err = actor_or_401()
        if err:
            return err
        task_id = str(task_id)
        with remote_download_tasks_lock:
            cleanup_stale_remote_download_tasks_locked()
            task = remote_download_tasks.get(task_id)
            if not task:
                persisted = _dismiss_persisted_remote_download_job(task_id, actor)
                if not persisted.get("ok"):
                    return json_resp({"ok": False, "msg": persisted.get("msg") or "下載任務移除失敗"}), int(persisted.get("status") or 400)
                return json_resp({"ok": True, "removed": bool(persisted.get("removed"))})
            if int(task.get("owner_user_id") or 0) != int(actor_value(actor, "id")):
                return json_resp({"ok": False, "msg": "沒有下載任務權限"}), 403
            if task.get("status") in {"queued", "running"}:
                return json_resp({"ok": False, "msg": "下載任務仍在進行，不能移除紀錄"}), 409
            cleanup_dir = task.get("torrent_cleanup_dir")
            remote_download_tasks.pop(task_id, None)
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        _dismiss_persisted_remote_download_job(task_id, actor, allow_non_terminal=True)
        return json_resp({"ok": True, "removed": True})

    @app.route("/api/cloud-drive/remote-download", methods=["POST"])
    @require_csrf
    def cloud_drive_remote_download():
        actor, err = actor_or_401()
        if err:
            return err
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        data = data if isinstance(data, dict) else {}
        url = str(data.get("url") or "").strip()
        privacy_mode = str(data.get("privacy_mode") or "standard_plain").strip() or "standard_plain"
        virtual_path = str(data.get("virtual_path") or "").strip()
        timeout_seconds = 120

        conn = None
        downloaded = None
        file_storage = None
        try:
            conn = get_db()
            ensure_cloud_drive_attachment_schema(conn)
            ensure_storage_album_schema(conn)
            rule = get_member_level_rule(conn, actor_value(actor, "effective_level") or actor_value(actor, "member_level"))
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

            remote_rate_kb_per_sec = int(actor_transfer_policy(actor).get("download_kb_per_sec") or 0)
            downloaded = download_remote_url(
                url,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
                rate_limit_kb_per_sec=remote_rate_kb_per_sec or None,
                treat_torrent_as_bt=False,
            )
            file_storage = DownloadedFileStorage(downloaded)
            conn = get_db()
            ensure_cloud_drive_attachment_schema(conn)
            ensure_storage_album_schema(conn)
            upload_result, msg = store_cloud_upload(
                conn,
                actor=actor,
                member_rule=rule,
                storage_root=storage_root,
                file_storage=file_storage,
                privacy_mode=privacy_mode,
                scan_now=True,
                server_file_fernet=server_file_fernet,
            )
            if msg:
                conn.rollback()
                return json_resp({"ok": False, "msg": msg}), 400

            file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (upload_result["file_id"],)).fetchone()
            storage_path = remote_download_storage_path(downloaded.filename, virtual_path)
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
                return json_resp({"ok": False, "msg": msg}), 400
            source_label = "BT 下載" if url.startswith("magnet:?") or url.lower().split("?", 1)[0].endswith(".torrent") else "遠端下載"
            create_notification_if_enabled(
                conn,
                user_id=actor_value(actor, "id"),
                type="cloud_drive_remote_download_completed",
                title=f"{source_label}已完成",
                body=f"{source_label}「{downloaded.filename}」已保存到你的雲端硬碟。",
                link="/drive",
            )
            conn.commit()
            audit("CLOUD_DRIVE_REMOTE_DOWNLOAD", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"file_id={upload_result['file_id']}")
            payload = {"ok": True, "msg": "遠端下載已保存到雲端硬碟", "file": {**upload_result, "filename": downloaded.filename}}
            if storage_file:
                payload["storage_file"] = storage_file
            return json_resp(payload)
        except RemoteDownloadError as exc:
            if conn:
                conn.rollback()
            return json_resp({"ok": False, "msg": str(exc)}), 400
        finally:
            if file_storage:
                file_storage.close()
            if downloaded and downloaded.cleanup_dir:
                shutil.rmtree(downloaded.cleanup_dir, ignore_errors=True)
            if conn:
                conn.close()
