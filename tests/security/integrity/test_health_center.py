import sqlite3
from pathlib import Path

from flask import Flask, jsonify, make_response

from routes.system_admin import register_system_admin_routes
from services.platform.bootstrap import CURRENT_SCHEMA_VERSION
from services.system.integrity_guard import IntegrityGuard, ensure_integrity_schema


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _passthrough(fn):
    return fn


class _SnapshotStub:
    def list_snapshots(self, actor=None):
        return [{"id": "snap_test"}]


def _make_app(
    tmp_path,
    actor=None,
    audit_result=(True, None, "integrity OK"),
    include_forum_tables=True,
    activation_log=None,
    integrity_guard=None,
    points_service=None,
    db_open_log=None,
):
    db_path = tmp_path / "health.db"
    chat_dir = tmp_path / "chats"
    log_dir = tmp_path / "logs"
    anchor_dir = tmp_path / "anchors"
    storage_dir = tmp_path / "storage"
    for path in (chat_dir, log_dir, anchor_dir, storage_dir):
        path.mkdir()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    schema = """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, status TEXT);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, user_id INTEGER, expires_at TEXT, is_revoked INTEGER DEFAULT 0);
        CREATE TABLE chat_messages (id INTEGER PRIMARY KEY);
        CREATE TABLE chat_message_reports (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE violation_appeals (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE moderation_proposals (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE secure_violations (id INTEGER PRIMARY KEY);
        CREATE TABLE secure_audit (id INTEGER PRIMARY KEY);
        CREATE TABLE uploaded_files (id TEXT PRIMARY KEY, scan_status TEXT, risk_level TEXT, deleted_at TEXT, size_bytes INTEGER DEFAULT 0);
        CREATE TABLE storage_files (id TEXT PRIMARY KEY);
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
        """
    if include_forum_tables:
        schema += """
        CREATE TABLE forum_boards (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE forum_threads (id INTEGER PRIMARY KEY, status TEXT);
        """
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, 'current', '2026-01-01T00:00:00')",
        (CURRENT_SCHEMA_VERSION,),
    )
    conn.execute("INSERT INTO users (id, username, status) VALUES (1, 'root', 'active')")
    conn.execute("INSERT INTO chat_message_reports (id, status) VALUES (1, 'pending')")
    conn.execute("INSERT INTO uploaded_files (id, scan_status, risk_level, deleted_at) VALUES ('f1', 'quarantined', 'blocked', NULL)")
    conn.commit()
    conn.close()

    def get_db():
        if db_open_log is not None:
            db_open_log.append("open")
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    app = Flask(__name__)
    app.testing = True
    register_system_admin_routes(app, {
        "ANCHOR_DIR": str(anchor_dir),
        "BASE_DIR": str(tmp_path),
        "CHAT_DIR": str(chat_dir),
        "DB_PATH": str(db_path),
        "LOG_DIR": str(log_dir),
        "SERVER_LOG_PATH": str(log_dir / "server.log"),
        "STORAGE_DIR": str(storage_dir),
        "activate_emergency_lockdown": lambda reason: (activation_log.append(reason) if activation_log is not None else None),
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor or {"id": 1, "username": "root", "role": "super_admin"},
        "get_db": get_db,
        "get_feature_settings": lambda: {},
        "get_system_settings": lambda: {"maintenance_mode": False},
        "get_ua": lambda: "pytest",
        "integrity_guard": integrity_guard,
        "is_audit_chain_enabled": lambda: True,
        "json_resp": _json_resp,
        "points_service": points_service,
        "repair_audit_chain": lambda **kwargs: {"entries_resealed": 0},
        "repair_violation_chains": lambda: {"entries_resealed": 0},
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "save_feature_settings": lambda data: {},
        "save_settings": lambda data: data,
        "server_mode_service": None,
        "snapshot_service": _SnapshotStub(),
        "verify_audit_integrity": audit_result if callable(audit_result) else (lambda: audit_result),
    })
    return app


