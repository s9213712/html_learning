import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from services.points_chain import PointsLedgerService, ensure_points_economy_schema
from services.job_center import list_jobs
from services.server.startup import start_trading_background_worker
from services.trading.trading_engine import TradingEngineService, ensure_trading_schema


def _db(tmp_path):
    path = tmp_path / "trading-background.db"

    def get_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    conn = get_db()
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active')"
    )
    conn.execute(
        "INSERT INTO users (username, role, status) VALUES "
        "('alice', 'user', 'active'), "
        "('root', 'super_admin', 'active')"
    )
    ensure_points_economy_schema(conn)
    ensure_trading_schema(conn)
    conn.commit()
    conn.close()
    return get_db


def _actor(user_id=1, username="alice", role="user"):
    return {"id": user_id, "username": username, "role": role}


def _settings():
    return {"feature_economy_enabled": True, "feature_trading_enabled": True}


def _set_trading_setting(trading, key, value):
    conn = trading.get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (str(key), str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def _stamp_boot_ready(trading):
    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        conn.execute(
            "UPDATE trading_markets SET live_price_confirmed_at=COALESCE(live_price_confirmed_at, ?)",
            ("2026-05-01T00:00:00",),
        )
        conn.commit()
    finally:
        conn.close()


def _services(tmp_path):
    get_db = _db(tmp_path)
    prices = {"BTC/POINTS": 77059, "ETH/POINTS": 5000}
    points = PointsLedgerService(get_db=get_db, chain_secret="background-test", backup_dir=tmp_path / "points-chain")
    trading = TradingEngineService(
        get_db=get_db,
        points_service=points,
        live_price_provider=lambda symbol: prices[symbol],
    )
    trading.test_prices = prices
    _set_trading_setting(trading, "trading.price_source", "binance_public_api")
    _stamp_boot_ready(trading)
    return points, trading


def test_background_price_refresh_runs_without_logged_in_actor(tmp_path):
    _points, trading = _services(tmp_path)

    result = trading.run_background_job_once(
        job_key="price_refresh",
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "production",
        owner="unit-test",
        force=True,
    )

    assert result["status"] == "success"
    assert result["result"]["refreshed_count"] >= 1
    status = trading.get_background_status()
    price_job = next(row for row in status["jobs"] if row["job_key"] == "price_refresh")
    assert price_job["last_success_at"]
    assert price_job["failure_count"] == 0
    conn = trading.get_db()
    try:
        jobs = list_jobs(conn, include_all=True, limit=20)
    finally:
        conn.close()
    visible = next(row for row in jobs if row["source_module"] == "trading_background" and row["source_ref"] == "price_refresh")
    assert visible["owner_user_id"] is None
    assert visible["status"] == "running"
    assert visible["metadata"]["server_background"] is True
    assert visible["metadata"]["login_required"] is False


def test_background_worker_thread_runs_without_any_login_session(tmp_path, monkeypatch):
    _points, trading = _services(tmp_path)
    monkeypatch.setenv("HTML_LEARNING_TRADING_BACKGROUND_CHECK_INTERVAL_SECONDS", "1")
    audit_events = []
    stop = threading.Event()

    worker = start_trading_background_worker(
        trading_service=trading,
        audit=lambda *args, **kwargs: audit_events.append((args, kwargs)),
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "production",
        shutdown_event=stop,
    )
    deadline = time.time() + 5
    try:
        while time.time() < deadline:
            status = trading.get_background_status()
            price_job = next(row for row in status["jobs"] if row["job_key"] == "price_refresh")
            if price_job["last_success_at"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("background worker did not refresh prices")
    finally:
        stop.set()
        worker.join(timeout=3)

    status = trading.get_background_status()
    assert any(row["job_key"] == "price_refresh" and row["status"] == "success" for row in status["recent_runs"])
    assert not [event for event in audit_events if event[0] and "FAILED" in str(event[0][0])]


def test_background_interest_accrual_runs_without_logged_in_actor(tmp_path):
    _points, trading = _services(tmp_path)
    trading.update_root_settings(
        actor=_actor(2, "root", "super_admin"),
        settings={"borrowing_enabled": True, "borrow_interest_percent_daily": 24},
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    opened_at = (datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) - timedelta(hours=2, minutes=1)).isoformat()
    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_margin_positions SET opened_at=?, interest_accrued_hours=0, interest_points=0, interest_paid_points=0, interest_carry_micropoints=0 WHERE position_uuid=?",
            (opened_at, opened["position"]["position_uuid"]),
        )
        conn.commit()
    finally:
        conn.close()

    result = trading.run_background_job_once(
        job_key="interest_accrual",
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "production",
        owner="unit-test",
        force=True,
    )

    assert result["status"] == "success"
    assert result["result"]["scanned"] >= 1
    assert result["result"]["accrued_count"] >= 1
    conn = trading.get_db()
    try:
        row = conn.execute(
            """
            SELECT interest_accrued_hours, interest_points, interest_paid_points, interest_carry_micropoints
            FROM trading_margin_positions
            WHERE position_uuid=?
            """,
            (opened["position"]["position_uuid"],),
        ).fetchone()
    finally:
        conn.close()
    assert int(row["interest_accrued_hours"]) >= 2
    assert int(row["interest_points"] or 0) == 0
    assert int(row["interest_paid_points"] or 0) == 0
    assert int(row["interest_carry_micropoints"] or 0) > 0


def test_background_lease_prevents_second_owner_from_running_same_job(tmp_path):
    _points, trading = _services(tmp_path)
    trading.ensure_background_schema()
    future = (datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) + timedelta(minutes=5)).isoformat()
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()
    conn = trading.get_db()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO trading_background_locks (
                job_key, lease_owner, lease_until, acquired_at, renewed_at
            ) VALUES ('price_refresh', 'other-owner', ?, ?, ?)
            """,
            (future, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    result = trading.run_background_job_once(
        job_key="price_refresh",
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "production",
        owner="second-owner",
        force=True,
    )

    assert result["status"] == "not_due_or_locked"
    status = trading.get_background_status()
    price_job = next(row for row in status["jobs"] if row["job_key"] == "price_refresh")
    assert price_job["run_count"] == 0


def test_background_paused_mode_records_skipped_not_failure(tmp_path):
    _points, trading = _services(tmp_path)

    result = trading.run_background_job_once(
        job_key="order_matching",
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "maintenance",
        owner="unit-test",
        force=True,
    )

    assert result["status"] == "skipped"
    assert result["result"]["reason"] == "server_mode_maintenance_paused"
    status = trading.get_background_status()
    job = next(row for row in status["jobs"] if row["job_key"] == "order_matching")
    assert job["run_count"] == 1
    assert job["failure_count"] == 0


def test_background_dev_ready_is_disabled_by_default(tmp_path):
    _points, trading = _services(tmp_path)

    result = trading.run_background_job_once(
        job_key="price_refresh",
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "dev_ready",
        owner="unit-test",
        force=True,
    )

    assert result["status"] == "skipped"
    assert result["result"]["reason"] == "server_mode_dev_ready_background_worker_disabled_by_default"
    status = trading.get_background_status()
    job = next(row for row in status["jobs"] if row["job_key"] == "price_refresh")
    assert job["failure_count"] == 0


def test_sitewide_metrics_refresh_runs_in_dev_ready_for_root_reports(tmp_path):
    _points, trading = _services(tmp_path)

    result = trading.run_background_job_once(
        job_key="sitewide_metrics_refresh",
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "dev_ready",
        owner="unit-test",
        force=True,
    )

    assert result["status"] == "success"
    assert "root_report" in result["result"]["snapshot_keys"]
    snapshot = trading.get_root_trading_snapshot(snapshot_key="root_report")
    assert snapshot["ok"] is True
    assert snapshot["source_job_key"] == "sitewide_metrics_refresh"


def test_background_dev_ready_can_be_enabled_for_isolated_qa(tmp_path):
    _points, trading = _services(tmp_path)
    _set_trading_setting(trading, "trading.background_worker_dev_ready_enabled", "true")

    result = trading.run_background_job_once(
        job_key="price_refresh",
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "dev_ready",
        owner="unit-test",
        force=True,
    )

    assert result["status"] == "success"
    assert result["result"]["refreshed_count"] >= 1
    status = trading.get_background_status()
    job = next(row for row in status["jobs"] if row["job_key"] == "price_refresh")
    assert job["failure_count"] == 0
    assert job["last_success_at"]


def test_background_sitewide_metrics_refresh_writes_root_snapshots(tmp_path):
    _points, trading = _services(tmp_path)

    result = trading.run_background_job_once(
        job_key="sitewide_metrics_refresh",
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "production",
        owner="unit-test",
        force=True,
    )

    assert result["status"] == "success"
    assert "root_report" in result["result"]["snapshot_keys"]
    pools = trading.get_root_trading_snapshot(snapshot_key="sitewide_pools")
    positions = trading.get_root_trading_snapshot(snapshot_key="sitewide_user_positions")
    assert pools["ok"] is True
    assert positions["ok"] is True
    assert pools["source_run_uuid"]
    assert pools["payload"]["pools"]["snapshot_backed"] is True


def test_background_run_once_queue_is_processed_by_worker_loop(tmp_path):
    _points, trading = _services(tmp_path)
    queued = trading.enqueue_background_job_once(
        job_key="sitewide_metrics_refresh",
        requested_by={"id": 2, "username": "root"},
        force=True,
    )

    result = trading.run_due_background_jobs(
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "production",
        owner="unit-test",
        job_keys=[],
    )

    assert result["queued_results"][0]["queue_uuid"] == queued["queue_uuid"]
    assert result["queued_results"][0]["queue_status"] == "succeeded"
    status = trading.get_background_status()
    queued_row = next(row for row in status["queued_runs"] if row["queue_uuid"] == queued["queue_uuid"])
    assert queued_row["status"] == "succeeded"


def test_background_queue_drains_more_than_legacy_three_job_batch(tmp_path):
    _points, trading = _services(tmp_path)
    queued = [
        trading.enqueue_background_job_once(
            job_key="sitewide_metrics_refresh",
            requested_by={"id": 2, "username": "root"},
            force=True,
        )
        for _ in range(5)
    ]

    result = trading.run_due_background_jobs(
        get_system_settings=_settings,
        get_runtime_server_mode=lambda: "production",
        owner="unit-test",
        job_keys=[],
    )

    succeeded = [row for row in result["queued_results"] if row.get("queue_status") == "succeeded"]
    assert len(succeeded) == 5
    status = trading.get_background_status(limit=20)
    queued_rows = {
        row["queue_uuid"]: row["status"]
        for row in status["queued_runs"]
        if row["queue_uuid"] in {item["queue_uuid"] for item in queued}
    }
    assert queued_rows == {item["queue_uuid"]: "succeeded" for item in queued}
