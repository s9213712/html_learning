import contextlib
import json
import sqlite3
import threading
import time

import services.system.audit as audit_service
from services.system.audit import audit, configure_audit_service, repair_audit_chain, reset_audit_chain_with_event, verify_audit_integrity
from services.governance.violations import (
    configure_violations_service,
    repair_violation_chains,
    secure_add_violation,
    verify_violation_integrity,
)


def _get_db_factory(db_path):
    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return get_db


def test_repair_audit_chain_reseals_corrupted_entries(tmp_path):
    db_path = tmp_path / "audit.db"
    audit_log = tmp_path / "audit.log"
    anchor_log = tmp_path / "audit_head.jsonl"
    anchor_latest = tmp_path / "audit_head_latest.json"

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    configure_audit_service(
        get_db=_get_db_factory(str(db_path)),
        chain_seed="seed",
        integrity_key=b"test-integrity-key",
        audit_log_path=str(audit_log),
        audit_anchor_path=str(anchor_log),
        audit_anchor_latest_path=str(anchor_latest),
        audit_anchor_interval_seconds=0,
    )
    audit("FIRST", "127.0.0.1", user="root", success=True, detail="ok")
    audit("SECOND", "127.0.0.1", user="root", success=True, detail="ok")
    assert verify_audit_integrity()[0] is True

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE secure_audit SET detail='tampered' WHERE id=1")
    conn.commit()
    conn.close()
    assert verify_audit_integrity()[0] is False

    result = repair_audit_chain(reason="test")

    assert result["entries_resealed"] == 2
    ok, broken_at, _ = verify_audit_integrity()
    assert ok is True
    assert broken_at is None


def test_verify_audit_integrity_range_uses_the_predecessor_chain_hash(tmp_path):
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    configure_audit_service(
        get_db=_get_db_factory(str(db_path)),
        chain_seed="range-seed",
        integrity_key=b"range-integrity-key",
        audit_log_path=str(tmp_path / "audit.log"),
        audit_anchor_path=str(tmp_path / "audit_head.jsonl"),
        audit_anchor_latest_path=str(tmp_path / "audit_head_latest.json"),
        audit_anchor_interval_seconds=3600,
    )
    audit("FIRST", "127.0.0.1", success=True)
    audit("SECOND", "127.0.0.1", success=True)

    ok, broken_at, details = verify_audit_integrity(start_id=2, end_id=2)
    assert ok is True, details
    assert broken_at is None
    assert "range integrity OK" in details
    assert "starts after audit id=1" in details

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE secure_audit SET detail='tampered' WHERE id=2")
    conn.commit()
    conn.close()

    ok, broken_at, details = verify_audit_integrity(start_id=2, end_id=2)
    assert ok is False
    assert broken_at == 2
    assert "entry_hash mismatch" in details


def test_reset_audit_chain_with_event_starts_new_chain_and_anchor(tmp_path):
    db_path = tmp_path / "audit.db"
    audit_log = tmp_path / "audit.log"
    anchor_log = tmp_path / "audit_head.jsonl"
    anchor_latest = tmp_path / "audit_head_latest.json"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    configure_audit_service(
        get_db=_get_db_factory(str(db_path)),
        chain_seed="seed",
        integrity_key=b"test-integrity-key",
        audit_log_path=str(audit_log),
        audit_anchor_path=str(anchor_log),
        audit_anchor_latest_path=str(anchor_latest),
        audit_anchor_interval_seconds=0,
    )
    audit("OLD", "127.0.0.1", user="root", success=True, detail="old")

    result = reset_audit_chain_with_event("SYSTEM_RUNTIME_RESET", "-", user="root", success=True, detail="reset")

    assert result["ok"] is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, action, prev_hash FROM secure_audit ORDER BY id").fetchall()
    conn.close()
    assert [(row["id"], row["action"], row["prev_hash"]) for row in rows] == [(1, "SYSTEM_RUNTIME_RESET", "seed")]
    assert "SYSTEM_RUNTIME_RESET" in audit_log.read_text(encoding="utf-8")
    assert "OLD" not in audit_log.read_text(encoding="utf-8")
    ok, broken_at, _ = verify_audit_integrity()
    assert ok is True
    assert broken_at is None


