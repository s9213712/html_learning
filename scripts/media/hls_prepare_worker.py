#!/usr/bin/env python3
"""Build HLS derivatives outside the Flask server process."""

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

try:
    import resource
except Exception:  # pragma: no cover - unavailable on some platforms
    resource = None

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX fallback
    fcntl = None

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.media.streaming import ensure_media_stream_schema, prepare_stream_asset  # noqa: E402
from services.media.videos import ensure_video_schema  # noqa: E402
from services.job_center import (  # noqa: E402
    add_job_event,
    create_job,
    get_job_by_source,
    update_job,
)
from services.server.database import get_db as open_db  # noqa: E402
from services.system.notifications import create_notification_if_enabled  # noqa: E402

HLS_JOB_SOURCE_MODULE = "media_hls_prepare"
DEFAULT_HLS_SERIALIZE_MIN_BYTES = 256 * 1024 * 1024
DEFAULT_HLS_MAX_CONCURRENT = 1


def _now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def _utc_now_iso():
    """Return Job Center timestamps in the same naive-UTC convention it uses."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def _build_fernet(secret):
    secret = str(secret or "").strip()
    if not secret:
        return None
    try:
        return Fernet(secret.encode("utf-8"))
    except Exception:
        derived = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        return Fernet(derived)


def _load_server_file_fernet(*, key_path="", env_name="SERVER_FILE_ENCRYPTION_KEY"):
    secret = ""
    if key_path:
        try:
            secret = Path(key_path).read_text(encoding="utf-8").strip()
        except Exception:
            secret = ""
    if not secret and env_name:
        secret = os.environ.get(env_name, "").strip()
    return _build_fernet(secret)


def _load_stream_file(conn, file_id):
    row = conn.execute(
        "SELECT * FROM uploaded_files WHERE id=? AND deleted_at IS NULL",
        (str(file_id or ""),),
    ).fetchone()
    if not row:
        raise ValueError("找不到影音檔案")
    return row


def _media_runtime_dir(storage_root):
    try:
        return Path(storage_root).resolve().parent
    except Exception:
        return Path.cwd()


def _lower_worker_priority():
    try:
        nice_value = int(os.environ.get("HACKME_MEDIA_HLS_WORKER_NICE", "10"))
    except Exception:
        nice_value = 10
    if nice_value <= 0 or not hasattr(os, "nice"):
        return
    try:
        os.nice(min(19, nice_value))
    except Exception:
        return


def _bounded_env_int(name, default, *, min_value=0, max_value=10 * 1024 * 1024 * 1024):
    try:
        value = int(float(str(os.environ.get(name, default)).strip()))
    except Exception:
        value = int(default)
    return max(int(min_value), min(int(max_value), value))


def _bounded_env_float(name, default, *, min_value=0.0, max_value=60.0):
    try:
        value = float(str(os.environ.get(name, default)).strip())
    except Exception:
        value = float(default)
    return max(float(min_value), min(float(max_value), value))


def _hls_worker_slot_required(file_row):
    if str(os.environ.get("HACKME_MEDIA_HLS_SERIALIZE_ALL") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    threshold = _bounded_env_int(
        "HACKME_MEDIA_HLS_SERIALIZE_MIN_BYTES",
        DEFAULT_HLS_SERIALIZE_MIN_BYTES,
        min_value=0,
    )
    if threshold <= 0:
        return False
    try:
        size_bytes = int(_row_value(file_row, "size_bytes", 0) or 0)
    except Exception:
        size_bytes = 0
    return size_bytes >= threshold


def _hls_worker_max_concurrent():
    return _bounded_env_int(
        "HACKME_MEDIA_HLS_MAX_CONCURRENT",
        DEFAULT_HLS_MAX_CONCURRENT,
        min_value=1,
        max_value=16,
    )


def _hls_debug_hold_slot_seconds():
    return _bounded_env_float(
        "HACKME_MEDIA_HLS_DEBUG_HOLD_SLOT_SECONDS",
        0.0,
        min_value=0.0,
        max_value=30.0,
    )


def _hls_worker_lock_dir(args):
    raw = str(os.environ.get("HACKME_MEDIA_HLS_LOCK_DIR") or "").strip()
    if raw:
        return Path(raw)
    return _media_runtime_dir(args.storage_root) / "locks" / "hls_prepare"


def _hls_worker_slot_path(lock_dir, index):
    return Path(lock_dir) / f"media_hls_prepare_slot_{int(index):02d}.lock"


def _compact_hls_slot_state(state):
    if not isinstance(state, dict):
        return {}
    compact = {}
    for key in ("scope", "active", "limit", "free", "slot_index", "wait_ms", "lock_dir"):
        if key in state:
            compact[key] = state.get(key)
    return compact


def _try_acquire_hls_worker_slot(args):
    if fcntl is None:
        return None, {
            "scope": "process",
            "active": 0,
            "limit": 0,
            "free": None,
            "reason": "fcntl_unavailable",
        }
    limit = _hls_worker_max_concurrent()
    lock_dir = _hls_worker_lock_dir(args)
    lock_dir.mkdir(parents=True, exist_ok=True)
    locked = 0
    errors = 0
    for index in range(limit):
        handle = _hls_worker_slot_path(lock_dir, index).open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            locked += 1
            handle.close()
            continue
        except Exception:
            errors += 1
            handle.close()
            continue
        active = locked + 1
        return handle, {
            "scope": "global",
            "active": active,
            "limit": limit,
            "free": max(0, limit - active),
            "slot_index": index,
            "lock_dir": str(lock_dir),
            "errors": errors,
        }
    return None, {
        "scope": "global",
        "active": limit,
        "limit": limit,
        "free": 0,
        "slot_index": None,
        "lock_dir": str(lock_dir),
        "errors": errors,
    }


def _acquire_hls_worker_slot(conn, *, args, file_row):
    if fcntl is None:
        return None
    started = time.monotonic()
    last_update = 0.0
    while True:
        lock_handle, state = _try_acquire_hls_worker_slot(args)
        state["wait_ms"] = round((time.monotonic() - started) * 1000, 3)
        if lock_handle is not None:
            if state["wait_ms"] > 0:
                _sync_hls_platform_job(
                    conn,
                    args=args,
                    file_row=file_row,
                    status="running",
                    progress_percent=14,
                    stage="worker_slot_acquired",
                    stage_detail=f"Premium HLS 轉檔資源已取得：slot {int(state.get('slot_index') or 0) + 1}/{int(state.get('limit') or 1)}。",
                    slot_state=state,
                )
                conn.commit()
            return lock_handle, state
        now = time.monotonic()
        if not last_update or now - last_update >= 2.0:
            last_update = now
            active = int(state.get("active") or 0)
            limit = int(state.get("limit") or 0)
            detail = f"Premium HLS 外部程序已排隊，等待轉檔資源：目前 {active}/{limit} 個 slot 使用中。"
            _sync_hls_platform_job(
                conn,
                args=args,
                file_row=file_row,
                status="running",
                progress_percent=12,
                stage="waiting_worker_slot",
                stage_detail=detail,
                slot_state=state,
            )
            conn.commit()
        time.sleep(0.5)


def _release_hls_worker_slot(slot_lock):
    if not slot_lock or fcntl is None:
        return
    if isinstance(slot_lock, tuple):
        lock_handle = slot_lock[0]
    else:
        lock_handle = slot_lock
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_handle.close()
    except Exception:
        pass


def _worker_resource_summary(started_at=None):
    summary = {
        "pid": os.getpid(),
    }
    if started_at:
        summary["wall_ms"] = round((time.monotonic() - float(started_at)) * 1000, 3)
    if resource is None:
        return summary
    try:
        self_usage = resource.getrusage(resource.RUSAGE_SELF)
        child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        self_cpu = float(self_usage.ru_utime or 0.0) + float(self_usage.ru_stime or 0.0)
        child_cpu = float(child_usage.ru_utime or 0.0) + float(child_usage.ru_stime or 0.0)
        summary.update({
            "self_cpu_seconds": round(self_cpu, 4),
            "child_cpu_seconds": round(child_cpu, 4),
            "total_cpu_seconds": round(self_cpu + child_cpu, 4),
            "self_max_rss_kb": int(getattr(self_usage, "ru_maxrss", 0) or 0),
            "child_max_rss_kb": int(getattr(child_usage, "ru_maxrss", 0) or 0),
        })
    except Exception:
        return summary
    return summary


def _hls_result_summary(asset, slot_state=None, worker_metrics=None):
    result = _asset_result_summary(asset)
    compact_slot = _compact_hls_slot_state(slot_state)
    if compact_slot:
        result["worker_slot"] = compact_slot
    if isinstance(worker_metrics, dict) and worker_metrics:
        result["worker_metrics"] = worker_metrics
    return result


def _hls_job_source_ref(file_id):
    return f"media_stream:{str(file_id or '').strip()}"


def _row_value(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _sync_hls_platform_job(
    conn,
    *,
    args,
    file_row=None,
    status="running",
    progress_percent=0,
    stage="processing",
    stage_detail="",
    error_message="",
    result=None,
    slot_state=None,
):
    try:
        file_id = str(args.file_id or _row_value(file_row, "id", ""))
        source_ref = _hls_job_source_ref(file_id)
        existing = get_job_by_source(conn, HLS_JOB_SOURCE_MODULE, source_ref)
        owner_user_id = int(args.owner_user_id or _row_value(file_row, "owner_user_id", 0) or 0)
        title = str(args.title or _row_value(file_row, "original_filename_plain_for_public", "") or "影音").strip() or "影音"
        metadata = {
            "file_id": file_id,
            "video_id": int(args.video_id or 0),
            "privacy_mode": str(_row_value(file_row, "privacy_mode", "") or ""),
            "source_process": "hls_prepare_worker",
            "service_tier": "premium_hls",
            "worker_slot": _compact_hls_slot_state(slot_state),
        }
        if existing:
            updates = {
                "status": status,
                "progress_percent": progress_percent,
                "stage": stage,
                "stage_detail": stage_detail,
                "metadata_json": metadata,
            }
            if status == "running" and not existing.get("started_at"):
                updates["started_at"] = _utc_now_iso()
            if status in {"succeeded", "failed", "cancelled", "expired"}:
                updates["finished_at"] = _utc_now_iso()
            if result is not None:
                updates["result_json"] = result
            if error_message:
                updates["error_message"] = error_message
                updates["error_stage"] = stage
            elif status != "failed":
                updates["error_code"] = None
                updates["error_message"] = None
                updates["error_stage"] = None
            job = update_job(conn, existing["job_uuid"], defer_progress=False, flush=True, **updates)
            add_job_event(
                conn,
                job["job_uuid"],
                event_type="failed" if status == "failed" else "progress",
                stage=stage,
                message=stage_detail or error_message or "HLS 任務狀態更新",
                progress_percent=progress_percent,
                payload=metadata,
                defer_progress=False,
                flush=True,
            )
            return job
        return create_job(
            conn,
            owner_user_id=owner_user_id or None,
            created_by_user_id=owner_user_id or None,
            job_type="video.hls.prepare",
            title=f"HLS 處理：{title[:96]}",
            description="影音 HLS 衍生檔建立、轉封裝與可播放狀態追蹤",
            source_module=HLS_JOB_SOURCE_MODULE,
            source_ref=source_ref,
            status=status,
            progress_percent=progress_percent,
            stage=stage,
            stage_detail=stage_detail,
            metadata=metadata,
        )
    except Exception:
        return None


def _asset_result_summary(asset):
    if not isinstance(asset, dict):
        return {}
    variants = asset.get("variants") if isinstance(asset.get("variants"), list) else []
    summarized_variants = []
    segment_count = 0
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        segments = variant.get("segments") if isinstance(variant.get("segments"), list) else []
        segment_count += len(segments)
        summarized_variants.append({
            "name": variant.get("name"),
            "width": variant.get("width"),
            "height": variant.get("height"),
            "bitrate": variant.get("bitrate"),
            "codec": variant.get("codec"),
            "playlist_path": variant.get("playlist_path"),
            "segment_count": len(segments),
        })
    return {
        "id": asset.get("id"),
        "uploaded_file_id": asset.get("uploaded_file_id"),
        "status": asset.get("status"),
        "media_type": asset.get("media_type"),
        "source_mode": asset.get("source_mode"),
        "source_size_bytes": asset.get("source_size_bytes"),
        "duration_seconds": asset.get("duration_seconds"),
        "master_manifest_path": asset.get("master_manifest_path"),
        "variants_count": len(summarized_variants),
        "segments_count": segment_count,
        "variants": summarized_variants,
    }


def _mark_video_ready(conn, *, file_id, video_id=0, duration_seconds=0):
    now = _now_iso()
    duration = int(float(duration_seconds or 0))
    if int(video_id or 0) > 0:
        conn.execute(
            """
            UPDATE videos
            SET status='ready', duration_seconds=?, updated_at=?
            WHERE id=? AND cloud_file_id=? AND deleted_at IS NULL
            """,
            (duration, now, int(video_id), str(file_id)),
        )
        return
    conn.execute(
        """
        UPDATE videos
        SET status='ready', duration_seconds=?, updated_at=?
        WHERE cloud_file_id=? AND deleted_at IS NULL
        """,
        (duration, now, str(file_id)),
    )


def _notify(conn, *, owner_user_id=0, video_id=0, title="", ok=True, error_message=""):
    if int(owner_user_id or 0) <= 0:
        return
    safe_title = str(title or "影音").strip() or "影音"
    if ok:
        create_notification_if_enabled(
            conn,
            user_id=int(owner_user_id),
            type="video_hls_ready",
            title="影音處理完成",
            body=f"「{safe_title}」已完成 HLS 處理，現在可以在影音頁播放與分享。",
            link=f"/#videos/{int(video_id)}" if int(video_id or 0) > 0 else "/#videos",
        )
        return
    create_notification_if_enabled(
        conn,
        user_id=int(owner_user_id),
        type="video_hls_failed",
        title="影音處理失敗",
        body=f"「{safe_title}」的 HLS 處理失敗：{str(error_message or '請稍後重試')[:220]}",
        link="/#shares",
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Prepare one media file as an HLS derivative asset.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--video-id", type=int, default=0)
    parser.add_argument("--owner-user-id", type=int, default=0)
    parser.add_argument("--title", default="")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument("--server-file-key-path", default="")
    parser.add_argument("--server-file-key-env", default="SERVER_FILE_ENCRYPTION_KEY")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    worker_started_at = time.monotonic()
    _lower_worker_priority()
    conn = open_db(args.db_path)
    slot_lock = None
    slot_state = {}
    progress_state = {"last_at": 0.0, "last_percent": -1}

    def progress(percent, stage="processing", detail=""):
        try:
            value = max(0, min(99, int(percent or 0)))
        except Exception:
            value = 0
        now = time.monotonic()
        if value < 100 and value < int(progress_state["last_percent"]) + 2 and now - float(progress_state["last_at"]) < 2.0:
            return
        progress_state["last_at"] = now
        progress_state["last_percent"] = value
        _sync_hls_platform_job(
            conn,
            args=args,
            file_row=file_row,
            status="running",
            progress_percent=value,
            stage=stage,
            stage_detail=detail or "HLS 外部程序正在處理；你可以先做別的事，完成後會通知。",
            slot_state=slot_state,
        )
        conn.commit()

    try:
        ensure_video_schema(conn)
        ensure_media_stream_schema(conn)
        file_row = _load_stream_file(conn, args.file_id)
        _sync_hls_platform_job(
            conn,
            args=args,
            file_row=file_row,
            status="running",
            progress_percent=10,
            stage="worker_started",
            stage_detail="HLS 外部程序已啟動，正在等待轉檔資源。",
        )
        conn.commit()
        if _hls_worker_slot_required(file_row):
            acquired = _acquire_hls_worker_slot(conn, args=args, file_row=file_row)
            if isinstance(acquired, tuple):
                slot_lock, slot_state = acquired
            else:
                slot_lock = acquired
                slot_state = {}
            stage_detail = "HLS 外部程序正在轉封裝與產生播放清單。"
        else:
            stage_detail = "小型影音不等待大型 HLS 任務，已直接開始轉封裝與產生播放清單。"
        debug_hold_seconds = _hls_debug_hold_slot_seconds() if slot_state else 0.0
        if debug_hold_seconds > 0:
            _sync_hls_platform_job(
                conn,
                args=args,
                file_row=file_row,
                status="running",
                progress_percent=14,
                stage="worker_slot_hold",
                stage_detail=f"Premium HLS QA 正在持有轉檔 slot {debug_hold_seconds:.1f}s，用於驗證排隊行為。",
                slot_state=slot_state,
            )
            conn.commit()
            time.sleep(debug_hold_seconds)
        _sync_hls_platform_job(
            conn,
            args=args,
            file_row=file_row,
            status="running",
            progress_percent=15,
            stage="transcoding",
            stage_detail=stage_detail,
            slot_state=slot_state,
        )
        conn.commit()
        server_file_fernet = _load_server_file_fernet(
            key_path=args.server_file_key_path,
            env_name=args.server_file_key_env,
        )
        asset = prepare_stream_asset(
            conn,
            file_row=file_row,
            storage_root=args.storage_root,
            server_file_fernet=server_file_fernet,
            ffprobe_bin=args.ffprobe_bin,
            ffmpeg_bin=args.ffmpeg_bin,
            progress_callback=progress,
        )
        if asset and asset.get("status") == "ready":
            _mark_video_ready(
                conn,
                file_id=args.file_id,
                video_id=args.video_id,
                duration_seconds=asset.get("duration_seconds", 0),
            )
            _notify(
                conn,
                owner_user_id=args.owner_user_id or int(file_row["owner_user_id"]),
                video_id=args.video_id,
                title=args.title,
                ok=True,
            )
            ready_result = _hls_result_summary(asset, slot_state, _worker_resource_summary(worker_started_at))
            _sync_hls_platform_job(
                conn,
                args=args,
                file_row=file_row,
                status="succeeded",
                progress_percent=100,
                stage="ready",
                stage_detail="HLS 處理完成，影音可以播放與分享。",
                result=ready_result,
                slot_state=slot_state,
            )
            conn.commit()
            print(json.dumps({"ok": True, "asset": ready_result}, ensure_ascii=False), flush=True)
            return 0
        failed_result = _hls_result_summary(asset or {}, slot_state, _worker_resource_summary(worker_started_at))
        _sync_hls_platform_job(
            conn,
            args=args,
            file_row=file_row,
            status="failed",
            progress_percent=100,
            stage="not_ready",
            stage_detail="HLS 處理結束但未產生可播放串流。",
            error_message="stream_not_ready",
            result=failed_result,
            slot_state=slot_state,
        )
        conn.commit()
        print(json.dumps({"ok": False, "asset": failed_result, "error": "stream_not_ready"}, ensure_ascii=False), flush=True)
        return 1
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 2
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        try:
            try:
                file_row = _load_stream_file(conn, args.file_id)
            except Exception:
                file_row = None
            _sync_hls_platform_job(
                conn,
                args=args,
                file_row=file_row,
                status="failed",
                progress_percent=100,
                stage="failed",
                stage_detail=f"HLS 處理失敗：{message[:220]}",
                error_message=message,
                slot_state=slot_state,
            )
            _notify(
                conn,
                owner_user_id=args.owner_user_id,
                video_id=args.video_id,
                title=args.title,
                ok=False,
                error_message=message,
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 1
    finally:
        _release_hls_worker_slot(slot_lock)
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
