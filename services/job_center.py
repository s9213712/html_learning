from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from services.core.progress_backend import (
    DEFAULT_PROGRESS_TTL_SECONDS,
    get_progress_backend,
    progress_backend_status,
)


JOB_STATUSES = {
    "queued",
    "running",
    "waiting_external",
    "paused",
    "succeeded",
    "failed",
    "cancelled",
    "retry_wait",
    "expired",
}

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
DEFAULT_SUCCEEDED_JOB_RETENTION_SECONDS = 60
DEFAULT_FAILED_JOB_RETENTION_SECONDS = 5 * 60
_JOB_PROGRESS_NAMESPACE = "job_center:jobs"
_JOB_PROGRESS_EVENT_NAMESPACE = "job_center:events"
_JOB_PROGRESS_FLUSH_STATE = {}
_JOB_PROGRESS_EVENT_FLUSH_STATE = {}
_JOB_PROGRESS_LOCK = threading.RLock()
_JOB_CENTER_SCHEMA_READY = set()
_JOB_CENTER_SCHEMA_LOCK = threading.RLock()


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _env_float(name, default, *, minimum=0.0, maximum=3600.0):
    try:
        value = float(str(os.environ.get(name, default)).strip())
    except Exception:
        value = float(default)
    return max(float(minimum), min(float(maximum), value))


def _env_int(name, default, *, minimum=0, maximum=31 * 24 * 60 * 60):
    try:
        value = int(float(str(os.environ.get(name, default)).strip()))
    except Exception:
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def _progress_buffer_enabled():
    return _env_bool("HACKME_JOB_PROGRESS_BUFFER_ENABLED", True)


def _progress_flush_interval_seconds():
    return _env_float("HACKME_JOB_PROGRESS_FLUSH_INTERVAL_SECONDS", 1.5, minimum=0.0, maximum=60.0)


def _progress_event_flush_interval_seconds():
    return _env_float("HACKME_JOB_PROGRESS_EVENT_FLUSH_INTERVAL_SECONDS", 5.0, minimum=0.0, maximum=300.0)


def job_progress_buffer_status():
    payload = progress_backend_status()
    payload.update(
        {
            "enabled": _progress_buffer_enabled(),
            "flush_interval_seconds": _progress_flush_interval_seconds(),
            "event_flush_interval_seconds": _progress_event_flush_interval_seconds(),
        }
    )
    return payload


def reset_job_progress_buffer_for_tests():
    with _JOB_PROGRESS_LOCK:
        _JOB_PROGRESS_FLUSH_STATE.clear()
        _JOB_PROGRESS_EVENT_FLUSH_STATE.clear()
    with _JOB_CENTER_SCHEMA_LOCK:
        _JOB_CENTER_SCHEMA_READY.clear()


def _job_notification_payload(job):
    status = str((job or {}).get("status") or "")
    title = str((job or {}).get("title") or "平台任務")
    if status == "succeeded":
        return {
            "type": "job_succeeded",
            "title": "任務已完成",
            "body": f"{title} 已完成。",
            "severity": "success",
        }
    if status == "failed":
        error = str((job or {}).get("error_message") or "").strip()
        return {
            "type": "job_failed",
            "title": "任務失敗",
            "body": f"{title} 失敗" + (f"：{error}" if error else "。"),
            "severity": "error",
        }
    if status == "cancelled":
        return {
            "type": "job_cancelled",
            "title": "任務已取消",
            "body": f"{title} 已取消。",
            "severity": "warning",
        }
    if status == "expired":
        return {
            "type": "job_expired",
            "title": "任務已逾時",
            "body": f"{title} 已逾時。",
            "severity": "warning",
        }
    return None


def _maybe_create_terminal_notification(conn, job, *, previous_status=None):
    if not job:
        return False
    status = str(job.get("status") or "")
    if status not in TERMINAL_JOB_STATUSES or previous_status == status:
        return False
    user_id = job.get("owner_user_id")
    if not user_id:
        return False
    payload = _job_notification_payload(job)
    if not payload:
        return False
    try:
        from services.system.notifications import (
            create_notification,
            ensure_notifications_schema,
            notification_type_is_muted,
            notifications_enabled,
        )

        if not notifications_enabled(default=True) or notification_type_is_muted(payload["type"]):
            return False
        ensure_notifications_schema(conn)
        exists = conn.execute(
            """
            SELECT id FROM notifications
            WHERE user_id=? AND type=? AND source_module='job_center' AND source_ref=?
            LIMIT 1
            """,
            (int(user_id), payload["type"], str(job.get("job_uuid") or "")),
        ).fetchone()
        if exists:
            return False
        create_notification(
            conn,
            user_id=int(user_id),
            type=payload["type"],
            title=payload["title"],
            body=payload["body"],
            link="#jobs",
            severity=payload["severity"],
            audience="user",
            source_module="job_center",
            source_ref=str(job.get("job_uuid") or ""),
            metadata_json=_json({
                "job_uuid": job.get("job_uuid"),
                "job_type": job.get("job_type"),
                "source_module": job.get("source_module"),
                "source_ref": job.get("source_ref"),
                "status": status,
            }),
        )
        return True
    except Exception:
        # Job Center is auxiliary. Notification failure must not break the job
        # lifecycle or background workers.
        return False


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def _json(data):
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)