def test_repair_serializes_against_append_from_another_connection(tmp_path, monkeypatch):
    db_path = tmp_path / "audit.db"
    audit_log = tmp_path / "audit.log"
    anchor_log = tmp_path / "audit_head.jsonl"
    anchor_latest = tmp_path / "audit_head_latest.json"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    repair_rows_read = threading.Event()
    allow_repair = threading.Event()
    append_begin_attempted = threading.Event()

    class CoordinatedConnection:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def execute(self, sql, *args):
            normalized = " ".join(sql.upper().split())
            worker_name = threading.current_thread().name
            if worker_name == "append-worker" and normalized == "BEGIN IMMEDIATE":
                append_begin_attempted.set()
            cursor = self._wrapped.execute(sql, *args)
            if (
                worker_name == "repair-worker"
                and normalized.startswith("SELECT ID, TS, ACTION")
                and "FROM SECURE_AUDIT ORDER BY ID ASC" in normalized
            ):
                repair_rows_read.set()
                if not allow_repair.wait(timeout=10):
                    raise TimeoutError("repair test coordination timed out")
            return cursor

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def get_coordinated_db():
        wrapped = sqlite3.connect(db_path, timeout=10)
        wrapped.row_factory = sqlite3.Row
        return CoordinatedConnection(wrapped)

    configure_audit_service(
        get_db=get_coordinated_db,
        chain_seed="repair-race-seed",
        integrity_key=b"repair-race-integrity-key",
        audit_log_path=str(audit_log),
        audit_anchor_path=str(anchor_log),
        audit_anchor_latest_path=str(anchor_latest),
        audit_anchor_interval_seconds=0,
    )
    audit("FIRST", "127.0.0.1", user="root", success=True, detail="first")
    audit("SECOND", "127.0.0.1", user="root", success=True, detail="second")

    # Force reseal to change the existing head.  An append based on the old
    # head would become invalid after repair commits.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE secure_audit SET detail='requires-reseal' WHERE id=1")
    conn.commit()
    conn.close()
    assert verify_audit_integrity()[0] is False

    # Different server processes do not share this module-level mutex.
    monkeypatch.setattr("services.system.audit._audit_db_lock", contextlib.nullcontext())
    monkeypatch.setattr(
        "services.system.audit._audit_mutation_guard",
        lambda: contextlib.nullcontext(),
    )
    errors = []

    def run_repair():
        try:
            repair_audit_chain(reason="concurrent repair regression")
        except Exception as exc:
            errors.append(exc)

    def run_append():
        try:
            audit("DURING_REPAIR", "127.0.0.1", user="worker", success=True, detail="append")
        except Exception as exc:
            errors.append(exc)

    repair_thread = threading.Thread(target=run_repair, name="repair-worker")
    append_thread = threading.Thread(target=run_append, name="append-worker")
    repair_thread.start()
    assert repair_rows_read.wait(timeout=10)
    append_thread.start()
    assert append_begin_attempted.wait(timeout=10)
    # Under the fixed transaction ordering, append is blocked on SQLite's
    # writer lock until repair is allowed to finish.
    time.sleep(0.1)
    assert append_thread.is_alive()
    allow_repair.set()
    repair_thread.join(timeout=10)
    append_thread.join(timeout=10)

    assert not repair_thread.is_alive()
    assert not append_thread.is_alive()
    assert errors == []
    ok, broken_at, details = verify_audit_integrity()
    assert ok is True, details
    assert broken_at is None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, prev_hash, chain_hash FROM secure_audit ORDER BY id").fetchall()
    conn.close()
    assert len(rows) == 3
    assert rows[2]["prev_hash"] == rows[1]["chain_hash"]


