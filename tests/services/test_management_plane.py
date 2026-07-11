import sqlite3
import threading
import time

from services.job_center import get_job
from services.management_plane import get_management_snapshot, start_management_plane_job


def _db_factory(path):
    def get_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    return get_db


def _wait_for_job(get_db, job_uuid, *, timeout=3.0):
    deadline = time.time() + timeout
    last_job = None
    while time.time() < deadline:
        conn = get_db()
        try:
            last_job = get_job(conn, job_uuid)
        finally:
            conn.close()
        if last_job and last_job.get("status") in {"succeeded", "failed", "cancelled", "expired"}:
            return last_job
        time.sleep(0.02)
    return last_job


def test_management_plane_job_reuses_active_and_recent_success_with_queue_metadata(tmp_path):
    get_db = _db_factory(tmp_path / "management_plane.db")
    calls = []

    def worker(progress):
        calls.append(time.time())
        progress(stage="unit_work", progress_percent=50, detail="unit worker running")
        time.sleep(0.05)
        return {"ok": True, "value": len(calls)}

    first = start_management_plane_job(
        get_db=get_db,
        actor={"id": 1, "username": "root", "role": "super_admin"},
        job_type="unit_management",
        title="Unit management",
        snapshot_key="unit_snapshot",
        request_payload={"reason": "unit"},
        worker=worker,
        queue_class="Trading Admin",
        resource_locks=("finance_db", "finance_db", "points chain"),
        reuse_recent_success_seconds=30,
    )
    second = start_management_plane_job(
        get_db=get_db,
        actor={"id": 1, "username": "root", "role": "super_admin"},
        job_type="unit_management",
        title="Unit management",
        snapshot_key="unit_snapshot",
        request_payload={"reason": "unit"},
        worker=worker,
        queue_class="Trading Admin",
        resource_locks=("finance_db",),
        reuse_recent_success_seconds=30,
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["job"]["job_uuid"] == first["job"]["job_uuid"]

    job = _wait_for_job(get_db, first["job"]["job_uuid"])
    assert job["status"] == "succeeded"
    assert job["metadata"]["queue_class"] == "trading_admin"
    assert job["metadata"]["resource_locks"] == ["finance_db", "points_chain"]
    assert len(calls) == 1

    conn = get_db()
    try:
        snapshot = get_management_snapshot(conn, snapshot_key="unit_snapshot", include_payload=True)
    finally:
        conn.close()
    assert snapshot["ok"] is True
    assert snapshot["payload"]["value"] == 1

    third = start_management_plane_job(
        get_db=get_db,
        actor={"id": 1, "username": "root", "role": "super_admin"},
        job_type="unit_management",
        title="Unit management",
        snapshot_key="unit_snapshot",
        request_payload={"reason": "unit"},
        worker=worker,
        queue_class="Trading Admin",
        resource_locks=("finance_db",),
        reuse_recent_success_seconds=30,
    )

    assert third["created"] is False
    assert third["job"]["job_uuid"] == first["job"]["job_uuid"]
    assert third["job"]["metadata"]["reused_recent_success"] is True
    assert len(calls) == 1


def test_management_plane_enqueue_retries_transient_sqlite_write_lock(tmp_path, monkeypatch):
    db_path = tmp_path / "management_plane_locked.db"
    connect_timeout = {"seconds": 0.01}

    def get_db():
        conn = sqlite3.connect(db_path, timeout=connect_timeout["seconds"])
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setenv("HACKME_SQLITE_LOCK_RETRY_ATTEMPTS", "5")
    monkeypatch.setenv("HACKME_SQLITE_LOCK_RETRY_BASE_SLEEP", "0.02")
    locker = sqlite3.connect(db_path, timeout=0, check_same_thread=False)
    locker.execute("CREATE TABLE lock_seed (id INTEGER PRIMARY KEY)")
    locker.commit()
    locker.execute("BEGIN IMMEDIATE")

    def release_lock():
        time.sleep(0.08)
        locker.rollback()
        locker.close()

    release_thread = threading.Thread(target=release_lock, daemon=True)
    release_thread.start()
    started = start_management_plane_job(
        get_db=get_db,
        actor={"id": 1, "username": "root", "role": "super_admin"},
        job_type="locked_management",
        title="Locked management",
        snapshot_key="locked_snapshot",
        request_payload={},
        worker=lambda progress: {"ok": True, "value": "released"},
        queue_class="management",
        resource_locks=("finance_db",),
    )
    release_thread.join(timeout=1)

    assert started["created"] is True
    connect_timeout["seconds"] = 1.0
    job = _wait_for_job(get_db, started["job"]["job_uuid"])
    assert job["status"] == "succeeded"