def _json_load(raw):
    try:
        value = json.loads(raw) if raw else {}
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _parse_utc_timestamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_terminal_age_timestamp(value, *, now=None):
    parsed = _parse_utc_timestamp(value)
    if not parsed:
        return None
    now = now or datetime.now(timezone.utc)
    if parsed <= now:
        return parsed
    # Some older media/job integrations wrote local wall-clock timestamps
    # without an offset. If treating the value as UTC puts it far in the future,
    # retry as the host's local timezone so terminal cleanup does not stall.
    text = str(value or "").strip()
    if not text or text.endswith("Z") or "+" in text[10:] or "-" in text[10:]:
        return parsed
    if (parsed - now).total_seconds() <= 5 * 60:
        return parsed
    candidate_tzs = [datetime.now().astimezone().tzinfo, timezone(timedelta(hours=8))]
    for candidate_tz in candidate_tzs:
        try:
            local_parsed = datetime.fromisoformat(text).replace(tzinfo=candidate_tz).astimezone(timezone.utc)
            if local_parsed <= now + timedelta(seconds=60):
                return local_parsed
        except Exception:
            continue
    return parsed


def _progress_ttl_seconds():
    return max(
        60,
        _safe_int(os.environ.get("HACKME_JOB_PROGRESS_CACHE_TTL_SECONDS"), DEFAULT_PROGRESS_TTL_SECONDS),
    )


def _normalize_progress(value):
    return max(0, min(100, _safe_int(value, 0)))


def _event_is_terminal(event_type, progress_percent=None):
    event_type = str(event_type or "").strip().lower()
    if event_type in {"created", "completed", "succeeded", "failed", "cancelled", "expired", "retry_requested"}:
        return True
    return progress_percent is not None and _normalize_progress(progress_percent) >= 100


def _cache_put(namespace, key, payload):
    if not _progress_buffer_enabled():
        return False
    try:
        return get_progress_backend().put(namespace, str(key), payload or {}, ttl_seconds=_progress_ttl_seconds())
    except Exception:
        return False


def _cache_get(namespace, key):
    if not _progress_buffer_enabled():
        return None
    try:
        return get_progress_backend().get(namespace, str(key))
    except Exception:
        return None


def _cache_delete(namespace, key):
    try:
        return get_progress_backend().delete(namespace, str(key))
    except Exception:
        return False


def _deserialize_cached_job_payload(payload):
    if not isinstance(payload, dict):
        return None
    job_uuid = str(payload.get("job_uuid") or "").strip()
    if not job_uuid:
        return None
    data = dict(payload)
    data["progress_percent"] = _normalize_progress(data.get("progress_percent", 0))
    data["retry_count"] = _safe_int(data.get("retry_count"), 0)
    data["max_retries"] = _safe_int(data.get("max_retries"), 0)
    data["cancellable"] = bool(data.get("cancellable"))
    data.setdefault("result", {})
    data.setdefault("metadata", {})
    return data


def _cached_job(job_uuid):
    return _deserialize_cached_job_payload(_cache_get(_JOB_PROGRESS_NAMESPACE, str(job_uuid)))


def _merge_cached_job(job):
    if not job:
        return job
    if str(job.get("status") or "") in TERMINAL_JOB_STATUSES:
        return job
    cached = _cached_job(job.get("job_uuid"))
    if not cached:
        return job
    merged = dict(job)
    merged.update(cached)
    return merged


def _cache_job_snapshot(snapshot):
    snapshot = _deserialize_cached_job_payload(snapshot)
    if not snapshot:
        return False
    return _cache_put(_JOB_PROGRESS_NAMESPACE, snapshot["job_uuid"], snapshot)


def _delete_cached_job(job_uuid):
    _cache_delete(_JOB_PROGRESS_NAMESPACE, str(job_uuid))
    _cache_delete(_JOB_PROGRESS_EVENT_NAMESPACE, str(job_uuid))


def _cached_event(job_uuid):
    payload = _cache_get(_JOB_PROGRESS_EVENT_NAMESPACE, str(job_uuid))
    return dict(payload) if isinstance(payload, dict) else None


def _cache_event(job_uuid, event):
    if not isinstance(event, dict):
        return False
    return _cache_put(_JOB_PROGRESS_EVENT_NAMESPACE, str(job_uuid), event)


def _mark_job_progress_flushed(job_uuid):
    with _JOB_PROGRESS_LOCK:
        _JOB_PROGRESS_FLUSH_STATE[str(job_uuid)] = time.monotonic()


def _mark_job_event_flushed(job_uuid):
    with _JOB_PROGRESS_LOCK:
        _JOB_PROGRESS_EVENT_FLUSH_STATE[str(job_uuid)] = time.monotonic()


