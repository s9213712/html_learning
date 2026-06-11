"""Server startup and background worker orchestration helpers.

The legacy ``server.py`` module remains the Flask entrypoint. This module
owns recurring worker loops plus the ``__main__`` startup sequence so the
entrypoint can converge toward a thinner façade.
"""

import json
import os
import threading
import time
from datetime import datetime

from services.platform.time_settings import normalize_server_timezone


def _int_env(name, default, *, minimum=None, maximum=None):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def _feature_flag_enabled(get_system_settings, key, *, default=False):
    if get_system_settings is None:
        return default
    try:
        settings = get_system_settings() or {}
    except Exception:
        return default
    value = settings.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _setting_bool(settings, key, *, default=False):
    value = (settings or {}).get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _scheduled_admin_salary_week(settings, now=None):
    settings = settings or {}
    if not _setting_bool(settings, "points_admin_weekly_salary_enabled", default=True):
        return None
    if now is None:
        timezone_name = normalize_server_timezone(settings.get("server_timezone"))
        if timezone_name:
            from zoneinfo import ZoneInfo  # imported lazily to keep startup import light

            now = datetime.now(ZoneInfo(timezone_name))
        else:
            now = datetime.now()
    try:
        weekday = int(settings.get("points_admin_weekly_salary_weekday", 1))
    except Exception:
        weekday = 1
    weekday = max(1, min(weekday, 7))
    if int(now.isoweekday()) != weekday:
        return None
    raw_time = str(settings.get("points_admin_weekly_salary_time") or "09:00").strip()
    try:
        hour_text, minute_text = raw_time.split(":", 1)
        payout_hour = int(hour_text)
        payout_minute = int(minute_text)
    except Exception:
        payout_hour, payout_minute = 9, 0
    payout_hour = max(0, min(payout_hour, 23))
    payout_minute = max(0, min(payout_minute, 59))
    if (int(now.hour), int(now.minute)) < (payout_hour, payout_minute):
        return None
    year, week, _weekday = now.isocalendar()
    return f"{int(year)}-W{int(week):02d}"


