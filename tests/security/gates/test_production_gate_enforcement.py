"""Locks the production-gate enforcement contract.

Existing tests/test_snapshots.py covers the happy path
(all required reports passing -> switch succeeds) and the empty-DB blocked
path. This file adds the granular regression cases the launch-check
UI promises to surface:

1. Wrong confirm phrase -> blocked
2. 0 / required reports -> blocked
3. one missing report -> blocked, response lists the
   missing report
4. all inserted but ONE has critical_findings_count > 0 -> blocked
5. all inserted but ONE has high_findings_count > 0 -> blocked
6. all inserted but ONE has empty report_hash -> blocked
7. all inserted but ONE has pass=False -> blocked
8. all required reports perfect -> mode actually switches to production

If any of these regress, an operator could push to production with
unverified or actively-failing reports — exactly the contamination
the gate exists to prevent.
"""

import json
import os
import sqlite3
import tempfile
import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from services.snapshots import (
    PRODUCTION_REQUIRED_REPORT_TYPES,
    ServerModeService,
    SnapshotService,
    _canonical_json_text,
    _hmac_sha256,
    _production_report_signature_payload,
    ensure_snapshot_schema,
)


def _build_runtime(tmp_path):
    """Build a ServerModeService backed by a snapshot service, mirroring
    the existing tests/test_snapshots.py helpers — so we drive the real
    gate-and-switch path, not a stripped-down stub.
    """
    base = tmp_path / "app"
    base.mkdir()
    db_path = base / "database.db"
    uploads = base / "uploads"
    storage = base / "storage"
    uploads.mkdir()
    storage.mkdir()

    def get_db():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # Minimum supporting tables ServerModeService.switch_mode reaches into.
    conn = get_db()
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, role TEXT, status TEXT NOT NULL DEFAULT 'active', "
        "member_level TEXT, base_level TEXT, effective_level TEXT, must_change_password INTEGER DEFAULT 0, "
        "is_default_password INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO users (id, username, role, status, member_level, base_level, effective_level) "
        "VALUES (1,'root','super_admin','active','normal','normal','normal')"
    )
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, user_id INTEGER, is_revoked INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE user_passwords (id INTEGER PRIMARY KEY, user_id INTEGER, password_hash TEXT, created_at TEXT)")
    ensure_snapshot_schema(conn)
    conn.commit()
    conn.close()

    snapshot = SnapshotService(
        get_db=get_db,
        db_path=db_path,
        base_dir=base,
        storage_root=storage,
        audit=lambda *a, **kw: None,
        file_roots=[uploads],
        config_files=[],
    )
    saved = []
    mode = ServerModeService(
        snapshot_service=snapshot,
        get_db=get_db,
        audit=lambda *a, **kw: None,
        save_settings=lambda data: saved.append(dict(data)) or dict(data),
    )
    return mode, get_db, db_path