def _job_progress_flush_due(job_uuid):
    interval = _progress_flush_interval_seconds()
    if interval <= 0:
        return True
    with _JOB_PROGRESS_LOCK:
        last = _JOB_PROGRESS_FLUSH_STATE.get(str(job_uuid))
    return last is None or (time.monotonic() - float(last)) >= interval


def _job_event_flush_due(job_uuid):
    interval = _progress_event_flush_interval_seconds()
    if interval <= 0:
        return True
    with _JOB_PROGRESS_LOCK:
        last = _JOB_PROGRESS_EVENT_FLUSH_STATE.get(str(job_uuid))
    return last is None or (time.monotonic() - float(last)) >= interval


def _snapshot_from_update(previous, normalized_updates):
    if not previous:
        return None
    snapshot = dict(previous)
    for key, value in normalized_updates.items():
        if key == "metadata_json":
            snapshot["metadata"] = _json_load(value)
            continue
        if key == "result_json":
            snapshot["result"] = _json_load(value)
            continue
        if key == "progress_percent":
            snapshot[key] = _normalize_progress(value)
            continue
        if key == "cancellable":
            snapshot[key] = bool(value)
            continue
        snapshot[key] = value
    snapshot["updated_at"] = utc_now()
    return snapshot


def _update_should_write_db(job_uuid, previous, normalized_updates, *, defer_progress=False, flush=False):
    if flush or not defer_progress or not _progress_buffer_enabled():
        return True
    if not normalized_updates:
        return True
    next_status = normalized_updates.get("status")
    if next_status in TERMINAL_JOB_STATUSES:
        return True
    if normalized_updates.get("finished_at") or normalized_updates.get("cancel_requested_at"):
        return True
    if normalized_updates.get("error_code") or normalized_updates.get("error_message") or normalized_updates.get("error_stage"):
        return True
    if normalized_updates.get("result_json") not in (None, "", "{}"):
        return True
    previous_status = str((previous or {}).get("status") or "")
    if next_status and previous_status and str(next_status) != previous_status:
        return True
    if not any(key in normalized_updates for key in ("status", "progress_percent", "stage", "stage_detail", "metadata_json", "started_at", "cancellable")):
        return True
    return _job_progress_flush_due(job_uuid)


def _schema_cache_key(conn):
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        for row in rows:
            try:
                name = row["name"]
                file_path = row["file"]
            except Exception:
                name = row[1]
                file_path = row[2]
            if name == "main" and file_path:
                return f"path:{file_path}"
    except Exception:
        pass
    return None


