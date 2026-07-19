import contextlib
import json
import multiprocessing
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.server.database import get_audit_db
from services.server.routes import OPERATION_ROUTE_KEYS
from services.system import audit as audit_service


def _write_anchor_from_process(anchor_path, latest_path, audit_id, start):
    audit_service.configure_audit_service(
        get_db=lambda: None,
        chain_seed="unused",
        integrity_key=b"unused",
        audit_log_path=str(anchor_path) + ".unused",
        audit_anchor_path=str(anchor_path),
        audit_anchor_latest_path=str(latest_path),
        audit_anchor_interval_seconds=0,
    )
    start.wait(timeout=15)
    audit_service._write_audit_anchor(
        audit_id,
        f"chain-{audit_id}",
        f"entry-{audit_id}",
        reason="cross-process-regression",
    )


def test_get_audit_db_creates_secure_audit_schema(tmp_path):
    audit_db_path = tmp_path / "audit.db"
    conn = get_audit_db(str(audit_db_path))
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(secure_audit)").fetchall()}
    finally:
        conn.close()
    assert {"id", "ts", "action", "chain_hash", "prev_hash", "entry_hash"} <= cols


def test_operation_routes_receive_split_audit_db_dependency():
    assert "get_audit_db" in OPERATION_ROUTE_KEYS