def test_repair_keeps_legacy_append_chain_verifiable(tmp_path):
    db_path = tmp_path / "legacy_audit.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            chain_hash TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    configure_audit_service(
        get_db=_get_db_factory(str(db_path)),
        chain_seed="legacy-seed",
        integrity_key=b"legacy-integrity-key",
        audit_log_path=str(tmp_path / "legacy_audit.log"),
        audit_anchor_path=str(tmp_path / "legacy_anchor.jsonl"),
        audit_anchor_latest_path=str(tmp_path / "legacy_anchor_latest.json"),
        audit_anchor_interval_seconds=0,
    )

    audit("LEGACY_FIRST", "127.0.0.1", success=True)
    audit("LEGACY_SECOND", "127.0.0.1", success=True)
    assert verify_audit_integrity()[0] is True

    result = repair_audit_chain(reason="legacy regression")

    assert result["entries_resealed"] == 2
    ok, broken_at, details = verify_audit_integrity()
    assert ok is True, details
    assert broken_at is None
    audit("LEGACY_AFTER_REPAIR", "127.0.0.1", success=True)
    assert verify_audit_integrity()[0] is True


def test_verify_retries_when_latest_anchor_changes_after_db_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    snapshot_captured = threading.Event()
    allow_verify = threading.Event()

    class CapturedRows:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class CoordinatedConnection:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def execute(self, sql, *args):
            cursor = self._wrapped.execute(sql, *args)
            normalized = " ".join(sql.upper().split())
            if (
                threading.current_thread().name == "verify-worker"
                and "FROM SECURE_AUDIT ORDER BY ID ASC" in normalized
            ):
                rows = cursor.fetchall()
                snapshot_captured.set()
                if not allow_verify.wait(timeout=10):
                    raise TimeoutError("verify race coordination timed out")
                return CapturedRows(rows)
            return cursor

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def get_db():
        wrapped = sqlite3.connect(db_path, timeout=10)
        wrapped.row_factory = sqlite3.Row
        return CoordinatedConnection(wrapped)

    configure_audit_service(
        get_db=get_db,
        chain_seed="verify-race-seed",
        integrity_key=b"verify-race-integrity-key",
        audit_log_path=str(tmp_path / "audit.log"),
        audit_anchor_path=str(tmp_path / "audit_head.jsonl"),
        audit_anchor_latest_path=str(tmp_path / "audit_head_latest.json"),
        audit_anchor_interval_seconds=0,
    )
    monkeypatch.setattr(audit_service, "_last_audit_anchor_at", 0.0)
    audit("BEFORE_VERIFY", "127.0.0.1", success=True)
    result = []

    def run_verify():
        result.append(verify_audit_integrity())

    verify_thread = threading.Thread(target=run_verify, name="verify-worker")
    verify_thread.start()
    assert snapshot_captured.wait(timeout=10)
    audit("DURING_VERIFY", "127.0.0.1", success=True)
    allow_verify.set()
    verify_thread.join(timeout=10)

    assert not verify_thread.is_alive()
    assert result and result[0][0] is True, result
    assert verify_audit_integrity()[0] is True