def _job_center_schema_present(conn):
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name='job_center_jobs'
            LIMIT 1
            """
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def ensure_job_center_schema(conn):
    cache_key = _schema_cache_key(conn)
    if cache_key is None:
        _ensure_job_center_schema_uncached(conn)
        return
    if cache_key in _JOB_CENTER_SCHEMA_READY:
        if _job_center_schema_present(conn):
            return
        with _JOB_CENTER_SCHEMA_LOCK:
            _JOB_CENTER_SCHEMA_READY.discard(cache_key)
    with _JOB_CENTER_SCHEMA_LOCK:
        if cache_key in _JOB_CENTER_SCHEMA_READY:
            if _job_center_schema_present(conn):
                return
            _JOB_CENTER_SCHEMA_READY.discard(cache_key)
        _ensure_job_center_schema_uncached(conn)
        if not bool(getattr(conn, "in_transaction", False)) and _job_center_schema_present(conn):
            _JOB_CENTER_SCHEMA_READY.add(cache_key)


def _ensure_job_center_schema_uncached(conn):
    was_in_transaction = bool(getattr(conn, "in_transaction", False))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_center_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_uuid TEXT NOT NULL UNIQUE,
            owner_user_id INTEGER,
            created_by_user_id INTEGER,
            job_type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            source_module TEXT NOT NULL,
            source_ref TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            progress_percent INTEGER NOT NULL DEFAULT 0,
            stage TEXT NOT NULL DEFAULT 'queued',
            stage_detail TEXT,
            error_code TEXT,
            error_message TEXT,
            error_stage TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 0,
            cancellable INTEGER NOT NULL DEFAULT 0,
            cancel_requested_at TEXT,
            result_json TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            updated_at TEXT NOT NULL,
            finished_at TEXT,
            expires_at TEXT,
            dismissed_at TEXT,
            dismissed_by_user_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_center_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_uuid TEXT NOT NULL,
            event_type TEXT NOT NULL,
            stage TEXT,
            message TEXT,
            progress_percent INTEGER,
            payload_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(job_center_jobs)").fetchall()}
    additions = {
        "owner_user_id": "INTEGER",
        "created_by_user_id": "INTEGER",
        "description": "TEXT",
        "source_ref": "TEXT",
        "stage_detail": "TEXT",
        "error_code": "TEXT",
        "error_message": "TEXT",
        "error_stage": "TEXT",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "max_retries": "INTEGER NOT NULL DEFAULT 0",
        "cancellable": "INTEGER NOT NULL DEFAULT 0",
        "cancel_requested_at": "TEXT",
        "result_json": "TEXT",
        "metadata_json": "TEXT",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "expires_at": "TEXT",
        "dismissed_at": "TEXT",
        "dismissed_by_user_id": "INTEGER",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE job_center_jobs ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_center_owner_status ON job_center_jobs(owner_user_id, status, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_center_status_updated ON job_center_jobs(status, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_center_source ON job_center_jobs(source_module, source_ref)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_center_events_job ON job_center_events(job_uuid, created_at)")
    if not was_in_transaction:
        try:
            conn.commit()
        except Exception:
            pass


def serialize_job(row):
    data = dict(row)
    data["progress_percent"] = max(0, min(100, _safe_int(data.get("progress_percent"), 0)))
    data["retry_count"] = _safe_int(data.get("retry_count"), 0)
    data["max_retries"] = _safe_int(data.get("max_retries"), 0)
    data["cancellable"] = bool(data.get("cancellable"))
    for key in ("result_json", "metadata_json"):
        raw = data.get(key)
        data[key.replace("_json", "")] = _json_load(raw)
        data.pop(key, None)
    return data


def serialize_event(row):
    data = dict(row)
    try:
        data["payload"] = json.loads(data.get("payload_json") or "{}")
    except Exception:
        data["payload"] = {}
    data.pop("payload_json", None)
    return data


def create_job(
    conn,
    *,
    owner_user_id=None,
    created_by_user_id=None,
    job_type,
    title,
    description="",
    source_module,
    source_ref=None,
    status="queued",
    progress_percent=0,
    stage="queued",
    stage_detail="",
    max_retries=0,
    cancellable=False,
    metadata=None,
    expires_at=None,
):
    ensure_job_center_schema(conn)
    status = str(status or "queued").strip() or "queued"
    if status not in JOB_STATUSES:
        status = "queued"
    now = utc_now()
    job_uuid = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO job_center_jobs (
            job_uuid, owner_user_id, created_by_user_id, job_type, title,
            description, source_module, source_ref, status, progress_percent,
            stage, stage_detail, max_retries, cancellable, metadata_json,
            created_at, updated_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_uuid,
            owner_user_id,
            created_by_user_id,
            str(job_type or "generic")[:80],
            str(title or "未命名任務")[:160],
            str(description or "")[:1000],
            str(source_module or "system")[:80],
            str(source_ref or "")[:160] or None,
            status,
            max(0, min(100, _safe_int(progress_percent, 0))),
            str(stage or status)[:80],
            str(stage_detail or "")[:1000],
            max(0, _safe_int(max_retries, 0)),
            1 if cancellable else 0,
            _json(metadata),
            now,
            now,
            expires_at,
        ),
    )
    add_job_event(conn, job_uuid, event_type="created", stage=stage or status, message="任務已建立", progress_percent=progress_percent)
    _mark_job_progress_flushed(job_uuid)
    _delete_cached_job(job_uuid)
    return get_job(conn, job_uuid)


def add_job_event(conn, job_uuid, *, event_type, stage=None, message="", progress_percent=None, payload=None, defer_progress=False, flush=False):
    ensure_job_center_schema(conn)
    event_type_value = str(event_type or "event")[:80]
    progress_value = None if progress_percent is None else _normalize_progress(progress_percent)
    event_payload = {
        "job_uuid": str(job_uuid),
        "event_type": event_type_value,
        "stage": str(stage or "")[:80] or None,
        "message": str(message or "")[:1000],
        "progress_percent": progress_value,
        "payload": payload or {},
        "created_at": utc_now(),
    }
    if (
        defer_progress
        and _progress_buffer_enabled()
        and not flush
        and not _event_is_terminal(event_type_value, progress_value)
        and not _job_event_flush_due(job_uuid)
    ):
        _cache_event(job_uuid, event_payload)
        return False
    conn.execute(
        """
        INSERT INTO job_center_events (job_uuid, event_type, stage, message, progress_percent, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_payload["job_uuid"],
            event_payload["event_type"],
            event_payload["stage"],
            event_payload["message"],
            event_payload["progress_percent"],
            _json(event_payload["payload"]),
            event_payload["created_at"],
        ),
    )
    _mark_job_event_flushed(job_uuid)
    _cache_delete(_JOB_PROGRESS_EVENT_NAMESPACE, str(job_uuid))
    return True