def test_audit_service_writes_to_split_audit_db_only(tmp_path):
    main_db_path = tmp_path / "database.db"
    audit_db_path = tmp_path / "audit.db"
    sqlite3.connect(main_db_path).close()

    def _get_audit_db():
        return get_audit_db(str(audit_db_path))

    audit_log_path = tmp_path / "audit.log"
    anchor_path = tmp_path / "audit_head.jsonl"
    anchor_latest_path = tmp_path / "audit_head_latest.json"
    audit_service.configure_audit_service(
        get_db=_get_audit_db,
        chain_seed="seed-chain-hash",
        integrity_key=b"integrity-key-for-tests",
        audit_log_path=str(audit_log_path),
        audit_anchor_path=str(anchor_path),
        audit_anchor_latest_path=str(anchor_latest_path),
        audit_anchor_interval_seconds=60,
    )

    audit_service.audit("LOGIN_OK", "127.0.0.1", user="root", success=True, ua="pytest", detail="split-db")

    main_conn = sqlite3.connect(main_db_path)
    try:
        main_table = main_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='secure_audit' LIMIT 1"
        ).fetchone()
    finally:
        main_conn.close()
    assert main_table is None

    audit_conn = get_audit_db(str(audit_db_path))
    try:
        row = audit_conn.execute(
            "SELECT action, user, success, detail FROM secure_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        audit_conn.close()
    assert row is not None
    assert row["action"] == "LOGIN_OK"
    assert row["user"] == "root"
    assert int(row["success"]) == 1
    assert row["detail"] == "split-db"


def test_audit_appends_are_atomic_across_concurrent_connections(tmp_path, monkeypatch):
    """Simulate independent server workers that do not share a Python lock."""

    audit_db_path = tmp_path / "audit.db"
    audit_log_path = tmp_path / "audit.log"
    anchor_path = tmp_path / "audit_head.jsonl"
    anchor_latest_path = tmp_path / "audit_head_latest.json"
    workers = 16
    start = threading.Barrier(workers)

    # Create/migrate the table before contention starts.  Each audit append
    # below still receives its own real SQLite connection.
    get_audit_db(str(audit_db_path)).close()

    class SlowHeadReadConnection:
        """Widen the old SELECT-before-write race without mocking SQLite."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            cursor = self._conn.execute(sql, *args)
            if sql.lstrip().upper().startswith("SELECT CHAIN_HASH FROM SECURE_AUDIT"):
                time.sleep(0.025)
            return cursor

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def _get_audit_db():
        return SlowHeadReadConnection(get_audit_db(str(audit_db_path)))

    audit_service.configure_audit_service(
        get_db=_get_audit_db,
        chain_seed="concurrent-seed",
        integrity_key=b"concurrent-integrity-key",
        audit_log_path=str(audit_log_path),
        audit_anchor_path=str(anchor_path),
        audit_anchor_latest_path=str(anchor_latest_path),
        audit_anchor_interval_seconds=60,
    )
    # Bypass both higher-level process/file guards here to prove the database
    # transaction itself serializes the real append path.
    monkeypatch.setattr(audit_service, "_audit_db_lock", contextlib.nullcontext())
    monkeypatch.setattr(
        audit_service,
        "_audit_mutation_guard",
        lambda: contextlib.nullcontext(),
    )
    monkeypatch.setattr(audit_service, "_last_audit_anchor_at", 0.0)

    def append(worker_id):
        start.wait(timeout=10)
        audit_service.audit(
            "CONCURRENT_APPEND",
            "127.0.0.1",
            user=f"worker-{worker_id}",
            success=True,
            ua="pytest",
            detail=f"connection-{worker_id}",
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(append, worker_id) for worker_id in range(workers)]
        for future in futures:
            future.result(timeout=30)

    conn = get_audit_db(str(audit_db_path))
    try:
        rows = conn.execute(
            "SELECT id, prev_hash, chain_hash FROM secure_audit ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == workers
    expected_prev_hash = "concurrent-seed"
    for row in rows:
        assert row["prev_hash"] == expected_prev_hash
        expected_prev_hash = row["chain_hash"]

    ok, broken_at, details = audit_service.verify_audit_integrity()
    assert ok is True, details
    assert broken_at is None


def test_mutation_lock_keeps_db_and_jsonl_in_audit_id_order(tmp_path, monkeypatch):
    audit_db_path = tmp_path / "audit.db"
    audit_log_path = tmp_path / "audit.log"
    workers = 12
    start = threading.Barrier(workers)
    get_audit_db(str(audit_db_path)).close()
    audit_service.configure_audit_service(
        get_db=lambda: get_audit_db(str(audit_db_path)),
        chain_seed="ordered-evidence-seed",
        integrity_key=b"ordered-evidence-integrity-key",
        audit_log_path=str(audit_log_path),
        audit_anchor_path=str(tmp_path / "audit_head.jsonl"),
        audit_anchor_latest_path=str(tmp_path / "audit_head_latest.json"),
        audit_anchor_interval_seconds=0,
    )
    # Independent worker processes do not share this Python mutex.  The
    # dedicated flock must preserve DB/file ordering on its own.
    monkeypatch.setattr(audit_service, "_audit_lock", contextlib.nullcontext())

    def append(worker_id):
        start.wait(timeout=10)
        audit_service.audit(
            "ORDERED_EVIDENCE",
            "127.0.0.1",
            user=f"worker-{worker_id}",
            success=True,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(append, worker_id) for worker_id in range(workers)]
        for future in futures:
            future.result(timeout=30)

    entries = [
        json.loads(line)
        for line in audit_log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["_audit_id"] for entry in entries] == list(range(1, workers + 1))
    expected_prev_hash = "ordered-evidence-seed"
    for entry in entries:
        assert entry["_prev_hash"] == expected_prev_hash
        expected_prev_hash = entry["_chain_hash"]
    assert audit_service.verify_audit_integrity()[0] is True


def test_audit_hashes_the_same_truncated_user_agent_that_it_stores(tmp_path):
    audit_db_path = tmp_path / "audit.db"

    audit_service.configure_audit_service(
        get_db=lambda: get_audit_db(str(audit_db_path)),
        chain_seed="long-ua-seed",
        integrity_key=b"long-ua-integrity-key",
        audit_log_path=str(tmp_path / "audit.log"),
        audit_anchor_path=str(tmp_path / "audit_head.jsonl"),
        audit_anchor_latest_path=str(tmp_path / "audit_head_latest.json"),
        audit_anchor_interval_seconds=60,
    )
    audit_service.audit(
        "LONG_USER_AGENT",
        "127.0.0.1",
        user="root",
        success=True,
        ua="u" * 500,
        detail="long-ua",
    )

    conn = get_audit_db(str(audit_db_path))
    try:
        stored_ua = conn.execute("SELECT ua FROM secure_audit").fetchone()["ua"]
    finally:
        conn.close()
    assert stored_ua == "u" * 200

    ok, broken_at, details = audit_service.verify_audit_integrity()
    assert ok is True, details
    assert broken_at is None


@pytest.mark.parametrize("failure", ["database is locked", "disk I/O error"])
def test_audit_does_not_treat_operational_failures_as_legacy_schema(tmp_path, failure):
    class Cursor:
        def fetchone(self):
            return None

    class FailingConnection:
        def __init__(self):
            self.insert_attempts = 0
            self.rolled_back = False

        def execute(self, sql, *_args):
            if sql.lstrip().upper().startswith("INSERT INTO SECURE_AUDIT"):
                self.insert_attempts += 1
                raise sqlite3.OperationalError(failure)
            return Cursor()

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    conn = FailingConnection()
    audit_service.configure_audit_service(
        get_db=lambda: conn,
        chain_seed="failure-seed",
        integrity_key=b"failure-integrity-key",
        audit_log_path=str(tmp_path / "audit.log"),
        audit_anchor_path=str(tmp_path / "audit_head.jsonl"),
        audit_anchor_latest_path=str(tmp_path / "audit_head_latest.json"),
        audit_anchor_interval_seconds=60,
    )

    with pytest.raises(sqlite3.OperationalError, match=failure.replace("I/O", "I/O")):
        audit_service.audit("FAIL_CLOSED", "127.0.0.1")

    assert conn.insert_attempts == 1
    assert conn.rolled_back is True


def test_audit_anchor_is_cross_process_safe_and_latest_is_monotonic(tmp_path):
    anchor_path = tmp_path / "audit_head.jsonl"
    latest_path = tmp_path / "audit_head_latest.json"
    worker_count = 12
    ctx = multiprocessing.get_context("fork")
    start = ctx.Barrier(worker_count)
    # Descending IDs make a late low-ID writer likely; correctness must not
    # depend on process scheduling order.
    processes = [
        ctx.Process(
            target=_write_anchor_from_process,
            args=(anchor_path, latest_path, audit_id, start),
        )
        for audit_id in range(worker_count, 0, -1)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    assert all(not process.is_alive() for process in processes)
    assert [process.exitcode for process in processes] == [0] * worker_count
    anchors = [
        json.loads(line)
        for line in anchor_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(anchors) == worker_count
    assert {anchor["audit_id"] for anchor in anchors} == set(range(1, worker_count + 1))
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    assert latest["audit_id"] == worker_count
    assert latest["chain_hash"] == f"chain-{worker_count}"
    assert list(tmp_path.glob("audit_head_latest.json.*.tmp")) == []