def _env_flag(name, *, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_flag_from(environ, name, *, default=False):
    raw = (environ or {}).get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def should_start_import_mode_workers(*, module_name, argv=None, environ=None):
    """Return whether a WSGI import should launch server-owned workers.

    ``python server.py`` starts workers from the ``__main__`` path. Gunicorn and
    other WSGI runners import ``server:app`` instead, so their worker processes
    need an import-mode bootstrap. Plain imports from tests, setup scripts, and
    one-off maintenance commands must stay side-effect-light.
    """
    if str(module_name or "") == "__main__":
        return False
    environ = environ or os.environ
    if (
        _env_flag_from(environ, "HTML_LEARNING_DISABLE_IMPORT_WORKERS", default=False)
        or _env_flag_from(environ, "HACKME_DISABLE_IMPORT_WORKERS", default=False)
    ):
        return False
    if (
        _env_flag_from(environ, "HTML_LEARNING_START_IMPORT_WORKERS", default=False)
        or _env_flag_from(environ, "HACKME_START_IMPORT_WORKERS", default=False)
    ):
        return True
    argv_text = " ".join(str(part or "") for part in (argv or [])).lower()
    return any(marker in argv_text for marker in ("gunicorn", "uwsgi", "mod_wsgi"))


def _startup_backtest_probe_enabled(get_system_settings):
    raw_env = os.environ.get("HTML_LEARNING_TRADING_BACKTEST_PROBE_ON_STARTUP")
    if raw_env is not None:
        return _env_flag("HTML_LEARNING_TRADING_BACKTEST_PROBE_ON_STARTUP", default=True)
    if get_system_settings is None:
        return True
    try:
        settings = get_system_settings() or {}
    except Exception:
        return True
    value = settings.get("trading_backtest_capacity_probe_on_startup_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _mode_allows_points_initial_grants(mode):
    return str(mode or "").strip() in {"production", "dev_ready", "test"}


def bootstrap_points_initial_grants_if_due(
    *,
    points_service,
    get_system_settings,
    get_runtime_server_mode=None,
    audit=None,
    env_value=None,
):
    settings = (get_system_settings() or {}) if callable(get_system_settings) else {}
    mode = None
    try:
        if callable(get_runtime_server_mode):
            mode = get_runtime_server_mode()
    except Exception:
        mode = None
    env_enabled = str(
        os.environ.get("HTML_LEARNING_BOOTSTRAP_POINTS_CHAIN", "") if env_value is None else env_value
    ).strip().lower() in {"1", "true", "yes", "on"}
    economy_enabled = _setting_bool(settings, "feature_economy_enabled", default=False)
    points_chain_enabled = _setting_bool(settings, "feature_points_chain_enabled", default=True)
    if not (env_enabled or (economy_enabled and _mode_allows_points_initial_grants(mode))):
        return {
            "ok": True,
            "skipped": True,
            "reason": "economy_disabled_or_mode_not_allowed",
            "mode": mode,
        }
    system_actor = {"username": "system", "role": "system"}
    seal_genesis = str(mode or "").strip() == "production"
    try:
        genesis = points_service.bootstrap_admin_initial_grants(
            actor=system_actor,
            seal_genesis=seal_genesis and points_chain_enabled,
            require_wallet=points_chain_enabled,
        )
        salary_week = _scheduled_admin_salary_week(settings)
        if salary_week:
            salary = points_service.award_admin_weekly_salaries(salary_week=salary_week, actor=system_actor)
        else:
            salary = {"created_count": 0, "salary_week": ""}
        if (genesis.get("created_count") or salary.get("created_count")) and callable(audit):
            audit(
                "POINTS_BOOTSTRAP_GRANTS",
                "0.0.0.0",
                success=True,
                detail=f"genesis={genesis.get('created_count')}, weekly={salary.get('created_count')}, week={salary.get('salary_week')}, mode={mode}",
            )
        return {"ok": True, "skipped": False, "mode": mode, "genesis": genesis, "salary": salary}
    except Exception as exc:
        if callable(audit):
            audit("POINTS_BOOTSTRAP_GRANTS_FAILED", "0.0.0.0", success=False, detail=str(exc))
        return {"ok": False, "skipped": False, "mode": mode, "error": str(exc)}


def _wait_or_stop(shutdown_event, seconds):
    if shutdown_event is not None:
        return shutdown_event.wait(max(0, float(seconds or 0)))
    time.sleep(seconds)
    return False


def start_daily_snapshot_worker(*, snapshot_service, get_system_settings, save_settings, audit, shutdown_event=None):
    interval = _int_env("HTML_LEARNING_SNAPSHOT_CHECK_INTERVAL_SECONDS", 3600, minimum=60)

    def loop():
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            try:
                result = snapshot_service.create_daily_snapshot_if_due(
                    actor={"id": 0, "username": "system"},
                    settings=get_system_settings(),
                    save_settings=save_settings,
                )
                if result.get("created"):
                    audit(
                        "DAILY_SNAPSHOT_CREATED",
                        "0.0.0.0",
                        user="system",
                        success=True,
                        detail=f"snapshot_id={result.get('snapshot_id')}",
                    )
            except Exception as exc:
                audit("DAILY_SNAPSHOT_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
            if _wait_or_stop(shutdown_event, interval):
                break

    worker = threading.Thread(target=loop, name="daily-snapshot-worker", daemon=True)
    worker.start()
    return worker


def start_storage_maintenance_worker(*, get_db, run_storage_maintenance_if_due, get_system_settings, save_settings, audit, storage_root=None, shutdown_event=None):
    interval = _int_env("HTML_LEARNING_STORAGE_MAINTENANCE_CHECK_INTERVAL_SECONDS", 3600, minimum=60)

    def loop():
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            conn = None
            try:
                conn = get_db()
                result = run_storage_maintenance_if_due(
                    conn,
                    settings=get_system_settings(),
                    save_settings=save_settings,
                    actor_user_id=0,
                    storage_root=storage_root,
                )
                if result.get("ran"):
                    conn.commit()
                    audit(
                        "STORAGE_MAINTENANCE_AUTO_RUN",
                        "0.0.0.0",
                        user="system",
                        success=True,
                        detail=str(result.get("result") or {}),
                    )
                else:
                    conn.rollback()
            except Exception as exc:
                if conn:
                    conn.rollback()
                audit("STORAGE_MAINTENANCE_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
            finally:
                if conn:
                    conn.close()
            if _wait_or_stop(shutdown_event, interval):
                break

    worker = threading.Thread(target=loop, name="storage-maintenance-worker", daemon=True)
    worker.start()
    return worker


def start_ai_agent_audit_worker(
    *,
    get_system_settings,
    get_db,
    audit,
    notify_root=None,
    shutdown_event=None,
):
    from services.ai_agent.hermes import (
        AI_AGENT_AUDIT_INTERVAL_MINUTES_DEFAULT,
        AI_AGENT_AUDIT_INTERVAL_MINUTES_MAX,
        normalize_ai_agent_audit_bool,
        normalize_ai_agent_audit_int,
        normalize_ai_agent_operation_mode,
        run_ai_agent_audit_scan,
    )

    def _audit_interval_seconds(settings):
        raw = (settings or {}).get("ai_agent_audit_interval_minutes")
        value = normalize_ai_agent_audit_int(
            raw,
            "ai_agent_audit_interval_minutes",
            default=AI_AGENT_AUDIT_INTERVAL_MINUTES_DEFAULT,
            minimum=1,
            maximum=AI_AGENT_AUDIT_INTERVAL_MINUTES_MAX,
        )
        return max(1, int(value)) * 60

    def _notify_root(scan):
        if not callable(notify_root):
            return
        if not scan.get("notifications"):
            return
        status = str(scan.get("status") or "ok").lower()
        if status == "ok":
            return
        anomaly_count = len(scan.get("anomalies") or [])
        intervention_count = len(scan.get("interventions") or [])
        messages = [
            str(item.get("message") or "").strip()
            for item in (scan.get("anomalies") or [])[:3]
            if str(item.get("message") or "").strip()
        ]
        title = "AI Agent 審計異常"
        body = (
            f"時間：{scan.get('scanned_at', '-')}\n"
            f"狀態：{status}\n"
            f"異常數：{anomaly_count}，自動處置：{intervention_count}\n"
        )
        if messages:
            body += "重點：\n" + "\n".join(f"- {msg}" for msg in messages)
        try:
            notify_root(type="AI_AGENT_AUDIT_ALERT", title=title, body=body, link="/security")
        except Exception:
            pass

    def loop():
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            settings = {}
            try:
                settings = get_system_settings() if callable(get_system_settings) else {}
                operation_mode = normalize_ai_agent_operation_mode(settings.get("ai_agent_audit_operation_mode") or settings.get("ai_agent_operation_mode"))
                if operation_mode != "audit":
                    if _wait_or_stop(shutdown_event, max(30, _audit_interval_seconds(settings))):
                        break
                    continue
                scan = run_ai_agent_audit_scan(
                    settings,
                    get_db=get_db,
                    actor={"id": 0, "username": "system-audit-worker", "role": "super_admin"},
                    get_client_ip=lambda: "0.0.0.0",
                    get_ua=lambda: "ai-agent-audit-worker",
                    audit=audit,
                    force=False,
                )
                if normalize_ai_agent_audit_bool(settings.get("ai_agent_audit_notify_root"), False) and scan.get("status") != "ok":
                    _notify_root(scan)
                if scan.get("status") in {"warn", "alert"}:
                    audit(
                        "AI_AGENT_AUDIT_SCAN_WORKER",
                        "0.0.0.0",
                        user="system-audit-worker",
                        success=scan.get("status") != "alert",
                        detail=f"status={scan.get('status')}, anomalies={len(scan.get('anomalies') or [])}, interventions={len(scan.get('interventions') or [])}",
                    )
                else:
                    audit(
                        "AI_AGENT_AUDIT_SCAN_WORKER",
                        "0.0.0.0",
                        user="system-audit-worker",
                        success=True,
                        detail="status=ok",
                    )
            except Exception as exc:
                audit(
                    "AI_AGENT_AUDIT_SCAN_WORKER_FAILED",
                    "0.0.0.0",
                    user="system-audit-worker",
                    success=False,
                    detail=str(exc),
                )
            if _wait_or_stop(shutdown_event, _audit_interval_seconds(settings)):
                break

    worker = threading.Thread(target=loop, name="ai-agent-audit-worker", daemon=True)
    worker.start()
    return worker


def start_points_chain_block_worker(
    *,
    points_service,
    audit,
    default_block_ledger_threshold,
    default_block_max_interval_seconds,
    get_system_settings=None,
    shutdown_event=None,
):
    check_interval = _int_env("HTML_LEARNING_POINTS_BLOCK_CHECK_INTERVAL_SECONDS", 15, minimum=5)
    ledger_threshold = _int_env(
        "HTML_LEARNING_POINTS_BLOCK_LEDGER_THRESHOLD",
        default_block_ledger_threshold,
        minimum=1,
    )
    max_interval_seconds = _int_env(
        "HTML_LEARNING_POINTS_BLOCK_MAX_INTERVAL_SECONDS",
        default_block_max_interval_seconds,
        minimum=60,
    )

    def loop():
        actor = {"username": "system", "role": "system"}
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            try:
                if not _feature_flag_enabled(get_system_settings, "feature_economy_enabled", default=False):
                    if _wait_or_stop(shutdown_event, check_interval):
                        break
                    continue
                settings = (get_system_settings() or {}) if callable(get_system_settings) else {}
                salary_week = _scheduled_admin_salary_week(settings)
                if salary_week:
                    salary_result = points_service.award_admin_weekly_salaries(
                        salary_week=salary_week,
                        actor=actor,
                    )
                    if salary_result.get("created_count"):
                        audit(
                            "POINTS_ADMIN_WEEKLY_SALARY_AUTO_RUN",
                            "0.0.0.0",
                            user="system",
                            success=True,
                            detail=f"week={salary_result.get('salary_week')},created={salary_result.get('created_count')}",
                        )
                if not _feature_flag_enabled(get_system_settings, "feature_points_chain_enabled", default=True):
                    if _wait_or_stop(shutdown_event, check_interval):
                        break
                    continue
                result = points_service.seal_due_block(
                    actor=actor,
                    ledger_threshold=ledger_threshold,
                    max_interval_seconds=max_interval_seconds,
                    limit=500,
                )
                if result.get("sealed"):
                    block = result.get("block") or {}
                    audit(
                        "POINTS_AUTO_BLOCK_SEALED",
                        "0.0.0.0",
                        user="system",
                        success=True,
                        detail=f"block_number={block.get('block_number')},ledger_count={block.get('ledger_count')}",
                    )
                elif result.get("ok") is False:
                    audit(
                        "POINTS_AUTO_BLOCK_SKIPPED",
                        "0.0.0.0",
                        user="system",
                        success=False,
                        detail=str(result.get("msg") or "verification failed"),
                    )
            except Exception as exc:
                audit("POINTS_AUTO_BLOCK_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
            if _wait_or_stop(shutdown_event, check_interval):
                break

    worker = threading.Thread(target=loop, name="points-chain-block-worker", daemon=True)
    worker.start()
    return worker


def start_trading_background_worker(
    *,
    trading_service,
    audit,
    get_system_settings=None,
    get_runtime_server_mode=None,
    shutdown_event=None,
):
    check_interval = _int_env("HTML_LEARNING_TRADING_BACKGROUND_CHECK_INTERVAL_SECONDS", 2, minimum=1, maximum=60)
    owner = f"trading-bg:{os.getpid()}"

    def loop():
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            try:
                result = trading_service.run_due_background_jobs(
                    get_system_settings=get_system_settings,
                    get_runtime_server_mode=get_runtime_server_mode,
                    owner=owner,
                )
                failures = [
                    row
                    for row in (result.get("results") or [])
                    if not row.get("ok") or row.get("status") == "failed"
                ]
                if failures:
                    audit(
                        "TRADING_BACKGROUND_WORKER_ERRORS",
                        "0.0.0.0",
                        user="system",
                        success=False,
                        detail=json.dumps(failures[:5], ensure_ascii=False, default=str),
                    )
            except Exception as exc:
                audit("TRADING_BACKGROUND_WORKER_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
            if _wait_or_stop(shutdown_event, check_interval):
                break

    try:
        trading_service.ensure_background_schema()
    except Exception as exc:
        audit("TRADING_BACKGROUND_SCHEMA_INIT_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
    worker = threading.Thread(target=loop, name="trading-background-worker", daemon=True)
    worker.start()
    return worker


def start_trading_liquidation_worker(*, trading_service, audit, get_system_settings=None, shutdown_event=None):
    check_interval = _int_env("HTML_LEARNING_TRADING_LIQUIDATION_CHECK_INTERVAL_SECONDS", 30, minimum=10)

    def loop():
        actor = {"username": "system", "role": "system"}
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            try:
                economy_enabled = _feature_flag_enabled(get_system_settings, "feature_economy_enabled", default=False)
                trading_enabled = _feature_flag_enabled(get_system_settings, "feature_trading_enabled", default=False)
                if not (economy_enabled and trading_enabled):
                    if _wait_or_stop(shutdown_event, check_interval):
                        break
                    continue
                match_result = trading_service.match_open_limit_orders(actor=actor, limit=200)
                matched = match_result.get("matched") or []
                match_errors = match_result.get("errors") or []
                if matched:
                    audit(
                        "TRADING_AUTO_LIMIT_MATCH_RUN",
                        "0.0.0.0",
                        user="system",
                        success=True,
                        detail=f"scanned={match_result.get('scanned')}, matched={len(matched)}",
                    )
                if match_errors:
                    audit(
                        "TRADING_AUTO_LIMIT_MATCH_ERRORS",
                        "0.0.0.0",
                        user="system",
                        success=False,
                        detail=json.dumps(match_errors[:5], ensure_ascii=False),
                    )
                spot_target_result = trading_service.scan_spot_risk_targets(actor=actor, limit=200)
                spot_triggered = spot_target_result.get("triggered") or []
                spot_errors = spot_target_result.get("errors") or []
                if spot_triggered:
                    audit(
                        "TRADING_AUTO_SPOT_RISK_TARGET_RUN",
                        "0.0.0.0",
                        user="system",
                        success=True,
                        detail=f"scanned={spot_target_result.get('scanned')}, triggered={len(spot_triggered)}",
                    )
                if spot_errors:
                    audit(
                        "TRADING_AUTO_SPOT_RISK_TARGET_ERRORS",
                        "0.0.0.0",
                        user="system",
                        success=False,
                        detail=json.dumps(spot_errors[:5], ensure_ascii=False),
                    )
                margin_target_result = trading_service.scan_margin_risk_targets(actor=actor, limit=100)
                margin_triggered = margin_target_result.get("triggered") or []
                margin_target_errors = margin_target_result.get("errors") or []
                if margin_triggered:
                    audit(
                        "TRADING_AUTO_MARGIN_RISK_TARGET_RUN",
                        "0.0.0.0",
                        user="system",
                        success=True,
                        detail=f"scanned={margin_target_result.get('scanned')}, triggered={len(margin_triggered)}",
                    )
                if margin_target_errors:
                    audit(
                        "TRADING_AUTO_MARGIN_RISK_TARGET_ERRORS",
                        "0.0.0.0",
                        user="system",
                        success=False,
                        detail=json.dumps(margin_target_errors[:5], ensure_ascii=False),
                    )
                result = trading_service.scan_margin_liquidations(actor=actor, limit=100)
                liquidated = result.get("liquidated") or []
                errors = result.get("errors") or []
                if liquidated:
                    audit(
                        "TRADING_AUTO_LIQUIDATION_RUN",
                        "0.0.0.0",
                        user="system",
                        success=True,
                        detail=f"scanned={result.get('scanned')}, liquidated={len(liquidated)}",
                    )
                if errors:
                    audit(
                        "TRADING_AUTO_LIQUIDATION_ERRORS",
                        "0.0.0.0",
                        user="system",
                        success=False,
                        detail=json.dumps(errors[:5], ensure_ascii=False),
                    )
            except Exception as exc:
                audit("TRADING_AUTO_LIQUIDATION_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
            if _wait_or_stop(shutdown_event, check_interval):
                break

    worker = threading.Thread(target=loop, name="trading-liquidation-worker", daemon=True)
    worker.start()
    return worker


def measure_backtest_capacity_if_needed(*, trading_service, audit):
    """First-boot backtest probe.

    If trading_settings has no ``trading.backtest_capacity_measured_at`` value yet,
    run a small synthetic backtest in a background thread and record the
    projected 60-second capacity. Subsequent boots no-op. Root can re-trigger
    via the admin API endpoint.
    """
    try:
        existing = trading_service.get_backtest_capacity_measurement()
    except Exception as exc:
        audit("TRADING_BACKTEST_CAPACITY_PROBE_SKIPPED", "0.0.0.0", user="system", success=False, detail=str(exc))
        return None
    if existing.get("measured_at"):
        return None

    from services.trading.backtest_capacity import measure_backtest_capacity

    try:
        time_budget = trading_service.get_backtest_capacity_time_budget_seconds()
    except Exception:
        time_budget = 60

    def run_probe():
        try:
            result = measure_backtest_capacity(trading_service=trading_service, time_budget_seconds=time_budget)
            trading_service.record_backtest_capacity_measurement(
                measured_capacity_min=result.get("measured_capacity_min") or 0,
                measured_capacity_max=result.get("measured_capacity_max") or 0,
                measured_at=result.get("measured_at") or "",
                bottleneck_strategy=result.get("bottleneck_strategy") or "",
                fastest_strategy=result.get("fastest_strategy") or "",
                actor_id="system-startup",
            )
            audit(
                "TRADING_BACKTEST_CAPACITY_PROBE_DONE",
                "0.0.0.0",
                user="system",
                success=bool(result.get("measured_capacity_min")),
                detail=json.dumps(result, ensure_ascii=False),
            )
        except Exception as exc:
            audit(
                "TRADING_BACKTEST_CAPACITY_PROBE_FAILED",
                "0.0.0.0",
                user="system",
                success=False,
                detail=str(exc),
            )

    worker = threading.Thread(target=run_probe, name="trading-backtest-capacity-probe", daemon=True)
    worker.start()
    return worker


def start_trading_bot_worker(*, trading_service, audit, get_system_settings=None, shutdown_event=None):
    fallback_interval = _int_env("HTML_LEARNING_TRADING_BOT_SCAN_INTERVAL_SECONDS", 30, minimum=10, maximum=3600)

    def loop():
        actor = {"username": "system", "role": "system"}
        last_audit_started_at = 0.0
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            interval = fallback_interval
            try:
                economy_enabled = _feature_flag_enabled(get_system_settings, "feature_economy_enabled", default=False)
                trading_enabled = _feature_flag_enabled(get_system_settings, "feature_trading_enabled", default=False)
                if not (economy_enabled and trading_enabled):
                    if _wait_or_stop(shutdown_event, interval):
                        break
                    continue
                settings = (trading_service.get_root_settings().get("settings") or {})
                interval = max(10, min(int(settings.get("bot_auto_scan_interval_seconds") or fallback_interval), 3600))
                if not settings.get("enabled", True) or not settings.get("bot_auto_scan_enabled", True):
                    if _wait_or_stop(shutdown_event, interval):
                        break
                    continue
                limit = max(1, min(int(settings.get("bot_auto_scan_limit") or 50), 200))
                result = trading_service.run_due_trading_bots(actor=actor, limit=limit)
                triggered = result.get("triggered") or []
                failed = result.get("failed") or []
                if triggered:
                    audit(
                        "TRADING_BOT_AUTO_SCAN_RUN",
                        "0.0.0.0",
                        user="system",
                        success=True,
                        detail=f"scanned={result.get('scanned')}, triggered={len(triggered)}",
                    )
                if failed:
                    audit(
                        "TRADING_BOT_AUTO_SCAN_ERRORS",
                        "0.0.0.0",
                        user="system",
                        success=False,
                        detail=json.dumps(failed[:5], ensure_ascii=False),
                    )
                audit_enabled = settings.get("bot_audit_enabled", True)
                audit_interval = max(60, min(int(settings.get("bot_audit_interval_seconds") or 300), 86400))
                audit_limit = max(1, min(int(settings.get("bot_audit_limit") or 50), 200))
                now_mono = time.monotonic()
                if audit_enabled and now_mono - last_audit_started_at >= audit_interval:
                    audit_result = trading_service.run_due_bot_audits(actor=actor, limit=audit_limit, force=False)
                    last_audit_started_at = now_mono
                    audited_rows = audit_result.get("audited") or []
                    if audited_rows:
                        audit(
                            "TRADING_BOT_AUDIT_AUTO_RUN",
                            "0.0.0.0",
                            user="system",
                            success=True,
                            detail=f"audited={len(audited_rows)}, skipped={len(audit_result.get('skipped') or [])}",
                        )
            except Exception as exc:
                audit("TRADING_BOT_AUTO_SCAN_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
            if _wait_or_stop(shutdown_event, interval):
                break

    worker = threading.Thread(target=loop, name="trading-bot-worker", daemon=True)
    worker.start()
    return worker


def run_server_main(
    *,
    server_log_path,
    install_runtime_output_capture,
    init_db,
    init_db_kwargs,
    get_db,
    ensure_trading_schema,
    reseal_audit_chain_if_required_on_startup,
    audit,
    server_mode_service,
    points_service,
    get_system_settings,
    integrity_guard,
    start_daily_snapshot_worker,
    start_storage_maintenance_worker,
    start_points_chain_block_worker,
    start_trading_liquidation_worker,
    start_trading_bot_worker,
    start_ai_agent_audit_worker=None,
    start_trading_background_worker=None,
    create_initial_startup_snapshot_if_due=None,
    get_runtime_server_mode=None,
    ensure_local_tls_files,
    cert_file,
    key_file,
    effective_server_ssl,
    effective_server_bind,
    server_bind_state,
    app,
    measure_backtest_capacity_first_boot=None,
    shutdown_event=None,
):
    install_runtime_output_capture(server_log_path)
    init_db(**init_db_kwargs)
    conn = get_db()
    try:
        ensure_trading_schema(conn)
        conn.commit()
    finally:
        conn.close()
    # §18.1 first-boot seeding for ComfyUI workflows.
    # Idempotent: existing runtime customizations stay; only missing
    # workflow_ids get copied from workflows/comfyui/.
    try:
        from services.comfyui.template.seeding import seed_default_comfyui_workflows
        seed_report = seed_default_comfyui_workflows()
        if seed_report.get("copied"):
            audit(
                "COMFYUI_TEMPLATE_SEED",
                "0.0.0.0",
                user="system",
                success=True,
                detail=(
                    f"copied={seed_report['copied']} "
                    f"skipped={seed_report.get('skipped', [])} "
                    f"destination={seed_report['destination']}"
                ),
            )
    except Exception as exc:  # pragma: no cover - seeding must never block boot
        audit(
            "COMFYUI_TEMPLATE_SEED_FAILED",
            "0.0.0.0",
            user="system",
            success=False,
            detail=str(exc),
        )
    try:
        reseal_audit_chain_if_required_on_startup()
    except Exception as exc:
        audit("AUDIT_CHAIN_STARTUP_RESEAL_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
    if create_initial_startup_snapshot_if_due is not None:
        try:
            startup_snapshot = create_initial_startup_snapshot_if_due()
            if startup_snapshot and startup_snapshot.get("created"):
                audit(
                    "INITIAL_STARTUP_SNAPSHOT_CREATED",
                    "0.0.0.0",
                    user="system",
                    success=True,
                    detail=f"snapshot_id={startup_snapshot.get('snapshot_id')}",
                )
        except Exception as exc:
            audit("INITIAL_STARTUP_SNAPSHOT_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
    try:
        recovery = server_mode_service.recover_superweak_on_startup(
            actor={"id": 0, "username": "system-startup", "role": "system"}
        )
        if recovery.get("recovered"):
            audit(
                "SERVER_MODE_SUPERWEAK_STARTUP_RECOVERED",
                "0.0.0.0",
                user="system",
                success=True,
                detail=json.dumps(recovery, ensure_ascii=False, sort_keys=True, default=str),
            )
        elif not recovery.get("ok"):
            audit(
                "SERVER_MODE_SUPERWEAK_STARTUP_RECOVERY_FAILED",
                "0.0.0.0",
                user="system",
                success=False,
                detail=json.dumps(recovery, ensure_ascii=False, sort_keys=True, default=str),
            )
    except Exception as exc:
        audit("SERVER_MODE_SUPERWEAK_STARTUP_RECOVERY_ERROR", "0.0.0.0", user="system", success=False, detail=str(exc))
    bootstrap_points_initial_grants_if_due(
        points_service=points_service,
        get_system_settings=get_system_settings,
        get_runtime_server_mode=get_runtime_server_mode,
        audit=audit,
    )
    if get_system_settings().get("integrity_guard_enabled", True):
        integrity_status = integrity_guard.scan(actor="system-startup", create_initial_manifest=True)
        if get_system_settings().get("integrity_guard_strict_mode", False):
            high_risk = (integrity_status.get("summary") or {}).get("high_risk_pending", 0)
            if high_risk:
                warning = (
                    f"Integrity Guard strict mode detected {high_risk} high risk finding(s) at startup; "
                    "startup continues, but production entry must stay blocked until root reviews them."
                )
                print(f"[integrity-guard] {warning}")
                try:
                    audit(
                        "INTEGRITY_GUARD_STARTUP_WARNING",
                        "0.0.0.0",
                        user="system-startup",
                        success=False,
                        detail=warning,
                    )
                except Exception:
                    pass
    workers = [
        start_daily_snapshot_worker(shutdown_event=shutdown_event),
        start_storage_maintenance_worker(shutdown_event=shutdown_event),
        start_points_chain_block_worker(shutdown_event=shutdown_event),
    ]
    if start_ai_agent_audit_worker is not None:
        workers.append(start_ai_agent_audit_worker(shutdown_event=shutdown_event))
    if start_trading_background_worker is not None:
        workers.append(start_trading_background_worker(shutdown_event=shutdown_event))
    else:
        workers.extend(
            [
                start_trading_liquidation_worker(shutdown_event=shutdown_event),
                start_trading_bot_worker(shutdown_event=shutdown_event),
            ]
        )
    if (
        measure_backtest_capacity_first_boot is not None
        and _startup_backtest_probe_enabled(get_system_settings)
    ):
        try:
            measure_backtest_capacity_first_boot()
        except Exception as exc:
            audit("TRADING_BACKTEST_CAPACITY_PROBE_BOOTSTRAP_FAILED", "0.0.0.0", user="system", success=False, detail=str(exc))
    ensure_local_tls_files(cert_file, key_file)
    has_ssl_files = os.path.exists(cert_file) and os.path.exists(key_file)
    ssl_state = effective_server_ssl(get_system_settings(), cert_exists=has_ssl_files)
    has_ssl = ssl_state["enabled"]
    scheme = ssl_state["scheme"]
    bind = effective_server_bind(get_system_settings())
    host = bind["host"]
    port = bind["port"]
    server_bind_state.update({"host": host, "port": port, "ssl_enabled": has_ssl})
    print(f"\n🌐  hackme_web server running at {scheme}://{host}:{port}")
    if has_ssl:
        ssl_label = "enabled"
    elif ssl_state["enabled_by_setting"] and not has_ssl_files:
        ssl_label = "disabled (runtime/cert.pem + runtime/key.pem missing)"
    else:
        ssl_label = "disabled by root setting"
    print(f"    SSL: {ssl_label}")
    print("    Audit log: database (secure_audit table + hash-chain)")
    print("    Security: Argon2id + timing-noise + account-enum-protection + CSRF + strict-headers\n")
    kwargs = {"host": host, "port": port, "debug": False}
    if has_ssl:
        kwargs["ssl_context"] = (cert_file, key_file)
    try:
        app.run(**kwargs)
    except SystemExit:
        pass
    finally:
        if shutdown_event is not None:
            shutdown_event.set()
            for worker in workers:
                if worker and worker.is_alive():
                    worker.join(timeout=5.0)