def update_job(conn, job_uuid, *, defer_progress=False, flush=False, **updates):
    ensure_job_center_schema(conn)
    previous = get_job(conn, job_uuid)
    allowed = {
        "status",
        "progress_percent",
        "stage",
        "stage_detail",
        "error_code",
        "error_message",
        "error_stage",
        "retry_count",
        "result_json",
        "metadata_json",
        "started_at",
        "finished_at",
        "expires_at",
        "cancel_requested_at",
        "cancellable",
    }
    fields = []
    values = []
    normalized_updates = {}
    for key, value in updates.items():
        if key not in allowed:
            continue
        if key == "status" and value not in JOB_STATUSES:
            continue
        if key == "progress_percent":
            value = _normalize_progress(value)
        if key in {"result_json", "metadata_json"} and not isinstance(value, str):
            value = _json(value)
        normalized_updates[key] = value
        fields.append(f"{key}=?")
        values.append(value)
    if not fields:
        return previous
    if not _update_should_write_db(
        job_uuid,
        previous,
        normalized_updates,
        defer_progress=defer_progress,
        flush=flush,
    ):
        snapshot = _snapshot_from_update(previous, normalized_updates)
        if snapshot:
            _cache_job_snapshot(snapshot)
            return snapshot
        return previous
    fields.append("updated_at=?")
    values.append(utc_now())
    values.append(str(job_uuid))
    conn.execute(f"UPDATE job_center_jobs SET {', '.join(fields)} WHERE job_uuid=?", tuple(values))
    updated = _get_job_from_db(conn, job_uuid)
    _maybe_create_terminal_notification(
        conn,
        updated,
        previous_status=previous.get("status") if previous else None,
    )
    _mark_job_progress_flushed(job_uuid)
    if updated and updated.get("status") in TERMINAL_JOB_STATUSES:
        _delete_cached_job(job_uuid)
    elif updated:
        _cache_job_snapshot(updated)
    return updated


def get_job(conn, job_uuid):
    return _merge_cached_job(_get_job_from_db(conn, job_uuid))


def _get_job_from_db(conn, job_uuid):
    ensure_job_center_schema(conn)
    row = conn.execute("SELECT * FROM job_center_jobs WHERE job_uuid=?", (str(job_uuid),)).fetchone()
    return serialize_job(row) if row else None


def get_job_by_source(conn, source_module, source_ref):
    ensure_job_center_schema(conn)
    row = conn.execute(
        "SELECT * FROM job_center_jobs WHERE source_module=? AND source_ref=? ORDER BY id DESC LIMIT 1",
        (str(source_module), str(source_ref)),
    ).fetchone()
    return _merge_cached_job(serialize_job(row)) if row else None


def list_jobs(conn, *, user_id=None, include_all=False, status=None, limit=50):
    ensure_job_center_schema(conn)
    where = []
    params = []
    where.append("dismissed_at IS NULL")
    if not include_all:
        where.append("owner_user_id=?")
        params.append(int(user_id))
    if status:
        where.append("status=?")
        params.append(str(status))
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        f"SELECT * FROM job_center_jobs {sql_where} ORDER BY updated_at DESC, id DESC LIMIT ?",
        tuple(params + [max(1, min(200, _safe_int(limit, 50)))]),
    ).fetchall()
    return [_merge_cached_job(serialize_job(row)) for row in rows]


def terminal_job_retention_seconds(status):
    value = str(status or "")
    if value in {"succeeded", "cancelled"}:
        return _env_int(
            "HACKME_JOB_CENTER_SUCCEEDED_RETENTION_SECONDS",
            DEFAULT_SUCCEEDED_JOB_RETENTION_SECONDS,
            minimum=0,
        )
    if value in {"failed", "expired"}:
        return _env_int(
            "HACKME_JOB_CENTER_FAILED_RETENTION_SECONDS",
            DEFAULT_FAILED_JOB_RETENTION_SECONDS,
            minimum=0,
        )
    return None


def purge_terminal_jobs(conn, *, limit=200, now=None):
    ensure_job_center_schema(conn)
    rows = conn.execute(
        """
        SELECT job_uuid, status, COALESCE(finished_at, updated_at, created_at) AS terminal_at
        FROM job_center_jobs
        WHERE status IN ('succeeded', 'failed', 'cancelled', 'expired')
        ORDER BY COALESCE(finished_at, updated_at, created_at) ASC, id ASC
        LIMIT ?
        """,
        (max(1, min(1000, _safe_int(limit, 200))),),
    ).fetchall()
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    purged = []
    for row in rows:
        retention = terminal_job_retention_seconds(row["status"])
        if retention is None:
            continue
        terminal_at = _parse_terminal_age_timestamp(row["terminal_at"], now=base)
        if not terminal_at:
            continue
        age = max(0, int((base - terminal_at).total_seconds()))
        if age < retention:
            continue
        job_uuid = str(row["job_uuid"] or "")
        conn.execute("DELETE FROM job_center_events WHERE job_uuid=?", (job_uuid,))
        conn.execute("DELETE FROM job_center_jobs WHERE job_uuid=?", (job_uuid,))
        _delete_cached_job(job_uuid)
        purged.append({"job_uuid": job_uuid, "status": row["status"], "age_seconds": age})
    return purged


