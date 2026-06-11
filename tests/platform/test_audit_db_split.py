import sqlite3

from services.server.database import get_audit_db
from services.server.routes import OPERATION_ROUTE_KEYS
from services.system import audit as audit_service


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