def test_verify_retries_during_repair_commit_to_anchor_window(tmp_path, monkeypatch):
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    configure_audit_service(
        get_db=_get_db_factory(str(db_path)),
        chain_seed="repair-verify-seed",
        integrity_key=b"repair-verify-integrity-key",
        audit_log_path=str(tmp_path / "audit.log"),
        audit_anchor_path=str(tmp_path / "audit_head.jsonl"),
        audit_anchor_latest_path=str(tmp_path / "audit_head_latest.json"),
        audit_anchor_interval_seconds=0,
    )
    audit("FIRST", "127.0.0.1", success=True, detail="first")
    audit("SECOND", "127.0.0.1", success=True, detail="second")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE secure_audit SET detail='requires-reseal' WHERE id=1")
    conn.commit()
    conn.close()

    repair_committed = threading.Event()
    allow_anchor = threading.Event()
    original_write_anchor = audit_service._write_audit_anchor

    def coordinated_write_anchor(*args, **kwargs):
        if threading.current_thread().name == "repair-worker":
            repair_committed.set()
            if not allow_anchor.wait(timeout=10):
                raise TimeoutError("repair anchor coordination timed out")
        return original_write_anchor(*args, **kwargs)

    monkeypatch.setattr(audit_service, "_write_audit_anchor", coordinated_write_anchor)
    errors = []
    verify_result = []

    def run_repair():
        try:
            repair_audit_chain(reason="verify race regression")
        except Exception as exc:
            errors.append(exc)

    def run_verify():
        verify_result.append(verify_audit_integrity())

    repair_thread = threading.Thread(target=run_repair, name="repair-worker")
    repair_thread.start()
    assert repair_committed.wait(timeout=10)
    verify_thread = threading.Thread(target=run_verify, name="verify-worker")
    verify_thread.start()
    time.sleep(0.08)
    assert verify_thread.is_alive()
    allow_anchor.set()
    repair_thread.join(timeout=10)
    verify_thread.join(timeout=10)

    assert not repair_thread.is_alive()
    assert not verify_thread.is_alive()
    assert errors == []
    assert verify_result and verify_result[0][0] is True, verify_result


def test_empty_db_with_persistent_latest_anchor_fails_closed(tmp_path, monkeypatch):
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    latest_path = tmp_path / "audit_head_latest.json"
    configure_audit_service(
        get_db=_get_db_factory(str(db_path)),
        chain_seed="stale-anchor-seed",
        integrity_key=b"stale-anchor-integrity-key",
        audit_log_path=str(tmp_path / "audit.log"),
        audit_anchor_path=str(tmp_path / "audit_head.jsonl"),
        audit_anchor_latest_path=str(latest_path),
        audit_anchor_interval_seconds=0,
    )
    audit("ANCHOR_THEN_DELETE", "127.0.0.1", success=True)
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM secure_audit")
    conn.commit()
    conn.close()
    assert latest_path.exists()
    monkeypatch.setattr(audit_service, "_AUDIT_VERIFY_STABLE_ATTEMPTS", 2)
    monkeypatch.setattr(audit_service, "_AUDIT_VERIFY_RETRY_SECONDS", 0)

    ok, broken_at, details = verify_audit_integrity()

    assert ok is False
    assert broken_at is None
    assert "missing audit id" in details


def test_reset_without_event_atomically_clears_latest_anchor(tmp_path):
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    audit_log = tmp_path / "audit.log"
    anchor_latest = tmp_path / "audit_head_latest.json"
    configure_audit_service(
        get_db=_get_db_factory(str(db_path)),
        chain_seed="reset-empty-seed",
        integrity_key=b"reset-empty-integrity-key",
        audit_log_path=str(audit_log),
        audit_anchor_path=str(tmp_path / "audit_head.jsonl"),
        audit_anchor_latest_path=str(anchor_latest),
        audit_anchor_interval_seconds=0,
    )
    audit("OLD", "127.0.0.1", success=True)

    result = reset_audit_chain_with_event(
        "NO_EVENT",
        "-",
        write_event=False,
    )

    assert result == {"ok": True, "reset": True, "event": None}
    assert not anchor_latest.exists()
    assert audit_log.read_text(encoding="utf-8") == ""
    assert verify_audit_integrity() == (True, None, "no entries; no latest anchor")