def dismiss_job(conn, job_uuid, *, actor_user_id=None, allow_active=False):
    ensure_job_center_schema(conn)
    job = get_job(conn, job_uuid)
    if not job:
        return None
    if not allow_active and str(job.get("status") or "") not in TERMINAL_JOB_STATUSES:
        raise ValueError("active_job")
    now = utc_now()
    conn.execute(
        """
        UPDATE job_center_jobs
        SET dismissed_at=?, dismissed_by_user_id=?, updated_at=?
        WHERE job_uuid=?
        """,
        (now, actor_user_id, now, str(job_uuid)),
    )
    add_job_event(
        conn,
        job_uuid,
        event_type="dismissed",
        stage=str(job.get("stage") or job.get("status") or "dismissed"),
        message="任務已從列表移除",
        progress_percent=job.get("progress_percent", 100),
        payload={"dismissed_by_user_id": actor_user_id},
        flush=True,
    )
    _delete_cached_job(job_uuid)
    return get_job(conn, job_uuid)


def _cloud_remote_download_stale_after_seconds(job):
    status = str((job or {}).get("status") or "")
    metadata = (job or {}).get("metadata") if isinstance((job or {}).get("metadata"), dict) else {}
    source_type = str(metadata.get("source_type") or "")
    timeout = _safe_int(metadata.get("timeout_seconds"), 0)
    if timeout <= 0:
        timeout = 1800 if source_type in {"magnet", "torrent_file", "torrent_url"} else 120
    return max(300, timeout + 180)


def _cloud_remote_download_last_activity_at(job, *, now=None):
    now = now or datetime.now(timezone.utc)
    metadata = (job or {}).get("metadata") if isinstance((job or {}).get("metadata"), dict) else {}
    candidates = [
        (job or {}).get("updated_at"),
        (job or {}).get("created_at"),
        metadata.get("worker_heartbeat_at"),
        metadata.get("progress_heartbeat_at"),
    ]
    parsed = []
    for value in candidates:
        timestamp = _parse_utc_timestamp(value)
        if timestamp and timestamp <= now + timedelta(seconds=60):
            parsed.append(timestamp)
    return max(parsed) if parsed else None


def _cloud_remote_download_job_is_stale(job, *, now=None):
    status = str((job or {}).get("status") or "")
    if status not in {"queued", "running", "waiting_external", "retry_wait"}:
        return False
    now = now or datetime.now(timezone.utc)
    active_at = _cloud_remote_download_last_activity_at(job, now=now)
    if not active_at:
        return False
    age = max(0, int((now - active_at).total_seconds()))
    return age > _cloud_remote_download_stale_after_seconds(job)


def _cloud_remote_download_cancel_request_is_stale(job, *, now=None):
    status = str((job or {}).get("status") or "")
    if status not in {"running", "waiting_external", "retry_wait"}:
        return False
    metadata = (job or {}).get("metadata") if isinstance((job or {}).get("metadata"), dict) else {}
    stage = str((job or {}).get("stage") or "")
    has_cancel_request = bool(
        (job or {}).get("cancel_requested_at")
        or stage == "cancel_requested"
        or str(metadata.get("control_action") or "") == "cancel"
    )
    if not has_cancel_request:
        return False
    requested_at = _parse_utc_timestamp(
        (job or {}).get("cancel_requested_at")
        or metadata.get("cancel_requested_at")
        or metadata.get("cancel_recovered_at")
        or (job or {}).get("updated_at")
        or (job or {}).get("created_at")
    )
    if not requested_at:
        return False
    now = now or datetime.now(timezone.utc)
    grace_seconds = max(5, _safe_int(os.environ.get("HACKME_REMOTE_DOWNLOAD_CANCEL_ACK_GRACE_SECONDS"), 30))
    age = max(0, int((now - requested_at).total_seconds()))
    return age >= grace_seconds


