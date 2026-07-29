import multiprocessing
import sqlite3
import time

import pytest

from services.core.sqlite_hardening import connect_sqlite, connect_sqlite_readonly, sqlite_busy_timeout_ms


def _concurrent_process_writer(database_path, start_event, result_queue, writer_id):
    """Exercise the file-backed writer gate from an independent process."""

    conn = connect_sqlite(database_path)
    failures = []
    try:
        if not start_event.wait(timeout=5):
            raise RuntimeError("concurrent writer start gate timed out")
        for sequence in range(20):
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO sample (writer_id, sequence) VALUES (?, ?)",
                    (writer_id, sequence),
                )
                # Keep the transaction open long enough that the other process
                # must contend for the same cross-process writer reservation.
                time.sleep(0.004)
                conn.commit()
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
                try:
                    conn.rollback()
                except Exception:
                    pass
    finally:
        conn.close()
    result_queue.put(failures)


def test_sqlite_busy_timeout_default_matches_web_connection_budget(monkeypatch):
    monkeypatch.delenv("HACKME_SQLITE_BUSY_TIMEOUT_MS", raising=False)

    assert sqlite_busy_timeout_ms() == 15000


def test_hardened_sqlite_applies_runtime_pragmas(tmp_path):
    db_path = tmp_path / "hardening.db"
    conn = connect_sqlite(db_path)
    try:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample (value) VALUES ('ok')")
        conn.commit()
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
        assert conn.execute("PRAGMA temp_store").fetchone()[0] in {1, 2}
        assert conn.execute("PRAGMA cache_size").fetchone()[0] < 0
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="the production cross-process writer lock is POSIX-specific",
)
def test_hardened_sqlite_serializes_concurrent_process_writers(tmp_path):
    db_path = tmp_path / "concurrent-process-writers.db"
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            "CREATE TABLE sample (id INTEGER PRIMARY KEY, writer_id INTEGER NOT NULL, sequence INTEGER NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    context = multiprocessing.get_context("fork")
    start_event = context.Event()
    result_queue = context.Queue()
    writers = [
        context.Process(
            target=_concurrent_process_writer,
            args=(str(db_path), start_event, result_queue, writer_id),
        )
        for writer_id in (1, 2)
    ]
    for writer in writers:
        writer.start()
    start_event.set()
    for writer in writers:
        writer.join(timeout=10)
        assert writer.exitcode == 0

    failures = [result_queue.get(timeout=2) for _ in writers]
    assert failures == [[], []]

    verify = connect_sqlite_readonly(db_path)
    try:
        assert verify.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 40
        assert verify.execute(
            "SELECT COUNT(*) FROM (SELECT writer_id, sequence FROM sample GROUP BY writer_id, sequence)"
        ).fetchone()[0] == 40
    finally:
        verify.close()


def test_readonly_sqlite_connection_allows_reads_and_blocks_writes(tmp_path):
    db_path = tmp_path / "readonly.db"
    conn = connect_sqlite(db_path)
    try:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample (value) VALUES ('ok')")
        conn.commit()
    finally:
        conn.close()

    ro = connect_sqlite_readonly(db_path)
    try:
        assert ro.execute("SELECT value FROM sample WHERE id=1").fetchone()["value"] == "ok"
        assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO sample (value) VALUES ('no')")
            ro.commit()
    finally:
        ro.close()


def test_hardened_sqlite_retries_schema_changed_transient(tmp_path, monkeypatch):
    db_path = tmp_path / "schema_changed_retry.db"
    monkeypatch.setenv("HACKME_SQLITE_LOCK_RETRY_ATTEMPTS", "3")
    conn = connect_sqlite(db_path)
    calls = {"count": 0}

    def flaky_operation():
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("database schema has changed")
        return "ok"

    try:
        assert conn._with_locked_retry(flaky_operation) == "ok"
        assert calls["count"] == 2
    finally:
        conn.close()