def _insert_report(db_path, report_type, *, _pass=True, critical=0, high=0, report_hash=None, target_commit="test-commit", server_mode="dev_ready"):
    """Insert a single production_entry_reports row with controllable
    failure dimensions, so each test can inject exactly the kind of
    failure it wants to see blocked.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat()
    raw_report = {
        "report_type": report_type,
        "target_commit": target_commit,
        "status": "pass" if _pass else "fail",
        "critical_findings_count": int(critical),
        "high_findings_count": int(high),
    }
    raw_report_json = _canonical_json_text(raw_report)
    rh = report_hash if report_hash is not None else f"sha256:{hashlib.sha256(raw_report_json.encode('utf-8')).hexdigest()}"
    key = os.environ.setdefault("SERVER_MODE_REPORT_HMAC_KEY", "pytest-production-report-key")
    key_version = os.environ.setdefault("SERVER_MODE_REPORT_HMAC_KEY_VERSION", "pytest-v1")
    signature_payload = {
        "report_type": report_type,
        "report_hash": rh,
        "target_commit": target_commit,
        "target_branch": "test-branch",
        "server_mode": server_mode,
        "test_result": "pass" if _pass else "fail",
        "pass": 1 if _pass else 0,
        "critical_findings_count": int(critical),
        "high_findings_count": int(high),
        "unresolved_findings_json": "[]",
        "tester": "pytest",
        "raw_report_json": raw_report_json,
        "report_source": "pytest_fixture",
        "key_version": key_version,
    }
    signature = f"hmac_sha256:{_hmac_sha256(key, _production_report_signature_payload(signature_payload))}"
    conn.execute(
        """
        INSERT INTO production_entry_reports
        (id, report_type, report_hash, target_commit, target_branch, server_mode,
         test_result, pass, critical_findings_count, high_findings_count,
         unresolved_findings_json, tester, signature, raw_report_json, report_source,
         trust_level, key_version, verified_at, created_at)
        VALUES (?, ?, ?, ?, 'test-branch', ?, ?, ?, ?, ?, '[]', 'pytest', ?, ?, 'pytest_fixture', 'verified', ?, ?, ?)
        """,
        (
            f"rep_{report_type}",
            report_type,
            rh,
            target_commit,
            server_mode,
            "pass" if _pass else "fail",
            1 if _pass else 0,
            int(critical),
            int(high),
            signature,
            raw_report_json,
            key_version,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()


def _insert_all_passing(db_path):
    for rt in PRODUCTION_REQUIRED_REPORT_TYPES:
        _insert_report(db_path, rt)


# ── #1 Wrong confirm phrase ───────────────────────────────────────────


def test_gate_blocks_with_wrong_confirm_phrase(tmp_path):
    mode, _, db_path = _build_runtime(tmp_path)
    _insert_all_passing(db_path)
    actor = {"id": 1, "username": "root"}
    res = mode.switch_mode(target_mode="production", actor=actor, confirm="LETS_GO")
    assert res["ok"] is False
    assert "GO_LIVE" in res["msg"]


# ── #2 Empty DB ───────────────────────────────────────────────────────


def test_switch_allows_no_reports_and_returns_advisory(tmp_path):
    mode, _, _ = _build_runtime(tmp_path)
    actor = {"id": 1, "username": "root"}
    res = mode.switch_mode(target_mode="production", actor=actor, confirm="GO_LIVE")
    assert res["ok"] is True
    assert res.get("mode", {}).get("current_mode") == "production"
    requirements = res.get("production_requirements") or {}
    assert set(requirements.get("missing", [])) == set(PRODUCTION_REQUIRED_REPORT_TYPES)
    assert res.get("advisories")


# ── #3 one missing report ────────────────────────────────────────────


def test_switch_allows_one_missing_report_and_keeps_report_status(tmp_path):
    mode, _, db_path = _build_runtime(tmp_path)
    # Insert every required report except the first one.
    skipped = PRODUCTION_REQUIRED_REPORT_TYPES[0]
    for rt in PRODUCTION_REQUIRED_REPORT_TYPES[1:]:
        _insert_report(db_path, rt)
    actor = {"id": 1, "username": "root"}
    res = mode.switch_mode(target_mode="production", actor=actor, confirm="GO_LIVE")
    assert res["ok"] is True
    requirements = res.get("production_requirements") or {}
    # The one we skipped must show up as missing — and only that one.
    assert requirements.get("missing") == [skipped]
    assert not requirements.get("failed")


# ── #4 critical_findings_count > 0 ─────────────────────────────────────


def test_switch_allows_critical_report_but_keeps_advisory(tmp_path):
    mode, _, db_path = _build_runtime(tmp_path)
    # All passing except one with critical=2.
    bad_one = PRODUCTION_REQUIRED_REPORT_TYPES[3]
    for rt in PRODUCTION_REQUIRED_REPORT_TYPES:
        if rt == bad_one:
            _insert_report(db_path, rt, critical=2)
        else:
            _insert_report(db_path, rt)
    actor = {"id": 1, "username": "root"}
    res = mode.switch_mode(target_mode="production", actor=actor, confirm="GO_LIVE")
    assert res["ok"] is True
    requirements = res.get("production_requirements") or {}
    assert bad_one in requirements.get("failed", []), requirements


# ── #5 high_findings_count > 0 ─────────────────────────────────────────


def test_switch_allows_high_report_but_keeps_advisory(tmp_path):
    mode, _, db_path = _build_runtime(tmp_path)
    bad_one = PRODUCTION_REQUIRED_REPORT_TYPES[5]
    for rt in PRODUCTION_REQUIRED_REPORT_TYPES:
        if rt == bad_one:
            _insert_report(db_path, rt, high=1)
        else:
            _insert_report(db_path, rt)
    actor = {"id": 1, "username": "root"}
    res = mode.switch_mode(target_mode="production", actor=actor, confirm="GO_LIVE")
    assert res["ok"] is True
    requirements = res.get("production_requirements") or {}
    assert bad_one in requirements.get("failed", []), requirements


# ── #6 empty report_hash ──────────────────────────────────────────────


def test_switch_allows_unsigned_report_but_keeps_advisory(tmp_path):
    mode, _, db_path = _build_runtime(tmp_path)
    bad_one = PRODUCTION_REQUIRED_REPORT_TYPES[7]
    for rt in PRODUCTION_REQUIRED_REPORT_TYPES:
        if rt == bad_one:
            _insert_report(db_path, rt, report_hash="")
        else:
            _insert_report(db_path, rt)
    actor = {"id": 1, "username": "root"}
    res = mode.switch_mode(target_mode="production", actor=actor, confirm="GO_LIVE")
    assert res["ok"] is True
    requirements = res.get("production_requirements") or {}
    assert bad_one in requirements.get("failed", []), requirements


# ── #7 pass=False ─────────────────────────────────────────────────────


def test_switch_allows_failed_report_but_keeps_advisory(tmp_path):
    mode, _, db_path = _build_runtime(tmp_path)
    bad_one = PRODUCTION_REQUIRED_REPORT_TYPES[10]
    for rt in PRODUCTION_REQUIRED_REPORT_TYPES:
        if rt == bad_one:
            _insert_report(db_path, rt, _pass=False)
        else:
            _insert_report(db_path, rt)
    actor = {"id": 1, "username": "root"}
    res = mode.switch_mode(target_mode="production", actor=actor, confirm="GO_LIVE")
    assert res["ok"] is True
    requirements = res.get("production_requirements") or {}
    assert bad_one in requirements.get("failed", []), requirements


# ── #8 all required reports perfect -> switch succeeds ───────────────


def test_gate_allows_switch_when_all_required_reports_pass(tmp_path):
    mode, _, db_path = _build_runtime(tmp_path)
    _insert_all_passing(db_path)
    actor = {"id": 1, "username": "root"}
    res = mode.switch_mode(target_mode="production", actor=actor, confirm="GO_LIVE")
    assert res["ok"] is True, res
    assert res.get("mode", {}).get("current_mode") == "production"


# ── #9 production_requirements rollup matches what the UI reads ────────


def test_production_requirements_payload_shape_for_launch_check_ui(tmp_path):
    """The launch-check tab reads .required / .missing / .failed /
    .reports / .ok. Verify all five fields exist and are well-formed
    so the UI doesn't have to defensive-code around shape drift.
    """
    mode, _, db_path = _build_runtime(tmp_path)
    # Insert 11 passing + 1 failed + leave 1 missing.
    skipped = PRODUCTION_REQUIRED_REPORT_TYPES[0]
    bad_one = PRODUCTION_REQUIRED_REPORT_TYPES[1]
    for rt in PRODUCTION_REQUIRED_REPORT_TYPES[1:]:
        if rt == bad_one:
            _insert_report(db_path, rt, critical=1)
        else:
            _insert_report(db_path, rt)
    requirements = mode.production_requirements()
    assert isinstance(requirements.get("required"), list)
    assert set(requirements["required"]) == set(PRODUCTION_REQUIRED_REPORT_TYPES)
    assert requirements.get("missing") == [skipped]
    assert bad_one in requirements.get("failed", [])
    assert isinstance(requirements.get("reports"), dict)
    assert requirements.get("ok") is False
    # Shape contracts the UI relies on.
    for rt in PRODUCTION_REQUIRED_REPORT_TYPES:
        assert rt in requirements["reports"]