def expire_stale_cloud_remote_download_jobs(conn, *, limit=100):
    """Mark orphaned persisted cloud-drive remote-download jobs as failed.

    In-memory remote-download tasks are lost on worker restart/HUP. Persisted
    Job Center rows should remain visible, but active rows that exceed the same
    timeout window as their worker must not stay "running" forever.
    """
    ensure_job_center_schema(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM job_center_jobs
        WHERE source_module='cloud_drive_remote_download'
          AND status IN ('queued', 'running', 'waiting_external', 'retry_wait')
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        """,
        (max(1, min(500, _safe_int(limit, 100))),),
    ).fetchall()
    now = datetime.now(timezone.utc)
    now_text = now.replace(tzinfo=None, microsecond=0).isoformat()
    message = "遠端下載任務逾時或已中斷，請重新建立下載任務"
    cancel_message = "下載任務已取消；worker 未在時間內回報，系統已清理任務狀態"
    expired = []
    for row in rows:
        job = _merge_cached_job(serialize_job(row))
        if _cloud_remote_download_cancel_request_is_stale(job, now=now):
            metadata = dict(job.get("metadata") or {})
            metadata.update({
                "cancel_recovered_at": now_text,
                "cancel_recovery_reason": "remote_download_cancel_request_not_acknowledged",
            })
            updated = update_job(
                conn,
                job["job_uuid"],
                status="cancelled",
                progress_percent=100,
                stage="cancelled",
                stage_detail=cancel_message,
                metadata_json=metadata,
                cancellable=False,
                finished_at=now_text,
                flush=True,
            )
            if updated:
                add_job_event(
                    conn,
                    job["job_uuid"],
                    event_type="cancelled",
                    stage="cancelled",
                    message=cancel_message,
                    progress_percent=100,
                    payload=metadata,
                    flush=True,
                )
                expired.append(updated)
            continue
        if not _cloud_remote_download_job_is_stale(job, now=now):
            continue
        metadata = dict(job.get("metadata") or {})
        metadata.update({
            "stale_recovered_at": now_text,
            "stale_recovery_reason": "remote_download_worker_missing_or_timed_out",
        })
        updated = update_job(
            conn,
            job["job_uuid"],
            status="failed",
            progress_percent=100,
            stage="failed",
            stage_detail=message,
            error_code="remote_download_task_stale",
            error_message=message,
            error_stage="stale_task_cleanup",
            metadata_json=metadata,
            cancellable=False,
            finished_at=now_text,
            flush=True,
        )
        if updated:
            add_job_event(
                conn,
                job["job_uuid"],
                event_type="failed",
                stage="failed",
                message=message,
                progress_percent=100,
                payload=metadata,
                flush=True,
            )
            expired.append(updated)
    return expired


def _table_exists(conn, table_name):
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(table_name),),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _resumable_upload_job_stale_after_seconds(job):
    metadata = (job or {}).get("metadata") if isinstance((job or {}).get("metadata"), dict) else {}
    stage = str((job or {}).get("stage") or "").strip().lower()
    received_bytes = _safe_int(metadata.get("received_bytes"), 0)
    if received_bytes <= 0 and stage in {"", "created", "queued", "uploading"}:
        return _env_int("HACKME_RESUMABLE_UPLOAD_EMPTY_STALE_SECONDS", 15 * 60, minimum=60)
    return _env_int("HACKME_RESUMABLE_UPLOAD_STALE_SECONDS", 60 * 60, minimum=5 * 60)


def _resumable_upload_job_is_stale(job, *, now=None):
    status = str((job or {}).get("status") or "")
    if status not in {"queued", "running", "waiting_external", "retry_wait"}:
        return False
    updated = _parse_utc_timestamp((job or {}).get("updated_at") or (job or {}).get("created_at"))
    if not updated:
        return False
    now = now or datetime.now(timezone.utc)
    age = max(0, int((now - updated).total_seconds()))
    return age >= _resumable_upload_job_stale_after_seconds(job)


def expire_stale_resumable_upload_jobs(conn, *, limit=100):
    """Expire orphaned resumable-upload jobs that no longer receive chunks.

    A resumable upload is user-driven, so a short gap is normal. Rows that stay
    at 0 bytes for the stale window are misleading in the task center: they are
    not an active background process and should not remain "running" forever.
    """

    ensure_job_center_schema(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM job_center_jobs
        WHERE source_module='cloud_drive_resumable_upload'
          AND status IN ('queued', 'running', 'waiting_external', 'retry_wait')
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        """,
        (max(1, min(500, _safe_int(limit, 100))),),
    ).fetchall()
    now = datetime.now(timezone.utc)
    now_text = now.replace(tzinfo=None, microsecond=0).isoformat()
    message = "分段上傳 session 長時間沒有進度，系統已標記過期；請重新上傳"
    expired = []
    for row in rows:
        job = _merge_cached_job(serialize_job(row))
        if not _resumable_upload_job_is_stale(job, now=now):
            continue
        metadata = dict(job.get("metadata") or {})
        session_id = str(metadata.get("session_id") or "").strip()
        metadata.update({
            "stale_recovered_at": now_text,
            "stale_recovery_reason": "resumable_upload_no_progress",
        })
        updated = update_job(
            conn,
            job["job_uuid"],
            status="expired",
            progress_percent=100,
            stage="expired",
            stage_detail=message,
            error_code="resumable_upload_stale",
            error_message=message,
            error_stage="stale_session_cleanup",
            metadata_json=metadata,
            cancellable=False,
            finished_at=now_text,
            flush=True,
        )
        if session_id and _table_exists(conn, "cloud_resumable_upload_sessions"):
            conn.execute(
                """
                UPDATE cloud_resumable_upload_sessions
                SET status='expired', error_message=?, updated_at=?
                WHERE session_id=?
                  AND status IN ('created', 'uploading', 'completing')
                """,
                (message, now_text, session_id),
            )
        if updated:
            add_job_event(
                conn,
                job["job_uuid"],
                event_type="expired",
                stage="expired",
                message=message,
                progress_percent=100,
                payload=metadata,
                flush=True,
            )
            expired.append(updated)
    return expired


def _media_hls_stale_after_seconds(job):
    stage = str((job or {}).get("stage") or "").strip().lower()
    if stage in {"queued", "worker_started", "waiting_worker_slot"}:
        return _env_int("HACKME_MEDIA_HLS_QUEUE_STALE_SECONDS", 15 * 60, minimum=60)
    return _env_int("HACKME_MEDIA_HLS_STALE_SECONDS", 20 * 60, minimum=2 * 60)