def test_reset_genesis_cannot_be_overtaken_by_another_worker(tmp_path, monkeypatch):
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    reset_committed = threading.Event()
    allow_reset = threading.Event()

    class CoordinatedConnection:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def commit(self):
            self._wrapped.commit()
            if (
                threading.current_thread().name == "reset-worker"
                and not reset_committed.is_set()
            ):
                reset_committed.set()
                if not allow_reset.wait(timeout=10):
                    raise TimeoutError("reset race coordination timed out")

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def get_db():
        wrapped = sqlite3.connect(db_path, timeout=10)
        wrapped.row_factory = sqlite3.Row
        return CoordinatedConnection(wrapped)

    audit_log = tmp_path / "audit.log"
    configure_audit_service(
        get_db=get_db,
        chain_seed="reset-race-seed",
        integrity_key=b"reset-race-integrity-key",
        audit_log_path=str(audit_log),
        audit_anchor_path=str(tmp_path / "audit_head.jsonl"),
        audit_anchor_latest_path=str(tmp_path / "audit_head_latest.json"),
        audit_anchor_interval_seconds=0,
    )
    audit("OLD", "127.0.0.1", success=True)
    # Simulate independent workers: only the dedicated flock remains shared.
    monkeypatch.setattr(audit_service, "_audit_lock", contextlib.nullcontext())
    errors = []

    def run_reset():
        try:
            reset_audit_chain_with_event("RESET_GENESIS", "-", success=True)
        except Exception as exc:
            errors.append(exc)

    def run_append():
        try:
            audit("AFTER_RESET", "127.0.0.1", success=True)
        except Exception as exc:
            errors.append(exc)

    reset_thread = threading.Thread(target=run_reset, name="reset-worker")
    append_thread = threading.Thread(target=run_append, name="append-worker")
    reset_thread.start()
    assert reset_committed.wait(timeout=10)
    append_thread.start()
    time.sleep(0.1)
    assert append_thread.is_alive()
    allow_reset.set()
    reset_thread.join(timeout=10)
    append_thread.join(timeout=10)

    assert not reset_thread.is_alive()
    assert not append_thread.is_alive()
    assert errors == []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, action, prev_hash, chain_hash FROM secure_audit ORDER BY id").fetchall()
    conn.close()
    assert [(row["id"], row["action"]) for row in rows] == [
        (1, "RESET_GENESIS"),
        (2, "AFTER_RESET"),
    ]
    assert rows[1]["prev_hash"] == rows[0]["chain_hash"]
    log_entries = [
        json.loads(line)
        for line in audit_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["_audit_id"] for entry in log_entries] == [1, 2]
    assert [entry["action"] for entry in log_entries] == ["RESET_GENESIS", "AFTER_RESET"]
    assert log_entries[1]["_prev_hash"] == log_entries[0]["_chain_hash"]
    assert verify_audit_integrity()[0] is True


def test_repair_violation_chains_reseals_corrupted_entries(tmp_path):
    db_path = tmp_path / "violations.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            violation_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE secure_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 1,
            reason TEXT NOT NULL,
            triggered_by TEXT NOT NULL,
            actor_username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO users (id, username, role, violation_count) VALUES (1, 'alice', 'user', 0)"
    )
    conn.commit()
    conn.close()

    configure_violations_service(
        get_db=_get_db_factory(str(db_path)),
        get_system_settings=lambda: {},
        audit=lambda *args, **kwargs: None,
        get_client_ip=lambda: "127.0.0.1",
        chain_seed="seed",
        integrity_key=b"test-integrity-key",
    )
    secure_add_violation(1, "alice", "user", 1, "first", "system", "root")
    secure_add_violation(1, "alice", "user", 1, "second", "system", "root")
    assert verify_violation_integrity(1)[0] is True

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE secure_violations SET entry_hash='broken' WHERE id=1")
    conn.commit()
    conn.close()
    assert verify_violation_integrity(1)[0] is False

    result = repair_violation_chains()

    assert result == {"entries_resealed": 2, "users_resealed": 1}
    ok, broken_at, _ = verify_violation_integrity(1)
    assert ok is True
    assert broken_at is None
