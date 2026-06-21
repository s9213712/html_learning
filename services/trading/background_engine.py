"""Server-owned trading background jobs.

This module intentionally keeps the first implementation simple: a single-node
SQLite lease protects each job, while the job bodies call the existing trading
service entry points. The frontend is not part of this execution path.
"""

from __future__ import annotations

import json
import threading
import socket
import time
import uuid
from datetime import datetime, timedelta

from services.core.sqlite_hardening import is_sqlite_retryable_operational_error
from services.server_mode.context import SmV2Context


BACKGROUND_JOB_DEFINITIONS = {
    "price_refresh": {
        "interval_seconds": 5,
        "lease_seconds": 20,
    },
    "order_matching": {
        "interval_seconds": 5,
        "lease_seconds": 20,
    },
    "take_profit_stop_loss_scan": {
        "interval_seconds": 5,
        "lease_seconds": 20,
    },
    "bot_trigger_scan": {
        "interval_seconds": 30,
        "lease_seconds": 60,
    },
    "margin_liquidation_scan": {
        "interval_seconds": 30,
        "lease_seconds": 60,
    },
    "interest_accrual": {
        "interval_seconds": 3600,
        "lease_seconds": 120,
    },
    "sitewide_metrics_refresh": {
        "interval_seconds": 60,
        "lease_seconds": 120,
    },
}

BACKGROUND_JOB_LABELS = {
    "price_refresh": "價格刷新",
    "order_matching": "掛單撮合",
    "take_profit_stop_loss_scan": "止盈止損掃描",
    "bot_trigger_scan": "交易機器人自動掃描",
    "margin_liquidation_scan": "借貸清算掃描",
    "interest_accrual": "借貸利息結算",
    "sitewide_metrics_refresh": "交易所報表快照",
}

PAUSED_MODES = {"maintenance", "incident_lockdown", "superweak"}
SHADOW_REQUIRES_TESTER_MODES = {"internal_test"}
SYSTEM_ACTOR = {"id": 0, "username": "system", "role": "system"}
_BACKGROUND_SCHEMA_LOCK = threading.RLock()
_BACKGROUND_SCHEMA_READY_KEYS = set()


def _background_schema_cache_key(service, conn):
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        for row in rows:
            name = row["name"] if hasattr(row, "keys") else row[1]
            path = row["file"] if hasattr(row, "keys") else row[2]
            if str(name or "") == "main" and str(path or ""):
                return str(path)
    except Exception:
        pass
    return f"service:{id(service)}"


def _with_sqlite_background_retry(operation, *, attempts=20, base_sleep=0.05):
    for attempt in range(max(1, int(attempts or 1))):
        try:
            return operation()
        except Exception as exc:
            if not is_sqlite_retryable_operational_error(exc) or attempt >= attempts - 1:
                raise
            time.sleep(min(0.25, float(base_sleep or 0.05) * (attempt + 1)))


def _now_dt() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _now_text() -> str:
    return _now_dt().isoformat()