def _media_hls_file_id(job):
    metadata = (job or {}).get("metadata") if isinstance((job or {}).get("metadata"), dict) else {}
    file_id = str(metadata.get("file_id") or "").strip()
    if file_id:
        return file_id
    source_ref = str((job or {}).get("source_ref") or "").strip()
    prefix = "media_stream:"
    if source_ref.startswith(prefix):
        return source_ref[len(prefix):].strip()
    return ""


def _media_hls_process_is_running(job):
    file_id = _media_hls_file_id(job)
    try:
        proc = subprocess.run(
            ["ps", "-eo", "comm=,args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        # If process inspection is unavailable, avoid false-failing a long HLS
        # transcode purely from a stale timestamp.
        return True
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "hls_prepare_worker.py" in line and (not file_id or file_id in line):
            return True
        if file_id and "ffmpeg" in line and file_id in line and ("media_derivatives" in line or ".m4s" in line):
            return True
    return False


def _media_hls_job_is_stale(job, *, now=None):
    status = str((job or {}).get("status") or "")
    if status not in {"queued", "running", "waiting_external", "retry_wait"}:
        return False
    updated = _parse_utc_timestamp((job or {}).get("updated_at") or (job or {}).get("created_at"))
    if not updated:
        return False
    now = now or datetime.now(timezone.utc)
    age = max(0, int((now - updated).total_seconds()))
    if age < _media_hls_stale_after_seconds(job):
        return False
    return not _media_hls_process_is_running(job)


def expire_stale_media_hls_jobs(conn, *, limit=100):
    """Mark orphaned HLS worker jobs as failed.

    Large HLS jobs update Job Center from the external worker while decrypting,
    probing, and running ffmpeg.  If the worker/ffmpeg process disappears before
    it can write a terminal state, the UI must not keep showing an endless
    "running" job.
    """

    ensure_job_center_schema(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM job_center_jobs
        WHERE source_module='media_hls_prepare'
          AND status IN ('queued', 'running', 'waiting_external', 'retry_wait')
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        """,
        (max(1, min(500, _safe_int(limit, 100))),),
    ).fetchall()
    now = datetime.now(timezone.utc)
    now_text = now.replace(tzinfo=None, microsecond=0).isoformat()
    message = "HLS 背景處理程序已中斷或長時間無進度，系統已標記失敗；請從任務中心重試。"
    expired = []
    for row in rows:
        job = _merge_cached_job(serialize_job(row))
        if not _media_hls_job_is_stale(job, now=now):
            continue
        file_id = _media_hls_file_id(job)
        metadata = dict(job.get("metadata") or {})
        metadata.update({
            "stale_recovered_at": now_text,
            "stale_recovery_reason": "media_hls_worker_missing_or_timed_out",
            "file_id": file_id or metadata.get("file_id") or "",
        })
        updated = update_job(
            conn,
            job["job_uuid"],
            status="failed",
            progress_percent=100,
            stage="failed",
            stage_detail=message,
            error_code="media_hls_task_stale",
            error_message=message,
            error_stage="stale_task_cleanup",
            metadata_json=metadata,
            cancellable=False,
            finished_at=now_text,
            flush=True,
        )
        if file_id and _table_exists(conn, "media_stream_assets"):
            conn.execute(
                """
                UPDATE media_stream_assets
                SET status='failed', error_message=?, updated_at=?
                WHERE uploaded_file_id=?
                  AND status='processing'
                """,
                (message, now_text, file_id),
            )
        if updated:
            add_job_event(
                conn,
                job["job_uuid"],
                event_type="failed",
                stage="failed",
                message=message,
                progress_percent=100,
                payload=metadata,
                flush=True,
            )
            expired.append(updated)
    return expired


def list_job_events(conn, job_uuid, *, limit=100):
    ensure_job_center_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM job_center_events
        WHERE job_uuid=?
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (str(job_uuid), max(1, min(500, _safe_int(limit, 100)))),
    ).fetchall()
    events = [serialize_event(row) for row in rows]
    cached = _cached_event(job_uuid)
    if cached:
        events.append(cached)
    return events


def request_cancel(conn, job_uuid):
    job = update_job(
        conn,
        job_uuid,
        status="cancelled",
        cancel_requested_at=utc_now(),
        stage="cancelled",
        stage_detail="使用者要求取消",
        finished_at=utc_now(),
    )
    if job:
        add_job_event(conn, job_uuid, event_type="cancelled", stage="cancelled", message="任務已取消", progress_percent=job.get("progress_percent", 0))
    return job


def request_retry(conn, job_uuid):
    job = get_job(conn, job_uuid)
    if not job:
        return None
    retry_count = _safe_int(job.get("retry_count"), 0) + 1
    updated = update_job(
        conn,
        job_uuid,
        status="queued",
        retry_count=retry_count,
        stage="queued",
        stage_detail="等待重試",
        error_code=None,
        error_message=None,
        error_stage=None,
        finished_at=None,
    )
    add_job_event(conn, job_uuid, event_type="retry_requested", stage="queued", message="任務已排入重試", progress_percent=updated.get("progress_percent", 0))
    return updated