def _write_integrity_project(base: Path):
    (base / "services").mkdir(parents=True)
    (base / "routes").mkdir(parents=True)
    (base / "public" / "js").mkdir(parents=True)
    (base / "server.py").write_text("print('server')\n", encoding="utf-8")
    (base / "services" / "auth.py").write_text("AUTH = True\n", encoding="utf-8")
    (base / "routes" / "system_admin.py").write_text("ROOT = True\n", encoding="utf-8")
    (base / "public" / "js" / "50-admin.js").write_text("const admin = true;\n", encoding="utf-8")
    (base / "bootstrap.schema.sql").write_text("CREATE TABLE x(id);\n", encoding="utf-8")
    (base / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (base / "README.md").write_text("# test project\n", encoding="utf-8")


def test_health_readiness_and_db_integrity_endpoints(tmp_path):
    app = _make_app(tmp_path)
    client = app.test_client()

    readiness = client.get("/api/admin/health/readiness")
    assert readiness.status_code == 200
    body = readiness.get_json()
    assert body["readiness"]["status"] == "ok"
    assert body["readiness"]["database"]["schema_version"] == CURRENT_SCHEMA_VERSION
    assert body["readiness"]["database"]["quick_check"] == ["skipped_fast_health"]

    db = client.get("/api/admin/health/db-integrity")
    assert db.status_code == 200
    assert db.get_json()["database"]["quick_check"] == ["ok"]
    assert db.get_json()["database"]["ok"] is True


def test_admin_health_summary_includes_grouped_dashboard_data(tmp_path):
    app = _make_app(tmp_path)
    nested = tmp_path / "storage" / "user-1" / "files"
    nested.mkdir(parents=True)
    (nested / "asset.bin").write_bytes(b"abc")
    conn = sqlite3.connect(tmp_path / "health.db")
    conn.execute("INSERT INTO uploaded_files (id, scan_status, risk_level, deleted_at, size_bytes) VALUES ('asset', 'clean', 'low', NULL, 3)")
    conn.execute("INSERT INTO storage_files (id) VALUES ('sf1')")
    conn.commit()
    conn.close()
    res = app.test_client().get("/api/admin/health")
    data = res.get_json()

    assert res.status_code == 200
    assert data["ok"] is True
    assert data["status"] in {"ok", "degraded", "critical"}
    assert data["counts"]["pending_chat_reports"] == 1
    assert data["counts"]["pending_reports"] == 1
    assert "pending_moderation_proposals" in data["counts"]
    assert {"log_files", "anchor_files", "storage_files"} <= set(data["storage"])
    assert data["storage"]["storage_files"] == 1
    assert data["storage"]["storage_bytes"] == 3
    assert data["storage"]["storage_dir"] == "storage"
    assert data["readiness"]["database"]["schema_version"] == CURRENT_SCHEMA_VERSION
    assert "signals" in data["anomaly"]


def test_health_anomaly_reports_quarantined_files(tmp_path):
    app = _make_app(tmp_path)
    res = app.test_client().get("/api/admin/health/anomaly")
    data = res.get_json()
    assert res.status_code == 200
    assert data["anomaly"]["status"] == "warning"
    assert any(signal["name"] == "quarantined_files" for signal in data["anomaly"]["signals"])


def test_health_anomaly_treats_missing_optional_forum_tables_as_zero(tmp_path):
    app = _make_app(tmp_path, include_forum_tables=False)
    res = app.test_client().get("/api/admin/health/anomaly")
    data = res.get_json()

    assert res.status_code == 200
    assert data["anomaly"]["counts"]["pending_board_reviews"] == 0
    assert data["anomaly"]["counts"]["pending_thread_reviews"] == 0
    assert "pending_board_reviews" not in data["anomaly"]["errors"]
    assert "pending_thread_reviews" not in data["anomaly"]["errors"]
    assert not any(signal["name"] == "count_errors" for signal in data["anomaly"]["signals"])


def test_security_center_reuses_audit_integrity_result(tmp_path):
    calls = {"count": 0}

    def verify_once():
        calls["count"] += 1
        return True, None, "ok"

    app = _make_app(tmp_path, audit_result=verify_once)
    res = app.test_client().get("/api/admin/security-center")

    assert res.status_code == 200
    payload = res.get_json()["security_center"]
    assert payload["audit_integrity"]["details"] == "ok"
    assert payload["readiness"]["database"]["quick_check"] == ["skipped_fast_health"]
    assert calls["count"] == 1


def test_security_center_reuses_short_status_snapshot(tmp_path):
    db_opens = []
    app = _make_app(tmp_path, db_open_log=db_opens)
    client = app.test_client()

    first = client.get("/api/admin/security-center")
    assert first.status_code == 200
    first_open_count = len(db_opens)
    assert first_open_count > 0

    second = client.get("/api/admin/security-center")
    assert second.status_code == 200
    assert second.get_json() == first.get_json()
    assert len(db_opens) == first_open_count


def test_health_readiness_reuses_short_status_snapshot(tmp_path):
    db_opens = []
    app = _make_app(tmp_path, db_open_log=db_opens)
    client = app.test_client()

    first = client.get("/api/admin/health/readiness")
    assert first.status_code == 200
    first_open_count = len(db_opens)
    assert first_open_count > 0

    second = client.get("/api/admin/health/readiness")
    assert second.status_code == 200
    assert second.get_json() == first.get_json()
    assert len(db_opens) == first_open_count


def test_fast_admin_health_reuses_audit_and_skips_db_quick_check(tmp_path):
    calls = {"count": 0}

    def verify_once():
        calls["count"] += 1
        return True, None, "ok"

    app = _make_app(tmp_path, audit_result=verify_once)
    res = app.test_client().get("/api/admin/health")
    data = res.get_json()

    assert res.status_code == 200
    assert data["readiness"]["database"]["quick_check"] == ["skipped_fast_health"]
    assert data["readiness"]["database"]["schema_version"] == CURRENT_SCHEMA_VERSION
    assert calls["count"] == 1

    cached = app.test_client().get("/api/admin/health/readiness")
    assert cached.status_code == 200
    verification = cached.get_json()["readiness"]["audit_integrity"]["verification"]
    assert verification["scope"] == "full_chain_and_latest_anchor"
    assert verification["cached"] is True
    assert verification["cache_max_age_seconds"] == 30.0
    assert calls["count"] == 1

    deep = app.test_client().get("/api/admin/health/audit-chain")
    assert deep.status_code == 200
    assert deep.get_json()["audit_integrity"]["verification"]["force_refresh"] is True
    assert calls["count"] == 2


def test_health_audit_chain_reports_broken_chain(tmp_path):
    activation_log = []
    app = _make_app(tmp_path, audit_result=(False, 7, "hash mismatch"), activation_log=activation_log)
    res = app.test_client().get("/api/admin/health/audit-chain")
    data = res.get_json()
    assert res.status_code == 200
    assert data["audit_integrity"]["ok"] is False
    assert data["audit_integrity"]["broken_at"] == 7
    assert data["audit_integrity"]["operator_action_required"] is True
    assert data["audit_integrity"]["auto_lockdown_applied"] is False
    assert activation_log == []


def test_admin_health_broken_audit_chain_marks_critical_without_auto_lockdown(tmp_path):
    activation_log = []
    app = _make_app(tmp_path, audit_result=(False, 7, "hash mismatch"), activation_log=activation_log)
    res = app.test_client().get("/api/admin/health")
    data = res.get_json()

    assert res.status_code == 200
    assert data["status"] == "critical"
    assert data["audit_integrity"]["ok"] is False
    assert data["audit_integrity"]["operator_action_required"] is True
    assert data["audit_integrity"]["auto_lockdown_applied"] is False
    assert activation_log == []


def test_admin_health_marks_points_safe_mode_critical_with_forensics(tmp_path):
    class PointsServiceStub:
        def safe_mode_status(self):
            return {
                "safe_mode": True,
                "reason": "governance_clock_jump_detected",
                "forensic_bundle_id": "fb-health-clock",
                "verification": {
                    "violation": "wall_clock_fast_forward",
                    "wall_elapsed_seconds": 7200,
                    "monotonic_elapsed_seconds": 10,
                    "tolerance_seconds": 300,
                    "guard_model": "wall_clock_vs_monotonic_v1",
                    "observed_ip": "203.0.113.77",
                },
            }

        def transfer_finality_observability_snapshot(self, *, recent_limit):
            return {
                "ok": True,
                "status": "ok",
                "snapshot_type": "points_transfer_finality_observability",
                "bounded": True,
                "recent_limit": recent_limit,
            }

    app = _make_app(tmp_path, points_service=PointsServiceStub())
    res = app.test_client().get("/api/admin/health")
    data = res.get_json()

    assert res.status_code == 200
    assert data["status"] == "critical"
    points = data["points_finality"]
    assert points["status"] == "critical"
    assert points["safe_mode_active"] is True
    assert points["safe_mode"]["reason"] == "governance_clock_jump_detected"
    assert points["attack_method"] == "suspected_governance_clock_manipulation"
    assert points["safe_mode_security"]["attack_classification"]["score"] >= points["safe_mode_security"]["attack_classification"]["threshold"]
    assert any(item["value"] == "203.0.113.77" for item in points["safe_mode_security"]["ip_evidence"])


def test_health_integrity_guard_clean_deploy_drift_is_degraded_not_critical(tmp_path):
    integrity_db = tmp_path / "integrity.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_integrity_project(repo)

    def get_integrity_db():
        conn = sqlite3.connect(integrity_db)
        conn.row_factory = sqlite3.Row
        ensure_integrity_schema(conn)
        return conn

    guard = IntegrityGuard(
        base_dir=repo,
        signing_key=b"test-signing-key",
        get_db=get_integrity_db,
        audit=lambda *args, **kwargs: None,
    )
    guard.scan(actor="system")
    (repo / "services" / "auth.py").write_text("AUTH = 'changed'\n", encoding="utf-8")
    guard._is_clean_git_checkout = lambda: True
    guard.scan(actor="system-startup", create_initial_manifest=False)

    app = _make_app(tmp_path, integrity_guard=guard)
    res = app.test_client().get("/api/admin/health/readiness")
    data = res.get_json()["readiness"]

    assert res.status_code == 200
    assert data["status"] == "degraded"
    integrity = next(item for item in data["checks"] if item["name"] == "integrity_guard")
    assert integrity["ok"] is False
    assert integrity["severity"] == "degraded"
    assert "尚未 rebaseline" in integrity["detail"]


def test_health_center_requires_super_admin(tmp_path):
    app = _make_app(tmp_path, actor={"id": 2, "username": "admin", "role": "manager"})
    res = app.test_client().get("/api/admin/health/readiness")
    assert res.status_code == 403


def test_unknown_path_options_does_not_advertise_unsafe_methods(tmp_path):
    app = _make_app(tmp_path)
    res = app.test_client().open("/not-real-pentest-path", method="OPTIONS")

    assert res.status_code == 404
    allow = res.headers["Allow"]
    assert "PUT" not in allow
    assert "DELETE" not in allow
    assert "PATCH" not in allow
    assert allow == "GET, POST, HEAD, OPTIONS"