def _parse_dt(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _json_dumps(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def _job_center_status_for_background(status: str) -> str:
    status = str(status or "")
    if status in {"success", "skipped"}:
        return "running"
    if status == "failed":
        return "retry_wait"
    if status == "running":
        return "running"
    return "running"


def _sync_job_center_background_run(
    service,
    *,
    lease,
    status,
    result=None,
    error="",
    finished_at="",
    duration_ms=None,
    next_run_at="",
):
    """Expose recurring trading jobs in the root task center without blocking trading."""
    try:
        from services.job_center import create_job, get_job_by_source, update_job
    except Exception:
        return
    job_key = str((lease or {}).get("job_key") or "")
    if not job_key:
        return
    label = BACKGROUND_JOB_LABELS.get(job_key, job_key)
    job_status = _job_center_status_for_background(status)
    success = str(status or "") == "success"
    skipped = str(status or "") == "skipped"
    progress = 100 if success or skipped else 0
    summary = result or {}
    detail_parts = []
    if success:
        detail_parts.append("上次執行成功")
    elif skipped:
        reason = str(summary.get("reason") or "條件未符合")
        detail_parts.append(f"上次略過：{reason}")
    else:
        detail_parts.append(f"上次執行失敗：{str(error or 'unknown')[:160]}")
    if duration_ms is not None:
        try:
            detail_parts.append(f"{int(round(float(duration_ms)))} ms")
        except Exception:
            pass
    if next_run_at:
        detail_parts.append(f"下次 {next_run_at}")
    metadata = {
        "server_background": True,
        "recurring": True,
        "login_required": False,
        "job_key": job_key,
        "run_uuid": lease.get("run_uuid"),
        "run_id": lease.get("run_id"),
        "lease_owner": lease.get("lease_owner"),
        "last_background_status": status,
        "last_finished_at": finished_at,
        "next_run_at": next_run_at,
        "result": summary,
    }
    conn = service.get_db()
    try:
        existing = get_job_by_source(conn, "trading_background", job_key)
        if not existing:
            create_job(
                conn,
                owner_user_id=None,
                created_by_user_id=None,
                job_type=f"trading.background.{job_key}",
                title=f"交易背景：{label}",
                description="伺服器自動交易與結算背景工作，不依賴會員登入或前台按鈕。",
                source_module="trading_background",
                source_ref=job_key,
                status=job_status,
                progress_percent=progress,
                stage=str(status or job_status)[:80],
                stage_detail=" · ".join(detail_parts),
                cancellable=False,
                metadata=metadata,
            )
        else:
            update_job(
                conn,
                existing["job_uuid"],
                status=job_status,
                progress_percent=progress,
                stage=str(status or job_status)[:80],
                stage_detail=" · ".join(detail_parts),
                error_message=str(error or "")[:1000] if job_status in {"failed", "retry_wait"} else "",
                error_stage=str(status or "")[:80] if job_status in {"failed", "retry_wait"} else "",
                result_json=summary,
                metadata_json=metadata,
                started_at=lease.get("started_at") or None,
                finished_at=finished_at or None,
                cancellable=False,
                defer_progress=True,
            )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def _feature_enabled(settings, key, *, default=False):
    value = (settings or {}).get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _runtime_mode(get_runtime_server_mode=None) -> str:
    try:
        mode = get_runtime_server_mode() if get_runtime_server_mode else "production"
    except Exception:
        mode = "production"
    return str(mode or "production").strip().lower() or "production"


def _lease_owner(prefix="trading-bg") -> str:
    return f"{prefix}:{socket.gethostname()}:{uuid.uuid4().hex[:10]}"


def _ctx_for_background_mode(mode: str) -> SmV2Context:
    normalized = str(mode or "production").strip().lower() or "production"
    # `test` mode runs in an isolated runtime. The current table router has no
    # physical `test_*` tables, so inside that isolated DB we use the production
    # physical tables while recording the real server mode in job metadata.
    routed_mode = "production" if normalized == "test" else normalized
    return SmV2Context(
        mode=routed_mode,
        tester_id=None,
        actor_role="system",
        request_id=f"trading-background-{uuid.uuid4().hex[:8]}",
    )


def _job_interval(job_key, overrides=None):
    default = int(BACKGROUND_JOB_DEFINITIONS[job_key]["interval_seconds"])
    try:
        return max(1, int((overrides or {}).get(job_key, default)))
    except Exception:
        return default


def ensure_background_schema(service, conn=None):
    owns_conn = conn is None
    conn = conn or service.get_db()
    try:
        cache_key = _background_schema_cache_key(service, conn)
        if cache_key in _BACKGROUND_SCHEMA_READY_KEYS:
            return
        with _BACKGROUND_SCHEMA_LOCK:
            if cache_key in _BACKGROUND_SCHEMA_READY_KEYS:
                return
            service.ensure_schema(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trading_background_jobs (
                    job_key TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    interval_seconds INTEGER NOT NULL DEFAULT 30,
                    lease_seconds INTEGER NOT NULL DEFAULT 60,
                    lease_owner TEXT,
                    lease_until TEXT,
                    last_started_at TEXT,
                    last_finished_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT 'never_run',
                    last_summary_json TEXT NOT NULL DEFAULT '{}',
                    run_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    next_run_at TEXT,
                    paused_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trading_background_locks (
                    job_key TEXT PRIMARY KEY,
                    lease_owner TEXT NOT NULL,
                    lease_until TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    renewed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trading_background_job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_uuid TEXT NOT NULL UNIQUE,
                    job_key TEXT NOT NULL,
                    lease_owner TEXT NOT NULL,
                    server_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_ms REAL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trading_background_job_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_uuid TEXT NOT NULL UNIQUE,
                    job_key TEXT NOT NULL,
                    requested_by_user_id INTEGER,
                    requested_by_username TEXT NOT NULL DEFAULT '',
                    force INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'queued',
                    requested_at TEXT NOT NULL,
                    next_attempt_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    worker_owner TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trading_root_snapshots (
                    snapshot_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    generated_at TEXT NOT NULL,
                    source_job_key TEXT NOT NULL DEFAULT '',
                    source_run_uuid TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trading_background_job_runs_job_started ON trading_background_job_runs(job_key, started_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trading_background_job_queue_status_next ON trading_background_job_queue(status, next_attempt_at, id)"
            )
            now = _now_text()
            for job_key, definition in BACKGROUND_JOB_DEFINITIONS.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO trading_background_jobs (
                        job_key, enabled, interval_seconds, lease_seconds,
                        last_summary_json, updated_at
                    ) VALUES (?, 1, ?, ?, '{}', ?)
                    """,
                    (
                        job_key,
                        int(definition["interval_seconds"]),
                        int(definition["lease_seconds"]),
                        now,
                    ),
                )
            conn.commit()
            _BACKGROUND_SCHEMA_READY_KEYS.add(cache_key)
    finally:
        if owns_conn:
            conn.close()


def _job_due(row, *, now_dt, force=False):
    if force:
        return True
    if not int(row["enabled"] or 0):
        return False
    next_run = _parse_dt(row["next_run_at"])
    return next_run is None or next_run <= now_dt


def _acquire_lease(service, *, job_key, owner, server_mode, force=False):
    conn = service.get_db()
    try:
        ensure_background_schema(service, conn)
        conn.commit()
        now_dt = _now_dt()
        now = now_dt.isoformat()
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM trading_background_jobs WHERE job_key=?", (job_key,)).fetchone()
        if not job:
            conn.rollback()
            return None
        if not _job_due(job, now_dt=now_dt, force=force):
            conn.rollback()
            return None
        lock = conn.execute("SELECT * FROM trading_background_locks WHERE job_key=?", (job_key,)).fetchone()
        if lock:
            lease_until = _parse_dt(lock["lease_until"])
            if lease_until and lease_until > now_dt and str(lock["lease_owner"] or "") != owner:
                conn.rollback()
                return None
        lease_seconds = max(5, int(job["lease_seconds"] or BACKGROUND_JOB_DEFINITIONS[job_key]["lease_seconds"]))
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        acquired_at = str(lock["acquired_at"] or now) if lock else now
        conn.execute(
            """
            INSERT OR REPLACE INTO trading_background_locks (
                job_key, lease_owner, lease_until, acquired_at, renewed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (job_key, owner, lease_until, acquired_at, now),
        )
        conn.execute(
            """
            UPDATE trading_background_jobs
            SET lease_owner=?, lease_until=?, last_started_at=?, last_status='running',
                run_count=run_count+1, updated_at=?
            WHERE job_key=?
            """,
            (owner, lease_until, now, now, job_key),
        )
        run_uuid = str(uuid.uuid4())
        cur = conn.execute(
            """
            INSERT INTO trading_background_job_runs (
                run_uuid, job_key, lease_owner, server_mode, status, started_at
            ) VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (run_uuid, job_key, owner, server_mode, now),
        )
        conn.commit()
        return {
            "run_id": int(cur.lastrowid),
            "run_uuid": run_uuid,
            "job_key": job_key,
            "started_at": now,
            "interval_seconds": int(job["interval_seconds"] or BACKGROUND_JOB_DEFINITIONS[job_key]["interval_seconds"]),
            "lease_seconds": lease_seconds,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finish_run(service, *, lease, owner, status, result=None, error=""):
    conn = service.get_db()
    try:
        ensure_background_schema(service, conn)
        conn.commit()
        finished = _now_text()
        started = _parse_dt(lease.get("started_at"))
        duration_ms = None
        if started:
            duration_ms = max(0.0, (_parse_dt(finished) - started).total_seconds() * 1000.0)
        interval = max(1, int(lease.get("interval_seconds") or BACKGROUND_JOB_DEFINITIONS[lease["job_key"]]["interval_seconds"]))
        if status == "skipped":
            interval = max(interval, 60)
        next_run_at = (_parse_dt(finished) + timedelta(seconds=interval)).isoformat()
        summary = result or {}
        err_text = str(error or "")[:1000]
        success = status == "success"
        counts_as_failure = status not in {"success", "skipped"}
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE trading_background_job_runs
            SET status=?, finished_at=?, duration_ms=?, result_json=?, error=?
            WHERE id=? AND lease_owner=?
            """,
            (status, finished, duration_ms, _json_dumps(summary), err_text, int(lease["run_id"]), owner),
        )
        conn.execute(
            """
            UPDATE trading_background_jobs
            SET lease_owner=NULL, lease_until=NULL, last_finished_at=?,
                last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
                last_error=?, last_status=?, last_summary_json=?,
                failure_count=failure_count+CASE WHEN ? THEN 0 ELSE 1 END,
                next_run_at=?, updated_at=?
            WHERE job_key=?
            """,
            (
                finished,
                1 if success else 0,
                finished,
                "" if not counts_as_failure else err_text,
                status,
                _json_dumps(summary),
                0 if counts_as_failure else 1,
                next_run_at,
                finished,
                lease["job_key"],
            ),
        )
        conn.execute(
            "DELETE FROM trading_background_locks WHERE job_key=? AND lease_owner=?",
            (lease["job_key"], owner),
        )
        conn.commit()
        _sync_job_center_background_run(
            service,
            lease={**lease, "lease_owner": owner},
            status=status,
            result=summary,
            error=err_text,
            finished_at=finished,
            duration_ms=duration_ms,
            next_run_at=next_run_at,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _skip_result(reason, **extra):
    payload = {"skipped": True, "reason": reason}
    payload.update(extra)
    return payload


def _background_features(settings):
    return {
        "economy_enabled": _feature_enabled(settings, "feature_economy_enabled", default=False),
        "trading_enabled": _feature_enabled(settings, "feature_trading_enabled", default=False),
    }


def _job_settings(service, get_system_settings=None):
    settings = dict(get_system_settings() if get_system_settings else {})
    try:
        trading_settings = (service.get_root_settings().get("settings") or {})
        settings.update(trading_settings)
        raw = trading_settings.get("raw") or {}
        if isinstance(raw, dict):
            settings.update(raw)
    except Exception:
        pass
    return settings


def _should_skip_job(job_key, *, server_mode, settings):
    if server_mode in PAUSED_MODES:
        return f"server_mode_{server_mode}_paused"
    if server_mode in SHADOW_REQUIRES_TESTER_MODES:
        return "internal_test_background_worker_requires_explicit_tester_scope"
    features = _background_features(settings)
    if not features["economy_enabled"] or not features["trading_enabled"]:
        return "feature_disabled"
    if server_mode == "dev_ready" and job_key == "sitewide_metrics_refresh":
        return ""
    if server_mode == "dev_ready":
        if not (
            _feature_enabled(settings, "background_worker_dev_ready_enabled", default=False)
            or _feature_enabled(settings, "trading.background_worker_dev_ready_enabled", default=False)
        ):
            return "server_mode_dev_ready_background_worker_disabled_by_default"
    return ""


def _run_price_refresh(service):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        symbols = service._list_live_price_market_symbols(conn)
    finally:
        conn.close()
    refreshed = []
    errors = []
    for symbol in symbols:
        try:
            quote = service.get_live_market_quote(market_symbol=symbol, force_refresh=True)
            market = quote.get("market") or {}
            refreshed.append(
                {
                    "symbol": symbol,
                    "price": market.get("manual_price_points"),
                    "source": quote.get("source"),
                    "risk_grade_usable": bool(quote.get("risk_grade_usable")),
                    "price_health": quote.get("price_health"),
                }
            )
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)[:300]})
    return {"refreshed_count": len(refreshed), "error_count": len(errors), "refreshed": refreshed, "errors": errors}


def _run_order_matching(service, *, ctx):
    match_result = service.match_open_limit_orders(actor=SYSTEM_ACTOR, limit=200, ctx=ctx)
    return {
        "scanned": match_result.get("scanned", 0),
        "matched_count": len(match_result.get("matched") or []),
        "error_count": len(match_result.get("errors") or []),
        "match_result": match_result,
    }


def _run_take_profit_stop_loss(service, *, ctx):
    spot = service.scan_spot_risk_targets(actor=SYSTEM_ACTOR, limit=200, ctx=ctx)
    margin = service.scan_margin_risk_targets(actor=SYSTEM_ACTOR, limit=100, ctx=ctx)
    return {
        "spot_scanned": spot.get("scanned", 0),
        "spot_triggered_count": len(spot.get("triggered") or []),
        "spot_error_count": len(spot.get("errors") or []),
        "margin_scanned": margin.get("scanned", 0),
        "margin_triggered_count": len(margin.get("triggered") or []),
        "margin_error_count": len(margin.get("errors") or []),
        "spot": spot,
        "margin": margin,
    }


def _run_all_grid_bots(service, *, limit=100):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT b.*, u.username, u.role
            FROM trading_grid_bots b
            JOIN users u ON u.id=b.user_id
            WHERE b.enabled=1 AND u.status='active'
            ORDER BY b.id ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    results = []
    errors = []
    for row in rows:
        actor = {
            "id": int(row["user_id"]),
            "username": str(row["username"] or "system"),
            "role": str(row["role"] or "user"),
        }
        try:
            results.append(service._scan_one_grid_bot(row, actor=actor))
        except Exception as exc:
            errors.append({"bot_uuid": row["bot_uuid"], "error": str(exc)[:300]})
            conn2 = service.get_db()
            try:
                conn2.execute(
                    "UPDATE trading_grid_bots SET last_error=?, updated_at=? WHERE id=?",
                    (str(exc)[:500], _now_text(), row["id"]),
                )
                conn2.commit()
            finally:
                conn2.close()
    return {"scanned": len(rows), "results": results, "errors": errors}


def _run_bot_trigger_scan(service):
    settings = service.get_root_settings().get("settings") or {}
    limit = max(1, min(int(settings.get("bot_auto_scan_limit") or 50), 200))
    bots = service.run_due_trading_bots(actor=SYSTEM_ACTOR, limit=limit)
    grids = _run_all_grid_bots(service, limit=limit)
    audit_limit = max(1, min(int(settings.get("bot_audit_limit") or 50), 200))
    audits = {"ok": True, "audited": [], "skipped": []}
    if settings.get("bot_audit_enabled", True):
        audits = service.run_due_bot_audits(actor=SYSTEM_ACTOR, limit=audit_limit, force=False)
    return {
        "trading_bots_scanned": bots.get("scanned", 0),
        "trading_bots_triggered_count": len(bots.get("triggered") or []),
        "trading_bots_failed_count": len(bots.get("failed") or []),
        "grid_bots_scanned": grids.get("scanned", 0),
        "grid_bots_error_count": len(grids.get("errors") or []),
        "bot_audits_count": len(audits.get("audited") or []),
        "trading_bots": bots,
        "grid_bots": grids,
        "bot_audits": audits,
    }


def _run_margin_liquidation(service, *, ctx):
    result = service.scan_margin_liquidations(actor=SYSTEM_ACTOR, limit=100, ctx=ctx)
    return {
        "scanned": result.get("scanned", 0),
        "candidate_count": len(result.get("candidates") or []),
        "liquidated_count": len(result.get("liquidated") or []),
        "error_count": len(result.get("errors") or []),
        "result": result,
    }


def _run_interest_accrual(service, *, ctx):
    conn = service.get_db()
    scanned = 0
    accrued = 0
    errors = []
    try:
        service.ensure_schema(conn)
        settings = service._settings_payload(conn)
        if not settings.get("borrowing_enabled"):
            return _skip_result("borrowing_disabled")
        table, route_ctx = service._resolve_table("margin_positions", ctx, action="background_interest_accrual")
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE status='open' ORDER BY id ASC LIMIT 500"
        ).fetchall()
        for row in rows:
            scanned += 1
            before_hours = int(row["interest_accrued_hours"] or 0) if "interest_accrued_hours" in row.keys() else 0
            before_paid = int(row["interest_paid_points"] or 0) if "interest_paid_points" in row.keys() else 0
            before_capitalized = int(row["interest_points"] or 0) if "interest_points" in row.keys() else 0
            try:
                updated = service._accrue_margin_interest(conn, row, actor=SYSTEM_ACTOR, ctx=route_ctx)
                after_hours = int(updated["interest_accrued_hours"] or 0) if "interest_accrued_hours" in updated.keys() else before_hours
                after_paid = int(updated["interest_paid_points"] or 0) if "interest_paid_points" in updated.keys() else before_paid
                after_capitalized = int(updated["interest_points"] or 0) if "interest_points" in updated.keys() else before_capitalized
                if (after_hours, after_paid, after_capitalized) != (before_hours, before_paid, before_capitalized):
                    accrued += 1
            except Exception as exc:
                errors.append({"position_uuid": row["position_uuid"], "error": str(exc)[:300]})
        conn.commit()
        return {"scanned": scanned, "accrued_count": accrued, "error_count": len(errors), "errors": errors}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def refresh_root_trading_snapshots(service, *, source_job_key="sitewide_metrics_refresh", source_run_uuid=""):
    from services.trading.sitewide_reports import build_sitewide_snapshot_payloads

    payloads = build_sitewide_snapshot_payloads(service)
    conn = service.get_db()
    try:
        ensure_background_schema(service, conn)
        conn.commit()
        now = _now_text()
        conn.execute("BEGIN IMMEDIATE")
        for snapshot_key, payload in payloads.items():
            conn.execute(
                """
                INSERT INTO trading_root_snapshots (
                    snapshot_key, payload_json, generated_at,
                    source_job_key, source_run_uuid, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, '', ?)
                ON CONFLICT(snapshot_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    generated_at=excluded.generated_at,
                    source_job_key=excluded.source_job_key,
                    source_run_uuid=excluded.source_run_uuid,
                    error='',
                    updated_at=excluded.updated_at
                """,
                (
                    str(snapshot_key),
                    _json_dumps(payload),
                    now,
                    str(source_job_key or ""),
                    str(source_run_uuid or ""),
                    now,
                ),
            )
        conn.commit()
        return {
            "snapshot_count": len(payloads),
            "snapshot_keys": sorted(payloads.keys()),
            "generated_at": now,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_root_trading_snapshot(service, *, snapshot_key):
    conn = service.get_db()
    try:
        ensure_background_schema(service, conn)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM trading_root_snapshots WHERE snapshot_key=?",
            (str(snapshot_key or ""),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {
            "ok": False,
            "missing": True,
            "snapshot_key": str(snapshot_key or ""),
            "payload": {},
            "msg": "交易報表第一次開啟正在建立快照；root 頁面會自動排入 sitewide_metrics_refresh，背景完成後即可顯示報表。",
        }
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}
    return {
        "ok": True,
        "missing": False,
        "snapshot_key": row["snapshot_key"],
        "payload": payload,
        "generated_at": row["generated_at"],
        "source_job_key": row["source_job_key"],
        "source_run_uuid": row["source_run_uuid"],
        "error": row["error"],
    }


def _execute_job_body(service, *, job_key, server_mode, source_run_uuid=""):
    ctx = _ctx_for_background_mode(server_mode)
    if job_key == "price_refresh":
        return _run_price_refresh(service)
    if job_key == "order_matching":
        return _run_order_matching(service, ctx=ctx)
    if job_key == "take_profit_stop_loss_scan":
        return _run_take_profit_stop_loss(service, ctx=ctx)
    if job_key == "bot_trigger_scan":
        return _run_bot_trigger_scan(service)
    if job_key == "margin_liquidation_scan":
        return _run_margin_liquidation(service, ctx=ctx)
    if job_key == "interest_accrual":
        return _run_interest_accrual(service, ctx=ctx)
    if job_key == "sitewide_metrics_refresh":
        return refresh_root_trading_snapshots(
            service,
            source_job_key=job_key,
            source_run_uuid=source_run_uuid,
        )
    raise ValueError(f"unknown trading background job: {job_key}")


def run_background_job_once(
    service,
    *,
    job_key,
    get_system_settings=None,
    get_runtime_server_mode=None,
    owner=None,
    force=False,
):
    if job_key not in BACKGROUND_JOB_DEFINITIONS:
        raise ValueError(f"unknown trading background job: {job_key}")
    settings = _job_settings(service, get_system_settings)
    server_mode = _runtime_mode(get_runtime_server_mode)
    owner = owner or _lease_owner()
    lease = _acquire_lease(service, job_key=job_key, owner=owner, server_mode=server_mode, force=force)
    if not lease:
        return {"ok": True, "job_key": job_key, "status": "not_due_or_locked"}
    skipped_reason = _should_skip_job(job_key, server_mode=server_mode, settings=settings)
    start = time.monotonic()
    try:
        if skipped_reason:
            result = _skip_result(skipped_reason, server_mode=server_mode)
            _finish_run(service, lease=lease, owner=owner, status="skipped", result=result)
            return {"ok": True, "job_key": job_key, "status": "skipped", "result": result}
        result = _execute_job_body(
            service,
            job_key=job_key,
            server_mode=server_mode,
            source_run_uuid=lease.get("run_uuid") or "",
        )
        result["duration_ms_client"] = round((time.monotonic() - start) * 1000.0, 3)
        result["server_mode"] = server_mode
        _finish_run(service, lease=lease, owner=owner, status="success", result=result)
        return {"ok": True, "job_key": job_key, "status": "success", "result": result}
    except Exception as exc:
        _finish_run(service, lease=lease, owner=owner, status="failed", result={}, error=str(exc))
        raise


def run_due_background_jobs(
    service,
    *,
    get_system_settings=None,
    get_runtime_server_mode=None,
    owner=None,
    job_keys=None,
    queued_max_jobs=None,
):
    ensure_background_schema(service)
    owner = owner or _lease_owner()
    queued_results = process_queued_background_jobs(
        service,
        get_system_settings=get_system_settings,
        get_runtime_server_mode=get_runtime_server_mode,
        owner=owner,
        max_jobs=queued_max_jobs if queued_max_jobs is not None else 10,
    )
    keys = list(BACKGROUND_JOB_DEFINITIONS.keys() if job_keys is None else job_keys)
    results = []
    for job_key in keys:
        try:
            results.append(
                run_background_job_once(
                    service,
                    job_key=job_key,
                    get_system_settings=get_system_settings,
                    get_runtime_server_mode=get_runtime_server_mode,
                    owner=owner,
                    force=False,
                )
            )
        except Exception as exc:
            results.append({"ok": False, "job_key": job_key, "status": "failed", "error": str(exc)[:1000]})
    return {"ok": True, "queued_results": queued_results, "results": results}


def enqueue_background_job_once(service, *, job_key, requested_by=None, force=True):
    if job_key not in BACKGROUND_JOB_DEFINITIONS:
        raise ValueError(f"unknown trading background job: {job_key}")
    requested_by = requested_by or {}
    conn = service.get_db()
    try:
        ensure_background_schema(service, conn)
        now = _now_text()
        queue_uuid = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO trading_background_job_queue (
                queue_uuid, job_key, requested_by_user_id, requested_by_username,
                force, status, requested_at, next_attempt_at
            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                queue_uuid,
                str(job_key),
                requested_by.get("id") if isinstance(requested_by, dict) else None,
                str((requested_by.get("username") if isinstance(requested_by, dict) else "") or ""),
                1 if force else 0,
                now,
                now,
            ),
        )
        conn.commit()
        return {
            "ok": True,
            "accepted": True,
            "queued": True,
            "queue_uuid": queue_uuid,
            "job_key": job_key,
            "status": "queued",
            "requested_at": now,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_queued_background_job(service, *, owner):
    conn = service.get_db()
    try:
        ensure_background_schema(service, conn)
        conn.commit()
        now = _now_text()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT *
            FROM trading_background_job_queue
            WHERE status='queued'
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        conn.execute(
            """
            UPDATE trading_background_job_queue
            SET status='running', started_at=?, attempt_count=attempt_count+1,
                worker_owner=?, error=''
            WHERE id=? AND status='queued'
            """,
            (now, str(owner or ""), int(row["id"])),
        )
        conn.commit()
        claimed = dict(row)
        claimed["started_at"] = now
        claimed["attempt_count"] = int(row["attempt_count"] or 0) + 1
        claimed["worker_owner"] = str(owner or "")
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finish_queued_background_job(service, *, queue_item, status, result=None, error="", retry_after_seconds=0):
    conn = service.get_db()
    try:
        ensure_background_schema(service, conn)
        conn.commit()
        finished = _now_text()
        if status == "queued":
            next_attempt_at = (_parse_dt(finished) + timedelta(seconds=max(1, int(retry_after_seconds or 1)))).isoformat()
            finished_at = None
        else:
            next_attempt_at = None
            finished_at = finished
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE trading_background_job_queue
            SET status=?, finished_at=?, next_attempt_at=?, result_json=?, error=?
            WHERE id=?
            """,
            (
                status,
                finished_at,
                next_attempt_at,
                _json_dumps(result or {}),
                str(error or "")[:1000],
                int(queue_item["id"]),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_queued_background_jobs(
    service,
    *,
    get_system_settings=None,
    get_runtime_server_mode=None,
    owner=None,
    max_jobs=10,
):
    ensure_background_schema(service)
    owner = owner or _lease_owner("trading-bg-queue")
    processed = []
    for _idx in range(max(1, min(int(max_jobs or 3), 10))):
        item = _claim_queued_background_job(service, owner=owner)
        if not item:
            break
        try:
            result = run_background_job_once(
                service,
                job_key=item["job_key"],
                get_system_settings=get_system_settings,
                get_runtime_server_mode=get_runtime_server_mode,
                owner=f"{owner}:queue:{str(item['queue_uuid'])[:8]}",
                force=bool(item.get("force", 1)),
            )
            status = str(result.get("status") or "")
            if status == "not_due_or_locked":
                _finish_queued_background_job(
                    service,
                    queue_item=item,
                    status="queued",
                    result=result,
                    error="job lease not available",
                    retry_after_seconds=5,
                )
                processed.append({"queue_uuid": item["queue_uuid"], **result, "queue_status": "retry_wait"})
                break
            queue_status = "succeeded" if status == "success" else status
            if queue_status not in {"succeeded", "skipped", "failed"}:
                queue_status = "succeeded" if result.get("ok") else "failed"
            _finish_queued_background_job(
                service,
                queue_item=item,
                status=queue_status,
                result=result,
                error=str(result.get("error") or ""),
            )
            processed.append({"queue_uuid": item["queue_uuid"], **result, "queue_status": queue_status})
        except Exception as exc:
            _finish_queued_background_job(
                service,
                queue_item=item,
                status="failed",
                result={},
                error=str(exc),
            )
            processed.append({
                "ok": False,
                "queue_uuid": item["queue_uuid"],
                "job_key": item["job_key"],
                "status": "failed",
                "queue_status": "failed",
                "error": str(exc)[:1000],
            })
    return processed


def get_background_status(service, *, limit=20):
    conn = service.get_db()
    try:
        ensure_background_schema(service, conn)
        conn.commit()
        row_limit = max(1, min(int(limit or 20), 200))

        def read_status_rows():
            jobs_rows = [dict(row) for row in conn.execute("SELECT * FROM trading_background_jobs ORDER BY job_key").fetchall()]
            locks_rows = [dict(row) for row in conn.execute("SELECT * FROM trading_background_locks ORDER BY job_key").fetchall()]
            runs_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM trading_background_job_runs
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (row_limit,),
                ).fetchall()
            ]
            queued_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM trading_background_job_queue
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (row_limit,),
                ).fetchall()
            ]
            return jobs_rows, locks_rows, runs_rows, queued_rows

        jobs, locks, runs, queued = _with_sqlite_background_retry(read_status_rows)
    finally:
        conn.close()
    now_dt = _now_dt()
    for job in jobs:
        job["enabled"] = bool(job.get("enabled"))
        for key in ("run_count", "failure_count", "interval_seconds", "lease_seconds"):
            job[key] = int(job.get(key) or 0)
        try:
            job["last_summary"] = json.loads(job.get("last_summary_json") or "{}")
        except Exception:
            job["last_summary"] = {}
        lease_until = _parse_dt(job.get("lease_until"))
        job["lease_active"] = bool(lease_until and lease_until > now_dt)
    for run in runs:
        try:
            run["result"] = json.loads(run.get("result_json") or "{}")
        except Exception:
            run["result"] = {}
    for item in queued:
        item["force"] = bool(item.get("force"))
        try:
            item["result"] = json.loads(item.get("result_json") or "{}")
        except Exception:
            item["result"] = {}
    return {
        "ok": True,
        "jobs": jobs,
        "locks": locks,
        "recent_runs": runs,
        "queued_runs": queued,
        "server_time": _now_text(),
    }


def set_background_job_enabled(service, *, job_key, enabled, reason="", actor=None):
    if job_key not in BACKGROUND_JOB_DEFINITIONS:
        raise ValueError(f"unknown trading background job: {job_key}")
    conn = service.get_db()
    try:
        ensure_background_schema(service, conn)
        conn.commit()
        now = _now_text()
        conn.execute(
            """
            UPDATE trading_background_jobs
            SET enabled=?, paused_reason=?, updated_at=?
            WHERE job_key=?
            """,
            (1 if enabled else 0, "" if enabled else str(reason or "paused_by_root")[:500], now, job_key),
        )
        try:
            service._audit_event(
                conn,
                "TRADING_BACKGROUND_JOB_ENABLED" if enabled else "TRADING_BACKGROUND_JOB_PAUSED",
                "trading background job state changed",
                actor=actor,
                severity="info" if enabled else "warning",
                metadata={"job_key": job_key, "enabled": bool(enabled), "reason": reason or ""},
            )
        except Exception:
            pass
        conn.commit()
        return {"ok": True, "job_key": job_key, "enabled": bool(enabled)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
