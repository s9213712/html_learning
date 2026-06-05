import io
import json
import os
import sqlite3
import hashlib
import tarfile
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask, jsonify, make_response

from routes.system_admin import register_system_admin_routes, restart_launcher_code
from services.snapshots import (
    ServerModeService,
    SnapshotService,
    _canonical_json_text,
    _hmac_sha256,
    _production_report_signature_payload,
    ensure_snapshot_schema,
)
from services.security.upload_security import ensure_upload_security_schema


ROOT = Path(__file__).resolve().parents[2]


def _db(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db(path):
    conn = _db(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            member_level TEXT NOT NULL DEFAULT 'normal',
            base_level TEXT NOT NULL DEFAULT 'normal',
            effective_level TEXT NOT NULL DEFAULT 'normal'
        );
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL
        );
        CREATE TABLE system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            value_type TEXT,
            updated_at TEXT,
            updated_by TEXT
        );
        INSERT INTO users (id, username, role, status, member_level, base_level, effective_level)
        VALUES (1, 'root', 'super_admin', 'active', 'normal', 'normal', 'normal');
        INSERT INTO posts (id, title) VALUES (1, 'P1');
        INSERT INTO system_settings (key, value, value_type, updated_at, updated_by)
        VALUES ('maintenance_mode', 'false', 'bool', '2026-01-01T00:00:00', 'test');
        """
    )
    ensure_snapshot_schema(conn)
    conn.commit()
    conn.close()


def _init_kv_db(path, key, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def _service(
    tmp_path,
    audit_log,
    *,
    runtime_secret_files=None,
    runtime_base_dir=None,
    additional_db_paths=None,
    file_roots=None,
):
    base = tmp_path / "app"
    base.mkdir()
    db_path = base / "database.db"
    uploads = base / "uploads"
    storage = base / "storage"
    uploads.mkdir()
    storage.mkdir()
    _init_db(db_path)

    def get_db():
        return _db(db_path)

    service = SnapshotService(
        get_db=get_db,
        db_path=db_path,
        base_dir=base,
        runtime_base_dir=runtime_base_dir,
        storage_root=storage,
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
        file_roots=file_roots if file_roots is not None else [uploads],
        config_files=[],
        runtime_secret_files=runtime_secret_files,
        additional_db_paths=additional_db_paths,
    )
    return service, db_path, uploads


def test_root_service_creates_manual_snapshot_with_metadata(tmp_path):
    audit_log = []
    runtime_key = tmp_path / "app" / ".filekey"
    service, db_path, uploads = _service(tmp_path, audit_log, runtime_secret_files=[runtime_key])
    runtime_key.write_text("secret-key-v1", encoding="utf-8")
    (uploads / "avatar.txt").write_text("v1", encoding="utf-8")

    result = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="before risky test")

    assert result.ok is True
    snapshot = service.get_snapshot(snapshot_id=result.snapshot_id, actor={"id": 1, "username": "root"})
    snapshot_dir = Path(snapshot["storage_path"])
    assert snapshot["status"] == "ready"
    assert snapshot["metadata"]["secrets_excluded"] is False
    assert snapshot["metadata"]["env_redacted"] is True
    assert snapshot["metadata"]["runtime_secret_files"][0]["path"] == ".filekey"
    assert (snapshot_dir / "metadata.json").exists()
    assert (snapshot_dir / "db.sqlite3.backup").exists()
    assert (snapshot_dir / "uploads.tar.gz").exists()
    assert (snapshot_dir / "config.tar.gz").exists()
    assert (snapshot_dir / "checksums.sha256").exists()
    assert any(call[0][0] == "SNAPSHOT_CREATE_READY" for call in audit_log)


def test_snapshot_covers_split_runtime_databases_and_portable_export(tmp_path):
    audit_log = []
    base = tmp_path / "app"
    auth_db = base / "runtime" / "database" / "auth.db"
    audit_db = base / "runtime" / "database" / "audit.db"
    service, _db_path, _uploads = _service(
        tmp_path,
        audit_log,
        additional_db_paths={"auth": auth_db, "audit": audit_db},
    )
    _init_kv_db(auth_db, "auth_state", "auth-v1")
    _init_kv_db(audit_db, "audit_state", "audit-v1")

    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="split db")

    assert snap.ok is True
    snapshot = service.get_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"})
    snapshot_dir = Path(snapshot["storage_path"])
    database_files = {item["label"]: item for item in snapshot["metadata"]["database_files"]}
    assert database_files["primary"]["archive_path"] == "db.sqlite3.backup"
    assert database_files["auth"]["archive_path"] == "databases/auth.sqlite3.backup"
    assert database_files["audit"]["archive_path"] == "databases/audit.sqlite3.backup"
    assert (snapshot_dir / "databases" / "auth.sqlite3.backup").exists()
    assert (snapshot_dir / "databases" / "audit.sqlite3.backup").exists()

    exported = service.export_snapshot_archive(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"})
    assert exported["ok"] is True
    with tarfile.open(exported["path"], "r:gz") as tar:
        names = set(tar.getnames())
    assert f"{snap.snapshot_id}/databases/auth.sqlite3.backup" in names
    assert f"{snap.snapshot_id}/databases/audit.sqlite3.backup" in names

    conn = sqlite3.connect(auth_db)
    conn.execute("UPDATE kv SET value='auth-dirty' WHERE key='auth_state'")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(audit_db)
    conn.execute("UPDATE kv SET value='audit-dirty' WHERE key='audit_state'")
    conn.commit()
    conn.close()

    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="restore split db")

    assert restored["ok"] is True
    assert {item["label"] for item in restored["database_restore"]["restored"]} == {"auth", "audit"}
    conn = sqlite3.connect(auth_db)
    assert conn.execute("SELECT value FROM kv WHERE key='auth_state'").fetchone()[0] == "auth-v1"
    conn.close()
    conn = sqlite3.connect(audit_db)
    assert conn.execute("SELECT value FROM kv WHERE key='audit_state'").fetchone()[0] == "audit-v1"
    conn.close()


def test_snapshot_file_roots_exclude_and_preserve_snapshot_repository(tmp_path):
    audit_log = []
    base = tmp_path / "app"
    base.mkdir()
    runtime_root = base / "runtime"
    storage_root = runtime_root / "storage"
    storage_root.mkdir(parents=True)
    db_path = base / "database.db"
    _init_db(db_path)

    def get_db():
        return _db(db_path)

    service = SnapshotService(
        get_db=get_db,
        db_path=db_path,
        base_dir=base,
        runtime_base_dir=runtime_root,
        storage_root=storage_root,
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
        file_roots=[storage_root],
        config_files=[],
    )
    preserved_snapshot_file = storage_root / "snapshots" / "keep" / "internal.txt"
    preserved_snapshot_file.parent.mkdir(parents=True)
    preserved_snapshot_file.write_text("local snapshot repository", encoding="utf-8")
    (storage_root / "asset.txt").write_text("asset-v1", encoding="utf-8")

    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="exclude snapshots")
    snapshot_dir = service._snapshot_dir(snap.snapshot_id)
    with tarfile.open(snapshot_dir / "uploads.tar.gz", "r:gz") as tar:
        names = set(tar.getnames())
    assert "runtime/storage/asset.txt" in names
    assert all("/snapshots/" not in name and not name.endswith("/snapshots") for name in names)

    (storage_root / "dirty.txt").write_text("dirty", encoding="utf-8")
    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="preserve snapshots")

    assert restored["ok"] is True
    assert (storage_root / "asset.txt").read_text(encoding="utf-8") == "asset-v1"
    assert not (storage_root / "dirty.txt").exists()
    assert preserved_snapshot_file.read_text(encoding="utf-8") == "local snapshot repository"


def test_snapshot_does_not_capture_legacy_points_chain_backup_root(tmp_path):
    audit_log = []
    base = tmp_path / "app"
    base.mkdir()
    points_backup_root = base / "runtime" / "database" / "points_chain_backups"
    points_backup_root.mkdir(parents=True)
    db_path = base / "database.db"
    _init_db(db_path)

    def get_db():
        return _db(db_path)

    service = SnapshotService(
        get_db=get_db,
        db_path=db_path,
        base_dir=base,
        runtime_base_dir=base / "runtime",
        storage_root=base / "runtime" / "storage",
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
        file_roots=[],
        config_files=[],
    )
    (points_backup_root / "chain-backup.json").write_text('{"height": 7}', encoding="utf-8")

    snap = service.create_snapshot(
        snapshot_type="manual",
        actor={"id": 1, "username": "root"},
        notes="points chain backup policy boundary",
    )
    snapshot_dir = service._snapshot_dir(snap.snapshot_id)
    with tarfile.open(snapshot_dir / "uploads.tar.gz", "r:gz") as tar:
        names = set(tar.getnames())

    assert snap.ok is True
    assert all("points_chain_backups" not in name for name in names)


def test_snapshot_runtime_secret_paths_use_runtime_prefix_for_external_runtime_dir(tmp_path):
    audit_log = []
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    runtime_key = runtime_root / ".filekey"
    service, _db_path, _uploads = _service(
        tmp_path,
        audit_log,
        runtime_base_dir=runtime_root,
        runtime_secret_files=[runtime_key],
    )
    runtime_key.write_text("secret-key-v1", encoding="utf-8")

    result = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="external runtime root")

    assert result.ok is True
    snapshot = service.get_snapshot(snapshot_id=result.snapshot_id, actor={"id": 1, "username": "root"})
    assert snapshot["metadata"]["runtime_secret_files"][0]["path"] == "runtime/.filekey"


def test_restore_stages_runtime_secret_files_before_repo_runtime_sentinel(tmp_path):
    audit_log = []
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    runtime_key = runtime_root / ".chain_seed"
    service, db_path, uploads = _service(
        tmp_path,
        audit_log,
        runtime_base_dir=runtime_root,
        runtime_secret_files=[runtime_key],
    )
    runtime_key.write_text("seed-v1", encoding="utf-8")
    (uploads / "f1.txt").write_text("one", encoding="utf-8")
    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="baseline external runtime")

    runtime_key.write_text("seed-v2", encoding="utf-8")
    (uploads / "f2.txt").write_text("two", encoding="utf-8")
    (service.base_dir / "runtime").write_text("block repo pollution\n", encoding="utf-8")

    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="restore with sentinel")

    assert restored["ok"] is True
    assert runtime_key.read_text(encoding="utf-8") == "seed-v1"
    assert not (uploads / "f2.txt").exists()
    assert (service.base_dir / "runtime").read_text(encoding="utf-8") == "block repo pollution\n"
    conn = _db(db_path)
    event = conn.execute(
        "SELECT status FROM snapshot_restore_events ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert event["status"] == "completed"


def test_restore_with_runtime_storage_roots_does_not_recreate_legacy_repo_dirs(tmp_path):
    audit_log = []
    base = tmp_path / "app"
    base.mkdir()
    runtime_root = base / "runtime"
    storage_root = runtime_root / "storage"
    chat_root = runtime_root / "chats"
    storage_root.mkdir(parents=True)
    chat_root.mkdir(parents=True)
    db_path = base / "database.db"
    _init_db(db_path)

    def get_db():
        return _db(db_path)

    service = SnapshotService(
        get_db=get_db,
        db_path=db_path,
        base_dir=base,
        runtime_base_dir=runtime_root,
        storage_root=storage_root,
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
        file_roots=[storage_root, chat_root],
        config_files=[],
    )

    (storage_root / "asset.txt").write_text("v1", encoding="utf-8")
    (chat_root / "room.txt").write_text("chat-v1", encoding="utf-8")
    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="runtime roots only")

    (storage_root / "asset-v2.txt").write_text("v2", encoding="utf-8")
    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="runtime root restore")

    assert restored["ok"] is True
    assert (storage_root / "asset.txt").exists()
    assert not (storage_root / "asset-v2.txt").exists()
    for legacy in ("uploads", "avatars", "attachments", "media"):
        assert not (base / legacy).exists()


def test_server_mode_hmac_key_defaults_to_runtime_subdir_without_snapshot_service(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKME_RUNTIME_DIR", raising=False)
    mode = ServerModeService(snapshot_service=None, get_db=lambda: None, audit=lambda *args, **kwargs: None)

    path = mode._local_hmac_key_path("server_mode_log")

    assert path == (Path(__file__).resolve().parents[2] / "runtime" / ".server_mode_log_hmac_key").resolve()


def test_server_mode_hmac_key_fails_closed_when_runtime_path_is_a_file(tmp_path, monkeypatch):
    blocked = tmp_path / "runtime"
    blocked.write_text("block repo pollution\n", encoding="utf-8")
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(blocked))

    with pytest.raises(RuntimeError, match="runtime path is blocked by a non-directory file"):
        ServerModeService(snapshot_service=None, get_db=lambda: None, audit=lambda *args, **kwargs: None)


def test_restore_reverts_db_and_uploaded_files_and_creates_pre_restore(tmp_path):
    audit_log = []
    runtime_key = tmp_path / "app" / ".filekey"
    service, db_path, uploads = _service(tmp_path, audit_log, runtime_secret_files=[runtime_key])
    runtime_key.write_text("secret-key-v1", encoding="utf-8")
    (uploads / "f1.txt").write_text("one", encoding="utf-8")
    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="baseline")

    conn = _db(db_path)
    conn.execute("INSERT INTO posts (id, title) VALUES (2, 'P2')")
    conn.execute("UPDATE users SET base_level='normal', effective_level='restricted', member_level='restricted' WHERE id=1")
    conn.commit()
    conn.close()
    (uploads / "f2.txt").write_text("two", encoding="utf-8")
    runtime_key.write_text("secret-key-v2", encoding="utf-8")

    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="rollback")

    assert restored["ok"] is True
    assert restored["requires_restart"] is True
    assert restored["runtime_secret_validation"]["ok"] is True
    conn = _db(db_path)
    posts = [row["title"] for row in conn.execute("SELECT title FROM posts ORDER BY id").fetchall()]
    user = conn.execute("SELECT base_level, effective_level FROM users WHERE id=1").fetchone()
    event = conn.execute("SELECT status, pre_restore_snapshot_id FROM snapshot_restore_events ORDER BY started_at DESC LIMIT 1").fetchone()
    conn.close()
    assert posts == ["P1"]
    assert user["effective_level"] == "normal"
    assert (uploads / "f1.txt").exists()
    assert not (uploads / "f2.txt").exists()
    assert runtime_key.read_text(encoding="utf-8") == "secret-key-v1"
    assert event["status"] == "completed"
    assert event["pre_restore_snapshot_id"]
    assert (service.snapshots_root / event["pre_restore_snapshot_id"]).exists()
    assert any(call[0][0] == "SNAPSHOT_RESTORE_COMPLETED" for call in audit_log)


def test_restore_preparation_tolerates_legacy_system_settings_without_value_type(tmp_path):
    audit_log = []
    service, db_path, _uploads = _service(tmp_path, audit_log)
    conn = _db(db_path)
    conn.executescript(
        """
        ALTER TABLE system_settings RENAME TO system_settings_old;
        CREATE TABLE system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT,
            updated_by TEXT
        );
        INSERT INTO system_settings (key, value, updated_at, updated_by)
        SELECT key, value, updated_at, updated_by FROM system_settings_old;
        DROP TABLE system_settings_old;
        """
    )
    conn.commit()
    conn.close()

    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="legacy settings schema")
    conn = _db(db_path)
    conn.execute("INSERT INTO posts (id, title) VALUES (2, 'legacy dirty state')")
    conn.commit()
    conn.close()

    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="legacy restore")

    assert restored["ok"] is True
    conn = _db(db_path)
    posts = [row["title"] for row in conn.execute("SELECT title FROM posts ORDER BY id").fetchall()]
    conn.close()
    assert posts == ["P1"]


def test_restore_reports_failure_when_post_restore_validation_fails(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    service.set_post_restore_validators([
        ("trading_state", lambda: {"ok": False, "errors": [{"type": "test_failure"}]}),
    ])
    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="baseline")

    conn = _db(db_path)
    conn.execute("INSERT INTO posts (id, title) VALUES (2, 'P2')")
    conn.commit()
    conn.close()

    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="rollback")

    assert restored["ok"] is False
    assert restored["msg"] == "Restore 後驗證失敗"
    assert restored["post_restore_validation"]["errors"][0]["name"] == "trading_state"
    conn = _db(db_path)
    event = conn.execute("SELECT status, error_message FROM snapshot_restore_events ORDER BY started_at DESC LIMIT 1").fetchone()
    conn.close()
    assert event["status"] == "failed"
    assert "test_failure" in event["error_message"]
    assert any(call[0][0] == "SNAPSHOT_RESTORE_VALIDATION_FAILED" for call in audit_log)


def test_restore_fails_when_runtime_secret_validation_fails(tmp_path):
    audit_log = []
    runtime_key = tmp_path / "app" / ".filekey"
    service, db_path, uploads = _service(tmp_path, audit_log, runtime_secret_files=[runtime_key])
    runtime_key.write_text("secret-key-v1", encoding="utf-8")
    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="baseline")
    snapshot_dir = service._snapshot_dir(snap.snapshot_id)
    metadata = json.loads((snapshot_dir / "metadata.json").read_text(encoding="utf-8"))
    metadata["runtime_secret_files"][0]["sha256"] = "0" * 64
    (snapshot_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="rollback")

    assert restored["ok"] is False
    assert restored["msg"] == "Runtime 密鑰驗證失敗"
    assert restored["requires_restart"] is True
    assert restored["runtime_secret_validation"]["ok"] is False
    assert any(call[0][0] == "SNAPSHOT_RESTORE_RUNTIME_SECRETS_FAILED" for call in audit_log)


def test_restore_fails_closed_when_maintenance_mode_cannot_be_enabled(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="baseline")

    conn = _db(db_path)
    conn.execute("INSERT INTO posts (id, title) VALUES (2, 'dirty state')")
    conn.commit()
    conn.close()

    original_get_db = service.get_db

    class FailingMaintenanceConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if "UPDATE system_settings SET value='true'" in sql:
                raise sqlite3.OperationalError("maintenance update blocked")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def failing_get_db():
        return FailingMaintenanceConnection(original_get_db())

    service.get_db = failing_get_db
    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="rollback")

    assert restored["ok"] is False
    assert restored["msg"] == "還原準備失敗"
    assert restored["pre_restore_snapshot_id"]
    conn = _db(db_path)
    posts = [row["title"] for row in conn.execute("SELECT title FROM posts ORDER BY id").fetchall()]
    event = conn.execute("SELECT status, restore_mode, error_message FROM snapshot_restore_events ORDER BY started_at DESC LIMIT 1").fetchone()
    conn.close()
    assert posts == ["P1", "dirty state"]
    assert event["status"] == "failed"
    assert event["restore_mode"] == "prepare"
    assert "maintenance update blocked" in event["error_message"]
    assert any(call[0][0] == "SNAPSHOT_RESTORE_PREPARE_FAILED" for call in audit_log)


def test_portable_snapshot_archive_restores_on_different_instance(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source_audit = []
    source, source_db, source_uploads = _service(source_root, source_audit)
    (source_uploads / "portable.txt").write_text("portable source", encoding="utf-8")
    snap = source.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="portable baseline")
    exported = source.export_snapshot_archive(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"})

    assert exported["ok"] is True
    archive_path = Path(exported["path"])
    assert archive_path.exists()

    target_audit = []
    target, target_db, target_uploads = _service(target_root, target_audit)
    (target_uploads / "dirty.txt").write_text("dirty target", encoding="utf-8")
    conn = _db(target_db)
    conn.execute("INSERT INTO posts (id, title) VALUES (99, 'target dirty')")
    conn.commit()
    conn.close()

    restored = target.restore_snapshot_archive(
        actor={"id": 1, "username": "root"},
        archive_path=archive_path,
        reason="portable restore",
    )

    assert restored["ok"] is True
    assert restored["imported_snapshot_id"] == snap.snapshot_id
    conn = _db(target_db)
    posts = [row["title"] for row in conn.execute("SELECT title FROM posts ORDER BY id").fetchall()]
    snapshot_row = conn.execute("SELECT storage_path, db_dump_path FROM snapshots WHERE id=?", (snap.snapshot_id,)).fetchone()
    conn.close()
    assert posts == ["P1"]
    assert (target_uploads / "portable.txt").read_text(encoding="utf-8") == "portable source"
    assert not (target_uploads / "dirty.txt").exists()
    assert str(target.snapshots_root) in snapshot_row["storage_path"]
    assert Path(snapshot_row["db_dump_path"]).exists()
    assert any(call[0][0] == "SNAPSHOT_IMPORTED" for call in target_audit)


def test_snapshot_path_traversal_is_rejected(tmp_path):
    audit_log = []
    service, _, _ = _service(tmp_path, audit_log)
    try:
        service.verify_snapshot(snapshot_id="../bad")
    except ValueError as exc:
        assert "snapshot_id" in str(exc)
    else:
        raise AssertionError("path traversal snapshot id was not rejected")


def test_checksum_mismatch_blocks_restore(tmp_path):
    audit_log = []
    service, _, uploads = _service(tmp_path, audit_log)
    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="baseline")
    snapshot = service.get_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"})
    Path(snapshot["db_dump_path"]).write_bytes(b"corrupt")

    restored = service.restore_snapshot(snapshot_id=snap.snapshot_id, actor={"id": 1, "username": "root"}, reason="bad")

    assert restored["ok"] is False
    assert "checksum" in restored["msg"]


def test_unsafe_checksum_paths_are_rejected(tmp_path):
    audit_log = []
    service, _, _uploads = _service(tmp_path, audit_log)
    snap = service.create_snapshot(snapshot_type="manual", actor={"id": 1, "username": "root"}, notes="baseline")
    snapshot_dir = service._snapshot_dir(snap.snapshot_id)
    (snapshot_dir / "checksums.sha256").write_text(f"{'0' * 64}  ../outside.db\n", encoding="utf-8")

    verification = service.verify_snapshot(snapshot_id=snap.snapshot_id)

    assert verification["ok"] is False
    assert verification["msg"] == "checksum path 不安全"


def test_superweak_enter_and_exit_restore_rolls_back_dirty_state(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    actor = {"id": 1, "username": "root"}

    entered = mode.enter_superweak(actor=actor, confirm="ENABLE_SUPERWEAK", notes="weak test")
    assert entered["ok"] is True
    assert entered["mode"]["current_mode"] == "superweak"

    conn = _db(db_path)
    conn.execute("INSERT INTO posts (id, title) VALUES (2, 'superweak dirty')")
    conn.commit()
    conn.close()

    exited = mode.exit_superweak(actor=actor, action="restore", confirm="RESTORE_BEFORE_SUPERWEAK", reason="done")

    assert exited["ok"] is True
    assert exited["mode"]["current_mode"] == "dev_ready"
    conn = _db(db_path)
    count = conn.execute("SELECT COUNT(*) AS c FROM posts WHERE id=2").fetchone()["c"]
    conn.close()
    assert count == 0
    assert any(call[0][0] == "SUPERWEAK_EXIT_RESTORE" for call in audit_log)


def test_superweak_keep_dirty_state_requires_root_confirmation(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    actor = {"id": 1, "username": "root"}
    assert mode.enter_superweak(actor=actor, confirm="ENABLE_SUPERWEAK", notes="weak test")["ok"] is True

    kept = mode.exit_superweak(
        actor=actor,
        action="keep_dirty_state",
        confirm="KEEP_DIRTY_SUPERWEAK_STATE",
        reason="intentional",
    )

    assert kept["ok"] is False
    assert "禁止保留" in kept["msg"]


def test_custom_security_profile_can_be_saved_and_applied(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    saved_settings = []
    mode = ServerModeService(
        snapshot_service=service,
        get_db=lambda: _db(db_path),
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
        save_settings=lambda data: saved_settings.append(dict(data)) or dict(data),
    )
    actor = {"id": 1, "username": "root"}

    saved = mode.save_profile(
        name="staging_lockdown",
        label="Staging Lockdown",
        description="custom staging thresholds",
        settings={"ip_blocking_enabled": True, "integrity_guard_strict_mode": True},
        thresholds={"security_pending_chat_reports_threshold": 3},
        actor=actor,
    )
    assert saved["ok"] is True
    assert saved["profile"]["is_builtin"] is False

    switched = mode.switch_mode(target_mode="staging_lockdown", actor=actor, confirm="SWITCH_CUSTOM_MODE", notes="test custom")

    assert switched["ok"] is True
    assert switched["mode"]["current_mode"] == "staging_lockdown"
    assert saved_settings[-1]["ip_blocking_enabled"] is True
    assert saved_settings[-1]["security_pending_chat_reports_threshold"] == 3


def test_server_mode_audit_exports_use_runtime_reports_dir(tmp_path):
    audit_log = []
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    service, db_path, _uploads = _service(tmp_path, audit_log, runtime_base_dir=runtime_root)
    mode = ServerModeService(
        snapshot_service=service,
        get_db=lambda: _db(db_path),
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
    )
    actor = {"id": 1, "username": "root"}

    result = mode.switch_mode(target_mode="maintenance", actor=actor, confirm="ENTER_MAINTENANCE", notes="audit path test")

    assert result["ok"] is True
    expected_dir = runtime_root / "reports" / "server_mode_audit"
    assert mode.audit_export_dir == expected_dir
    assert expected_dir.exists()
    assert list(expected_dir.glob("*.json"))
    assert list(expected_dir.glob("server_mode_audit_*.jsonl"))
    assert list(expected_dir.glob("server_mode_audit_*.sha256"))
    assert any(call[0][0] == "SERVER_MODE_CHANGE" for call in audit_log)


def test_tester_token_shadow_layer_does_not_mutate_formal_user_or_points_chain(tmp_path):
    audit_log = []
    service, db_path, _uploads = _service(tmp_path, audit_log)
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO users (id, username, role, status, member_level, base_level, effective_level) VALUES (2, 'tester', 'user', 'active', 'normal', 'normal', 'normal')"
    )
    # tester tokens are scoped to ['test', 'internal_test'] modes. Default boot
    # is now 'dev_ready' (see SERVER_MODE_V2_PROFILE_MATRIX.md §Default Mode);
    # this test exercises the shadow layer so explicitly seat it in 'test' mode.
    conn.execute("UPDATE server_modes SET current_mode='test' WHERE id=1")
    conn.commit()
    conn.close()
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    actor = {"id": 1, "username": "root"}

    token = mode.create_tester_token(
        actor=actor,
        tester_user_id=2,
        allowed_routes=["/api/tester"],
        expires_at="2099-01-01T00:00:00",
        can_modify_own_role=True,
        can_modify_own_points=True,
    )
    assert token["ok"] is True
    tester_actor = {"id": 2, "username": "tester", "role": "user"}

    role_result = mode.set_tester_shadow_role(
        actor=tester_actor,
        token=token["token"],
        shadow_role="manager",
        route="/api/tester/shadow-role",
    )
    wallet_result = mode.adjust_tester_shadow_wallet(
        actor=tester_actor,
        token=token["token"],
        delta_points=500,
        reason="qa balance",
        route="/api/tester/shadow-wallet",
    )
    state = mode.tester_shadow_state(actor=tester_actor, token=token["token"], route="/api/tester/shadow-state")

    assert role_result["ok"] is True
    assert role_result["formal_users_table_changed"] is False
    assert wallet_result["ok"] is True
    assert wallet_result["formal_points_chain_changed"] is False
    assert state["shadow_role"]["shadow_role"] == "manager"
    assert state["shadow_wallet"]["balance_points"] == 500
    conn = _db(db_path)
    formal = conn.execute("SELECT role FROM users WHERE id=2").fetchone()
    points_tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='points_ledger'").fetchone()
    conn.close()
    assert formal["role"] == "user"
    assert points_tables is None


def test_create_tester_token_rejects_expired_or_timezone_aware_expiry(tmp_path):
    audit_log = []
    service, db_path, _uploads = _service(tmp_path, audit_log)
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO users (id, username, role, status, member_level, base_level, effective_level) VALUES (2, 'tester', 'user', 'active', 'normal', 'normal', 'normal')"
    )
    conn.execute("UPDATE server_modes SET current_mode='test' WHERE id=1")
    conn.commit()
    conn.close()
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    actor = {"id": 1, "username": "root"}

    expired = mode.create_tester_token(
        actor=actor,
        tester_user_id=2,
        allowed_routes=["/api/tester"],
        expires_at="2000-01-01T00:00:00",
    )
    aware = mode.create_tester_token(
        actor=actor,
        tester_user_id=2,
        allowed_routes=["/api/tester"],
        expires_at="2099-01-01T00:00:00+00:00",
    )

    assert expired["ok"] is False
    assert "未來時間" in expired["msg"]
    assert aware["ok"] is False
    assert "不含時區" in aware["msg"]


def _insert_passing_production_reports(db_path, *, server_mode="dev_ready"):
    from services.snapshots import PRODUCTION_REQUIRED_REPORT_TYPES

    conn = _db(db_path)
    ensure_snapshot_schema(conn)
    now = datetime.now().isoformat()
    key = os.environ.setdefault("SERVER_MODE_REPORT_HMAC_KEY", "pytest-production-report-key")
    key_version = os.environ.setdefault("SERVER_MODE_REPORT_HMAC_KEY_VERSION", "pytest-v1")
    for report_type in PRODUCTION_REQUIRED_REPORT_TYPES:
        raw_report = {"report_type": report_type, "status": "pass", "summary": "fixture"}
        raw_report_json = _canonical_json_text(raw_report)
        report_hash = f"sha256:{hashlib.sha256(raw_report_json.encode('utf-8')).hexdigest()}"
        signature_payload = {
            "report_type": report_type,
            "report_hash": report_hash,
            "target_commit": "test-commit",
            "target_branch": "test-branch",
            # server_mode must match what _current_production_target() returns
            # at runtime, otherwise target_match fails and the gate refuses to
            # accept the report. Default `dev_ready` matches the runtime
            # default when no server_modes row + no git repo dir is set.
            "server_mode": server_mode,
            "test_result": "pass",
            "pass": 1,
            "critical_findings_count": 0,
            "high_findings_count": 0,
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
            (id, report_type, report_hash, target_commit, target_branch, server_mode, test_result,
             pass, critical_findings_count, high_findings_count, unresolved_findings_json, tester, signature,
             raw_report_json, report_source, trust_level, key_version, verified_at, created_at)
            VALUES (?, ?, ?, 'test-commit', 'test-branch', ?, 'pass', 1, 0, 0, '[]', 'pytest', ?, ?, 'pytest_fixture', 'verified', ?, ?, ?)
            """,
            (f"rep_{report_type}", report_type, report_hash, server_mode, signature, raw_report_json, key_version, now, now),
        )
    conn.commit()
    conn.close()


def _write_production_gate_report_file(runtime_root, report_type, payload, *, signed=True, key_version=None):
    report_dir = Path(runtime_root) / "reports" / "security" / "production_gate"
    report_dir.mkdir(parents=True, exist_ok=True)
    data = dict(payload)
    data.setdefault("report_type", report_type)
    if signed:
        key = os.environ.setdefault("SERVER_MODE_REPORT_HMAC_KEY", "pytest-production-report-key")
        key_version = key_version or os.environ.setdefault("SERVER_MODE_REPORT_HMAC_KEY_VERSION", "pytest-v1")
        raw_report = data.get("raw_report") or {"report_type": data["report_type"], "status": data.get("test_result", "pass")}
        raw_report_json = _canonical_json_text(raw_report)
        report_hash = data.get("report_hash") or f"sha256:{hashlib.sha256(raw_report_json.encode('utf-8')).hexdigest()}"
        signature_payload = {
            "report_type": str(data.get("report_type") or report_type),
            "report_hash": report_hash,
            "target_commit": str(data.get("target_commit") or ""),
            "target_branch": str(data.get("target_branch") or ""),
            "server_mode": str(data.get("server_mode") or ""),
            "test_result": str(data.get("test_result") or "pass"),
            "pass": 1 if bool(data.get("pass", True)) else 0,
            "critical_findings_count": int(data.get("critical_findings_count") or 0),
            "high_findings_count": int(data.get("high_findings_count") or 0),
            "unresolved_findings_json": json.dumps(data.get("unresolved_findings") or [], ensure_ascii=False, sort_keys=True),
            "tester": str(data.get("tester") or "pytest"),
            "raw_report_json": raw_report_json,
            "report_source": str(data.get("report_source") or "filesystem_auto_detect"),
            "key_version": key_version,
        }
        data["raw_report"] = raw_report
        data["report_hash"] = report_hash
        data["key_version"] = key_version
        data["signature"] = f"hmac_sha256:{_hmac_sha256(key, _production_report_signature_payload(signature_payload))}"
    (report_dir / f"{report_type}_report.json").write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def test_production_report_upload_requires_signed_raw_report(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    actor = {"id": 1, "username": "root"}

    missing_raw = mode.upload_production_report(
        actor=actor,
        report_type="stress",
        report_hash="sha256:" + ("0" * 64),
        target_commit="abc123",
        target_branch="main",
        server_mode="preprod",
        test_result="pass",
        passed=True,
        critical_findings_count=0,
        high_findings_count=0,
        unresolved_findings=[],
        tester="root",
        signature="hmac_sha256:deadbeef",
    )
    assert missing_raw["ok"] is False
    assert missing_raw["reason"] == "missing_raw_report"


def test_production_report_upload_verifies_signature_and_hash(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    actor = {"id": 1, "username": "root"}
    raw_report = {"suite": "stress", "summary": "all pass", "checks": [{"name": "rate_limit", "ok": True}]}
    attestation = mode._prepare_production_report_attestation(
        report_type="stress",
        raw_report=raw_report,
        target_commit="abc123",
        target_branch="main",
        server_mode="preprod",
        test_result="pass",
        passed=True,
        critical_findings_count=0,
        high_findings_count=0,
        unresolved_findings=[],
        tester="root",
    )
    ok = mode.upload_production_report(
        actor=actor,
        report_type="stress",
        report_hash=attestation["report_hash"],
        target_commit="abc123",
        target_branch="main",
        server_mode="preprod",
        test_result="pass",
        passed=True,
        critical_findings_count=0,
        high_findings_count=0,
        unresolved_findings=[],
        tester="root",
        signature=attestation["signature"],
        raw_report=raw_report,
        key_version=attestation["key_version"],
    )
    assert ok["ok"] is True
    requirements = mode.production_requirements()
    assert requirements["reports"]["stress"]["signature_valid"] is True
    assert requirements["reports"]["stress"]["trust_level"] == "verified"

    bad_sig = mode.upload_production_report(
        actor=actor,
        report_type="permission",
        report_hash=attestation["report_hash"],
        target_commit="abc123",
        target_branch="main",
        server_mode="preprod",
        test_result="pass",
        passed=True,
        critical_findings_count=0,
        high_findings_count=0,
        unresolved_findings=[],
        tester="root",
        signature="hmac_sha256:deadbeef",
        raw_report=raw_report,
        key_version=attestation["key_version"],
    )
    assert bad_sig["ok"] is False
    assert "signature" in bad_sig["msg"]


def test_production_report_upload_reuses_current_connection_for_requirements(tmp_path, monkeypatch):
    # Clear cached HMAC env vars so this test deterministically exercises the
    # "_hmac_key opens a control-db connection to read current mode" branch.
    # An earlier test in the suite may have populated these via
    # os.environ.setdefault and that would short-circuit _hmac_key, dropping
    # the expected connection count from 2 → 1.
    monkeypatch.delenv("SERVER_MODE_REPORT_HMAC_KEY", raising=False)
    monkeypatch.delenv("SERVER_MODE_REPORT_HMAC_KEY_VERSION", raising=False)
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    calls = {"count": 0}

    def single_use_get_db():
        calls["count"] += 1
        if calls["count"] > 2:
            raise AssertionError("upload_production_report should not open an extra DB connection to compute requirements")
        return _db(db_path)

    mode = ServerModeService(
        snapshot_service=service,
        get_db=single_use_get_db,
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
    )
    actor = {"id": 1, "username": "root"}
    raw_report = {"suite": "stress", "summary": "all pass", "checks": [{"name": "rate_limit", "ok": True}]}
    attestation = mode._prepare_production_report_attestation(
        report_type="stress",
        raw_report=raw_report,
        target_commit="abc123",
        target_branch="main",
        server_mode="preprod",
        test_result="pass",
        passed=True,
        critical_findings_count=0,
        high_findings_count=0,
        unresolved_findings=[],
        tester="root",
    )
    calls["count"] = 0
    ok = mode.upload_production_report(
        actor=actor,
        report_type="stress",
        report_hash=attestation["report_hash"],
        target_commit="abc123",
        target_branch="main",
        server_mode="preprod",
        test_result="pass",
        passed=True,
        critical_findings_count=0,
        high_findings_count=0,
        unresolved_findings=[],
        tester="root",
        signature=attestation["signature"],
        raw_report=raw_report,
        key_version=attestation["key_version"],
    )
    assert ok["ok"] is True
    assert ok["requirements"]["reports"]["stress"]["signature_valid"] is True
    assert calls["count"] == 2


def test_production_requirements_fail_when_latest_report_signature_is_tampered(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    raw_report = {"suite": "stress", "summary": "all pass"}
    attestation = mode._prepare_production_report_attestation(
        report_type="stress",
        raw_report=raw_report,
        target_commit="abc123",
        target_branch="main",
        server_mode="preprod",
        test_result="pass",
        passed=True,
        critical_findings_count=0,
        high_findings_count=0,
        unresolved_findings=[],
        tester="root",
    )
    conn = _db(db_path)
    ensure_snapshot_schema(conn)
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO production_entry_reports
        (id, report_type, report_hash, target_commit, target_branch, server_mode, test_result,
         pass, critical_findings_count, high_findings_count, unresolved_findings_json, tester, signature,
         raw_report_json, report_source, trust_level, key_version, verified_at, created_at)
        VALUES (?, 'stress', ?, 'abc123', 'main', 'preprod', 'pass', 1, 0, 0, '[]', 'root', ?, ?, 'manual_signed_upload', 'verified', ?, ?, ?)
        """,
        ("rep_stress", attestation["report_hash"], attestation["signature"], attestation["raw_report_json"].replace("all pass", "tampered"), attestation["key_version"], now, now),
    )
    conn.commit()
    conn.close()
    requirements = mode.production_requirements()
    assert "stress" in requirements["failed"]
    assert requirements["reports"]["stress"]["signature_valid"] is False
    assert requirements["reports"]["stress"]["verification_reason"] in {"report_hash_mismatch", "signature_mismatch"}


def test_unverified_filesystem_report_cannot_override_verified_db_report(tmp_path):
    audit_log = []
    runtime_root = tmp_path / "runtime"
    service, db_path, _uploads = _service(tmp_path, audit_log, runtime_base_dir=runtime_root)
    # This test patches _current_production_target to return server_mode='test'
    # so the seed reports must be inserted with that same mode (otherwise the
    # target_match check fails before this test even reaches its assertion).
    _insert_passing_production_reports(db_path, server_mode="test")
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    mode._current_production_target = lambda conn=None: {"target_commit": "test-commit", "target_branch": "test-branch", "server_mode": "test"}
    _write_production_gate_report_file(
        runtime_root,
        "stress",
        {
            "report_type": "stress",
            "target_commit": "test-commit",
            "target_branch": "test-branch",
            "server_mode": "test",
            "test_result": "fail",
            "pass": False,
            "critical_findings_count": 0,
            "high_findings_count": 1,
            "unresolved_findings": [{"severity": "high"}],
            "tester": "pytest",
            "report_source": "filesystem_auto_detect",
            "raw_report": {"report_type": "stress", "status": "fail"},
        },
        signed=False,
    )
    requirements = mode.production_requirements()
    assert requirements["ok"] is True
    assert requirements["reports"]["stress"]["report_source"] == "pytest_fixture"
    assert requirements["reports"]["stress"]["trust_level"] == "verified"


def test_unsigned_filesystem_report_never_satisfies_production_gate(tmp_path):
    audit_log = []
    runtime_root = tmp_path / "runtime"
    service, db_path, _uploads = _service(tmp_path, audit_log, runtime_base_dir=runtime_root)
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    mode._current_production_target = lambda conn=None: {"target_commit": "test-commit", "target_branch": "test-branch", "server_mode": "test"}
    _write_production_gate_report_file(
        runtime_root,
        "stress",
        {
            "report_type": "stress",
            "target_commit": "test-commit",
            "target_branch": "test-branch",
            "server_mode": "test",
            "test_result": "pass",
            "pass": True,
            "tester": "pytest",
            "report_source": "filesystem_auto_detect",
            "raw_report": {"report_type": "stress", "status": "pass"},
        },
        signed=False,
    )
    requirements = mode.production_requirements()
    assert "stress" in requirements["failed"]
    assert requirements["reports"]["stress"]["trust_level"] == "unverified"
    assert requirements["reports"]["stress"]["signature_valid"] is False


def test_report_type_mismatch_file_is_rejected(tmp_path):
    audit_log = []
    runtime_root = tmp_path / "runtime"
    service, db_path, _uploads = _service(tmp_path, audit_log, runtime_base_dir=runtime_root)
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    mode._current_production_target = lambda conn=None: {"target_commit": "test-commit", "target_branch": "test-branch", "server_mode": "test"}
    _write_production_gate_report_file(
        runtime_root,
        "stress",
        {
            "report_type": "permission",
            "target_commit": "test-commit",
            "target_branch": "test-branch",
            "server_mode": "test",
            "test_result": "pass",
            "pass": True,
            "tester": "pytest",
            "report_source": "filesystem_auto_detect",
            "raw_report": {"report_type": "permission", "status": "pass"},
        },
        signed=True,
    )
    requirements = mode.production_requirements()
    assert "stress" in requirements["failed"]
    assert requirements["reports"]["stress"]["verification_reason"] == "report_type_mismatch"
    assert requirements["reports"]["stress"]["trust_level"] == "unverified"


def test_invalid_json_filesystem_report_is_warning_only(tmp_path):
    audit_log = []
    runtime_root = tmp_path / "runtime"
    report_dir = runtime_root / "reports" / "security" / "production_gate"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "stress_report.json").write_text("{not-json", encoding="utf-8")
    service, db_path, _uploads = _service(tmp_path, audit_log, runtime_base_dir=runtime_root)
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    mode._current_production_target = lambda conn=None: {"target_commit": "test-commit", "target_branch": "test-branch", "server_mode": "test"}
    requirements = mode.production_requirements()
    assert "stress" in requirements["failed"]
    assert requirements["reports"]["stress"]["verification_reason"] == "invalid_report_json"
    assert requirements["reports"]["stress"]["trust_level"] == "unverified"


def test_old_commit_signed_report_cannot_pass_current_target_gate(tmp_path):
    audit_log = []
    runtime_root = tmp_path / "runtime"
    service, db_path, _uploads = _service(tmp_path, audit_log, runtime_base_dir=runtime_root)
    _insert_passing_production_reports(db_path)
    mode = ServerModeService(snapshot_service=service, get_db=lambda: _db(db_path), audit=lambda *args, **kwargs: audit_log.append((args, kwargs)))
    mode._current_production_target = lambda conn=None: {"target_commit": "new-commit", "target_branch": "test-branch", "server_mode": "test"}
    requirements = mode.production_requirements()
    assert "stress" in requirements["failed"]
    assert requirements["reports"]["stress"]["signature_valid"] is True
    assert requirements["reports"]["stress"]["target_match"] is False
    assert requirements["reports"]["stress"]["target_verification_reason"] == "target_commit_mismatch"


def test_current_production_target_uses_previous_mode_after_entering_production(tmp_path):
    audit_log = []
    service, db_path, _uploads = _service(tmp_path, audit_log)
    control_db_path = tmp_path / "app" / "control.db"
    mode = ServerModeService(
        snapshot_service=service,
        get_db=lambda: _db(db_path),
        get_control_db=lambda: _db(control_db_path),
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
    )
    conn = _db(control_db_path)
    mode.ensure_schema(conn)
    conn.execute(
        """
        UPDATE server_modes
        SET current_mode='production', previous_mode='dev_ready'
        WHERE id=1
        """
    )
    conn.commit()
    target = mode._current_production_target(conn)
    conn.close()
    assert target["current_mode"] == "production"
    assert target["server_mode"] == "dev_ready"
    assert target["report_server_mode"] == "dev_ready"


def test_builtin_security_profiles_refresh_and_close_superweak_controls(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    conn = _db(db_path)
    conn.execute(
        "UPDATE security_profiles SET settings_json=? WHERE name='superweak'",
        ('{"audit_chain_enabled": true, "integrity_guard_enabled": true, "feature_economy_enabled": true}',),
    )
    conn.commit()
    ensure_snapshot_schema(conn)
    row = conn.execute("SELECT settings_json FROM security_profiles WHERE name='superweak'").fetchone()
    conn.close()

    refreshed = json.loads(row["settings_json"])
    assert refreshed["server_ssl_enabled"] is False
    assert refreshed["audit_chain_enabled"] is False
    assert refreshed["feature_audit_log_enabled"] is False
    assert refreshed["integrity_guard_enabled"] is False
    assert refreshed["ip_blocking_enabled"] is False
    assert refreshed["login_violation_enabled"] is False
    assert refreshed["feature_economy_enabled"] is False
    assert refreshed["captcha_mode"] == "none"


def test_production_mode_requires_confirmation_and_hardens_accounts(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    conn = _db(db_path)
    conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE users ADD COLUMN is_default_password INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, user_id INTEGER, is_revoked INTEGER NOT NULL DEFAULT 0, revoked_at TEXT)")
    conn.execute("INSERT INTO users (id, username, role, status, member_level, base_level, effective_level, must_change_password, is_default_password) VALUES (2, 'admin', 'manager', 'active', 'normal', 'normal', 'normal', 0, 0)")
    conn.execute("INSERT INTO users (id, username, role, status, member_level, base_level, effective_level, must_change_password, is_default_password) VALUES (3, 'test', 'user', 'active', 'trusted', 'trusted', 'trusted', 0, 1)")
    conn.execute("INSERT INTO sessions (id, user_id, is_revoked) VALUES (1, 3, 0)")
    ensure_upload_security_schema(conn)
    conn.commit()
    conn.close()
    saved_settings = []
    mode = ServerModeService(
        snapshot_service=service,
        get_db=lambda: _db(db_path),
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
        save_settings=lambda data: saved_settings.append(dict(data)) or dict(data),
    )
    actor = {"id": 1, "username": "root"}

    denied = mode.switch_mode(target_mode="production", actor=actor, confirm="", notes="go live")
    assert denied["ok"] is False
    assert "GO_LIVE" in denied["msg"]

    blocked = mode.switch_mode(target_mode="production", actor=actor, confirm="GO_LIVE", notes="go live")
    assert blocked["ok"] is False
    assert "production gate" in blocked["msg"]

    _insert_passing_production_reports(db_path)
    switched = mode.switch_mode(target_mode="production", actor=actor, confirm="GO_LIVE", notes="go live")
    assert switched["ok"] is True
    assert switched["mode"]["current_mode"] == "production"
    assert saved_settings[-1]["audit_chain_enabled"] is True
    assert saved_settings[-1]["feature_account_security_enabled"] is True
    assert saved_settings[-1]["captcha_mode"] == "math"
    assert switched["production"]["accounts"]["default_password_reset_required"] == 3
    assert switched["production"]["accounts"]["test_accounts_disabled"] == 1

    conn = _db(db_path)
    rows = {
        row["username"]: dict(row)
        for row in conn.execute("SELECT username, status, must_change_password, is_default_password FROM users")
    }
    session = conn.execute("SELECT is_revoked FROM sessions WHERE user_id=3").fetchone()
    policy = conn.execute("SELECT scanner_enabled, scanner_backend, fail_closed_on_scanner_error, yara_enabled FROM cloud_drive_security_policies WHERE scope='default'").fetchone()
    conn.close()
    assert rows["root"]["must_change_password"] == 1
    assert rows["admin"]["must_change_password"] == 1
    assert rows["test"]["status"] == "inactive"
    assert rows["test"]["must_change_password"] == 1
    assert session["is_revoked"] == 1
    assert policy["scanner_enabled"] == 1
    assert policy["scanner_backend"] == "clamav"
    assert policy["fail_closed_on_scanner_error"] == 1
    assert policy["yara_enabled"] == 1


def test_internal_test_mode_disables_open_registration_and_revokes_non_root_sessions(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    conn = _db(db_path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, user_id INTEGER, is_revoked INTEGER NOT NULL DEFAULT 0, revoked_at TEXT)")
    conn.execute("INSERT INTO users (id, username, role, status, member_level, base_level, effective_level) VALUES (2, 'alice', 'user', 'active', 'normal', 'normal', 'normal')")
    conn.execute("INSERT INTO sessions (id, user_id, is_revoked) VALUES (1, 1, 0)")
    conn.execute("INSERT INTO sessions (id, user_id, is_revoked) VALUES (2, 2, 0)")
    conn.commit()
    conn.close()
    saved_settings = []
    mode = ServerModeService(
        snapshot_service=service,
        get_db=lambda: _db(db_path),
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
        save_settings=lambda data: saved_settings.append(dict(data)) or dict(data),
    )

    switched = mode.switch_mode(
        target_mode="internal_test",
        actor={"id": 1, "username": "root"},
        confirm="SWITCH_TO_INTERNAL_TEST",
        notes="invite-only test",
    )

    assert switched["ok"] is True
    assert switched["mode"]["current_mode"] == "internal_test"
    assert saved_settings[-1]["allow_register"] is False
    assert saved_settings[-1]["feature_account_security_enabled"] is True
    assert switched["internal_test"]["sessions_revoked"] == 1
    conn = _db(db_path)
    sessions = {row["user_id"]: row["is_revoked"] for row in conn.execute("SELECT user_id, is_revoked FROM sessions")}
    conn.close()
    assert sessions[1] == 0
    assert sessions[2] == 1


def test_mode_switch_checkpoint_gate_and_independent_log(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    saved_settings = []
    mode = ServerModeService(
        snapshot_service=service,
        get_db=lambda: _db(db_path),
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
        save_settings=lambda data: saved_settings.append(dict(data)) or dict(data),
    )

    switched = mode.switch_mode(
        target_mode="maintenance",
        actor={"id": 1, "username": "root"},
        confirm="ENTER_MAINTENANCE",
        notes="migration window",
    )

    assert switched["ok"] is True
    assert switched["checkpoint"]["checkpoint_id"].startswith("chk_")
    assert switched["mode"]["current_mode"] == "maintenance"
    conn = _db(db_path)
    log = conn.execute("SELECT from_mode, to_mode, success, checkpoint_id FROM mode_switch_logs ORDER BY created_at DESC LIMIT 1").fetchone()
    checkpoint = conn.execute("SELECT status, target_mode FROM server_checkpoints WHERE id=?", (log["checkpoint_id"],)).fetchone()
    conn.close()
    assert log["from_mode"] == "dev_ready"
    assert log["to_mode"] == "maintenance"
    assert log["success"] == 1
    assert checkpoint["status"] == "ready"
    assert checkpoint["target_mode"] == "maintenance"


def test_mode_switch_checkpoint_failure_blocks_switch_and_logs_failure(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)

    def fail_snapshot(**kwargs):
        return type("Result", (), {"ok": False, "snapshot_id": "snap_20260427_153000_abcdef", "status": "failed", "error": "disk full", "metadata": {}})()

    service.create_snapshot = fail_snapshot
    mode = ServerModeService(
        snapshot_service=service,
        get_db=lambda: _db(db_path),
        audit=lambda *args, **kwargs: audit_log.append((args, kwargs)),
        save_settings=lambda data: data,
    )

    result = mode.switch_mode(
        target_mode="maintenance",
        actor={"id": 1, "username": "root"},
        confirm="ENTER_MAINTENANCE",
        notes="migration window",
    )

    assert result["ok"] is False
    conn = _db(db_path)
    current = conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()["current_mode"]
    log = conn.execute("SELECT to_mode, success, error_message FROM mode_switch_logs ORDER BY created_at DESC LIMIT 1").fetchone()
    conn.close()
    assert current == "incident_lockdown"
    assert log["to_mode"] == "incident_lockdown"
    assert log["success"] == 1


def test_daily_snapshot_runs_once_after_configured_time(tmp_path):
    audit_log = []
    service, _, _ = _service(tmp_path, audit_log)
    saved = []
    settings = {
        "snapshot_daily_auto_enabled": True,
        "snapshot_daily_time": "03:00",
        "snapshot_daily_last_date": "",
    }

    early = service.create_daily_snapshot_if_due(
        actor={"id": 0, "username": "system"},
        settings=settings,
        save_settings=saved.append,
        now=datetime(2026, 4, 27, 2, 59, 0),
    )
    assert early["created"] is False
    assert early["status"]["reason"] == "before_scheduled_time"

    due = service.create_daily_snapshot_if_due(
        actor={"id": 0, "username": "system"},
        settings=settings,
        save_settings=saved.append,
        now=datetime(2026, 4, 27, 3, 0, 0),
    )

    assert due["ok"] is True
    assert due["created"] is True
    assert saved[-1] == {"snapshot_daily_last_date": "2026-04-27"}
    snapshot = service.get_snapshot(snapshot_id=due["snapshot_id"], actor={"id": 1, "username": "root"})
    assert snapshot["type"] == "scheduled"


def test_runtime_reset_creates_pre_reset_snapshot_and_clears_runtime_tables_and_files(tmp_path):
    audit_log = []
    service, db_path, uploads = _service(tmp_path, audit_log)
    (uploads / "dirty.txt").write_text("dirty", encoding="utf-8")
    conn = _db(db_path)
    conn.execute("INSERT INTO posts (id, title) VALUES (2, 'dirty')")
    conn.commit()
    conn.close()

    result = service.reset_runtime_state(
        actor={"id": 1, "username": "root"},
        confirm="RESET_RUNTIME_STATE",
        reason="cleanup",
    )

    assert result["ok"] is True
    assert result["pre_reset_snapshot_id"].startswith("snap_")
    assert "posts" in result["cleared_tables"]
    assert not (uploads / "dirty.txt").exists()
    conn = _db(db_path)
    post_count = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()["c"]
    root_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE username='root'").fetchone()["c"]
    pre_reset = conn.execute("SELECT type, status FROM snapshots WHERE id=?", (result["pre_reset_snapshot_id"],)).fetchone()
    settings = {
        row["key"]: row["value"]
        for row in conn.execute(
            "SELECT key, value FROM system_settings WHERE key LIKE 'feature_%' OR key IN ('allow_register', 'snapshot_daily_auto_enabled', 'storage_maintenance_auto_enabled')"
        ).fetchall()
    }
    conn.close()
    assert post_count == 0
    assert root_count == 1
    assert pre_reset["type"] == "pre_reset"
    assert pre_reset["status"] == "ready"
    assert settings["feature_accounts_enabled"] == "True"
    assert settings["feature_audit_log_enabled"] == "True"
    assert settings["feature_snapshot_restore_enabled"] == "True"
    assert settings["feature_chat_enabled"] == "False"
    assert settings["feature_community_enabled"] == "False"
    assert settings["feature_storage_albums_enabled"] == "False"
    assert settings["feature_comfyui_enabled"] == "False"
    assert settings["feature_economy_enabled"] == "False"
    assert settings["feature_games_enabled"] == "False"
    assert settings["allow_register"] == "False"
    conn = _db(db_path)
    current_mode = conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()["current_mode"]
    conn.close()
    assert result["server_mode"] == "dev_ready"
    assert current_mode == "dev_ready"
    assert any(call[0][0] == "SYSTEM_RUNTIME_RESET" for call in audit_log)


def test_runtime_reset_removes_generated_secret_and_tls_files(tmp_path):
    audit_log = []
    base = tmp_path / "app"
    secret_names = [
        ".chain_seed",
        ".csrfkey",
        ".fkey",
        ".fley",
        ".integrity_key",
        "integrity_manifest.json",
        "cert.pem",
        "key.pem",
    ]
    service, _db_path, _uploads = _service(
        tmp_path,
        audit_log,
        runtime_secret_files=[base / name for name in secret_names],
    )
    for name in secret_names:
        (base / name).write_text(f"{name}-value", encoding="utf-8")

    result = service.reset_runtime_state(
        actor={"id": 1, "username": "root"},
        confirm="RESET_RUNTIME_STATE",
        reason="rotate generated runtime secrets",
    )

    assert result["ok"] is True
    assert result["requires_restart"] is True
    assert sorted(result["runtime_secret_files_removed"]) == sorted(secret_names)
    assert result["runtime_secret_files_skipped"] == []
    for name in secret_names:
        assert not (base / name).exists()
    reset_detail = next(call[1]["detail"] for call in audit_log if call[0][0] == "SYSTEM_RUNTIME_RESET")
    assert "runtime_secret_files_removed=" in reset_detail
    assert ".chain_seed" in reset_detail


def test_runtime_reset_invokes_points_and_audit_chain_resets(tmp_path):
    audit_log = []
    service, _db_path, _uploads = _service(tmp_path, audit_log)
    calls = {"points": [], "audit": []}
    service.reset_points_chain = lambda **kwargs: calls["points"].append(kwargs) or {"ok": True, "reset": True}
    service.reset_audit_chain = lambda *args, **kwargs: calls["audit"].append((args, kwargs)) or {"ok": True, "reset": True}

    result = service.reset_runtime_state(
        actor={"id": 1, "username": "root"},
        confirm="RESET_RUNTIME_STATE",
        reason="cleanup",
    )

    assert result["ok"] is True
    assert result["points_chain_reset"]["reset"] is True
    assert result["audit_chain_reset"]["reset"] is True
    assert result["server_mode"] == "dev_ready"
    assert result["management_only_settings"]["feature_accounts_enabled"] is True
    assert result["management_only_settings"]["feature_chat_enabled"] is False
    assert calls["points"][0]["pre_reset_snapshot_id"] == result["pre_reset_snapshot_id"]
    assert calls["points"][0]["reason"] == "cleanup"
    assert calls["audit"][0][0][0] == "SYSTEM_RUNTIME_RESET"
    assert calls["audit"][0][1]["write_event"] is False
    assert "points_chain_reset=True" in calls["audit"][0][1]["detail"]


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _passthrough(fn):
    return fn


class _FakeSnapshotService:
    def __init__(self):
        self.created = []
        self.download_path = None
        self.upload_restores = []

    def create_snapshot(self, *, snapshot_type, actor, notes=None):
        self.created.append((snapshot_type, actor["username"], notes))
        return type("Result", (), {"ok": True, "snapshot_id": "snap_20260427_153000_abcdef", "status": "ready"})()

    def list_snapshots(self, *, actor):
        return [{"id": "snap_20260427_153000_abcdef", "status": "ready", "type": "manual"}]

    def get_snapshot(self, *, snapshot_id, actor=None):
        return {"id": snapshot_id, "status": "ready"}

    def restore_snapshot(self, *, snapshot_id, actor, reason, dry_run=False):
        return {"ok": True, "snapshot_id": snapshot_id, "dry_run": dry_run}

    def export_snapshot_archive(self, *, snapshot_id, actor=None):
        if not self.download_path:
            return {"ok": False, "msg": "download path not configured"}
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "path": str(self.download_path),
            "filename": Path(self.download_path).name,
            "size_bytes": Path(self.download_path).stat().st_size,
        }

    def restore_snapshot_archive(self, *, actor, file_storage, reason, dry_run=False):
        self.upload_restores.append((actor["username"], getattr(file_storage, "filename", ""), reason, dry_run))
        return {"ok": True, "imported_snapshot_id": "snap_20260427_153000_abcdef", "dry_run": dry_run}

    def delete_snapshot(self, *, snapshot_id, actor, reason):
        return {"ok": True}

    def daily_snapshot_status(self, *, settings):
        return {"enabled": True, "due": True, "reason": "due"}

    def create_daily_snapshot_if_due(self, *, actor, settings, save_settings=None, force=False, notes=None):
        if save_settings:
            save_settings({"snapshot_daily_last_date": "2026-04-27"})
        return {"ok": True, "created": True, "snapshot_id": "snap_20260427_153000_abcdef", "force": force}

    def reset_runtime_state(self, *, actor, confirm, reason):
        if confirm != "RESET_RUNTIME_STATE":
            return {"ok": False, "msg": "confirm 必須等於 RESET_RUNTIME_STATE"}
        return {"ok": True, "pre_reset_snapshot_id": "snap_20260427_153000_abcdef", "cleared_tables": ["posts"]}


class _FakeServerModeService:
    def __init__(self):
        self.saved_profiles = []
        self.shadow_role = None
        self.shadow_wallet_balance = 0

    def get_current_mode(self):
        return {"current_mode": "dev_ready", "previous_mode": None, "active_snapshot_id": None}

    def list_profiles(self):
        return [{"name": "dev_ready", "label": "dev ready（準上線 / 開發就緒）", "is_builtin": True, "settings": {}, "thresholds": {}}] + self.saved_profiles

    def save_profile(self, **kwargs):
        profile = {
            "name": kwargs["name"],
            "label": kwargs["label"],
            "description": kwargs.get("description") or "",
            "settings": kwargs.get("settings") or {},
            "thresholds": kwargs.get("thresholds") or {},
            "is_builtin": False,
        }
        self.saved_profiles.append(profile)
        return {"ok": True, "profile": profile}

    def switch_mode(self, **kwargs):
        return {"ok": True, "mode": {"current_mode": kwargs["target_mode"]}}

    def exit_superweak(self, **kwargs):
        return {"ok": True, "mode": {"current_mode": "dev_ready"}}

    def create_mode_checkpoint(self, **kwargs):
        return {"ok": True, "checkpoint_id": "chk_test", "snapshot_id": "snap_20260427_153000_abcdef"}

    def validate_checkpoint_restore(self, **kwargs):
        return {"ok": True, "checkpoint_id": kwargs.get("checkpoint_id"), "checks": {"snapshot_verified": True}}

    def production_requirements(self):
        return {"ok": False, "missing": ["stress"], "failed": [], "required": ["stress"], "reports": {}}

    def mode_switch_logs(self, **kwargs):
        return [{"id": "mode_test", "from_mode": "test", "to_mode": "maintenance", "success": 1}]

    def upload_production_report(self, **kwargs):
        return {"ok": True, "report_id": "prodrep_test"}

    def enter_incident_lockdown(self, **kwargs):
        return {"ok": True, "incident_id": "incident_test", "mode": {"current_mode": "incident_lockdown"}}

    def incident_status(self):
        return {"ok": True, "incident": None, "mode": {"current_mode": "test"}}

    def resolve_incident(self, **kwargs):
        return {"ok": True, "incident_id": "incident_test"}

    def create_tester_token(self, **kwargs):
        return {"ok": True, "token_id": "tester_token_test", "token": "hmt_test"}

    def revoke_tester_token(self, **kwargs):
        return {"ok": True, "token_id": kwargs.get("token_id")}

    def list_tester_tokens(self):
        return [{"id": "tester_token_test", "tester_user_id": 2, "revoked_at": None}]

    def tester_shadow_state(self, **kwargs):
        role_payload = None
        if self.shadow_role:
            role_payload = {"tester_user_id": 2, "shadow_role": self.shadow_role, "original_role": "user"}
        return {
            "ok": True,
            "mode": "test",
            "token": {
                "id": "tester_token_test",
                "expires_at": "2026-05-03T00:00:00",
                "can_modify_own_role": True,
                "can_modify_own_points": True,
                "can_run_security_tests": False,
            },
            "shadow_wallet": {"tester_user_id": 2, "balance_points": self.shadow_wallet_balance},
            "shadow_role": role_payload,
        }

    def set_tester_shadow_role(self, **kwargs):
        self.shadow_role = kwargs.get("shadow_role")
        return {"ok": True, "shadow_role": self.shadow_role}

    def adjust_tester_shadow_wallet(self, **kwargs):
        self.shadow_wallet_balance += int(kwargs.get("delta_points") or 0)
        return {"ok": True, "balance_points": self.shadow_wallet_balance, "formal_points_chain_changed": False}


class _FakePointsService:
    def __init__(self, calls):
        self.calls = calls

    def force_seal_block(self, *, actor=None, reason=""):
        self.calls.append(("points_block", reason))
        return {"ok": True, "sealed": False, "reason": reason, "forced": True}


def _build_admin_app(actor_box, snapshot_service, restart_calls=None, extra_deps=None):
    app = Flask(__name__)
    app.testing = True
    if restart_calls is None:
        restart_calls = []
    deps = {
        "ANCHOR_DIR": ".",
        "BASE_DIR": ".",
        "CHAT_DIR": ".",
        "DB_PATH": "missing.db",
        "LOG_DIR": ".",
        "SERVER_LOG_PATH": "server.log",
        "STORAGE_DIR": ".",
        "activate_emergency_lockdown": lambda reason: None,
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": lambda: None,
        "get_feature_settings": lambda: {},
        "get_system_settings": lambda: {},
        "get_ua": lambda: "test-agent",
        "is_audit_chain_enabled": lambda: False,
        "json_resp": _json_resp,
        "repair_audit_chain": lambda **kwargs: {"entries_resealed": 0},
        "repair_violation_chains": lambda: {"entries_resealed": 0},
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "role_rank": lambda role: {"user": 0, "manager": 3, "super_admin": 4}.get(role or "user", 0),
        "save_feature_settings": lambda data: {},
        "save_settings": lambda data: data,
        "server_mode_service": _FakeServerModeService(),
        "snapshot_service": snapshot_service,
        "schedule_server_restart": lambda **kwargs: restart_calls.append(kwargs) or {"mode": "test"},
        "verify_audit_integrity": lambda: (True, None, "ok"),
    }
    if extra_deps:
        deps.update(extra_deps)
    register_system_admin_routes(app, deps)
    return app


def test_snapshot_api_is_root_only_and_supports_dry_run_restore():
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    client = _build_admin_app(actor_box, snapshot_service).test_client()

    created = client.post("/api/admin/snapshots", json={"type": "manual", "notes": "api"})
    assert created.status_code == 200
    assert created.get_json()["snapshot_id"] == "snap_20260427_153000_abcdef"

    dry_run = client.post(
        "/api/admin/snapshots/snap_20260427_153000_abcdef/restore",
        json={"confirm": "DRY_RUN", "dry_run": True, "reason": "validate"},
    )
    assert dry_run.status_code == 200
    assert dry_run.get_json()["dry_run"] is True

    actor_box["actor"] = {"id": 2, "username": "admin", "role": "manager"}
    denied = client.post("/api/admin/snapshots", json={"type": "manual"})
    assert denied.status_code == 403


def test_snapshot_api_seals_points_block_before_writing_snapshot():
    calls = []

    class OrderedSnapshotService(_FakeSnapshotService):
        def create_snapshot(self, *, snapshot_type, actor, notes=None):
            calls.append(("snapshot", snapshot_type))
            return super().create_snapshot(snapshot_type=snapshot_type, actor=actor, notes=notes)

    snapshot_service = OrderedSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    client = _build_admin_app(
        actor_box,
        snapshot_service,
        extra_deps={"points_service": _FakePointsService(calls)},
    ).test_client()

    created = client.post("/api/admin/snapshots", json={"type": "manual", "notes": "api"})

    assert created.status_code == 200
    assert created.get_json()["points_block"]["reason"] == "snapshot_create_pre"
    assert calls == [("points_block", "snapshot_create_pre"), ("snapshot", "manual")]


def test_system_reset_rejects_bad_confirm_without_sealing_points_block():
    calls = []
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    client = _build_admin_app(
        actor_box,
        snapshot_service,
        extra_deps={"points_service": _FakePointsService(calls)},
    ).test_client()

    denied = client.post("/api/admin/system-reset", json={"confirm": "WRONG"})

    assert denied.status_code == 400
    assert denied.get_json()["ok"] is False
    assert calls == []


def test_snapshot_api_downloads_and_restores_uploaded_portable_archive(tmp_path):
    snapshot_service = _FakeSnapshotService()
    archive = tmp_path / "snap_20260427_153000_abcdef.snapshot.tar.gz"
    archive.write_bytes(b"portable snapshot bytes")
    snapshot_service.download_path = archive
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    client = _build_admin_app(actor_box, snapshot_service).test_client()

    downloaded = client.get("/api/admin/snapshots/snap_20260427_153000_abcdef/download")
    assert downloaded.status_code == 200
    assert downloaded.data == b"portable snapshot bytes"
    assert "attachment" in downloaded.headers["Content-Disposition"]

    restored = client.post(
        "/api/admin/snapshots/upload-restore",
        data={
            "confirm": "RESTORE",
            "reason": "api portable restore",
            "file": (io.BytesIO(b"portable upload"), "portable.snapshot.tar.gz"),
        },
        content_type="multipart/form-data",
    )
    assert restored.status_code == 200
    assert restored.get_json()["imported_snapshot_id"] == "snap_20260427_153000_abcdef"
    assert snapshot_service.upload_restores == [("root", "portable.snapshot.tar.gz", "api portable restore", False)]

    dry_run = client.post(
        "/api/admin/snapshots/upload-restore",
        data={
            "confirm": "DRY_RUN",
            "dry_run": "true",
            "file": (io.BytesIO(b"portable upload"), "portable.snapshot.tar.gz"),
        },
        content_type="multipart/form-data",
    )
    assert dry_run.status_code == 200
    assert dry_run.get_json()["dry_run"] is True


def test_daily_snapshot_and_reset_api_are_root_only():
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    restart_calls = []
    client = _build_admin_app(actor_box, snapshot_service, restart_calls).test_client()

    daily = client.post("/api/admin/snapshots/daily", json={"confirm": "RUN_DAILY_SNAPSHOT", "force": True})
    assert daily.status_code == 200
    assert daily.get_json()["created"] is True

    reset = client.post("/api/admin/system-reset", json={"confirm": "RESET_RUNTIME_STATE", "reason": "test"})
    assert reset.status_code == 200
    reset_data = reset.get_json()
    assert reset_data["pre_reset_snapshot_id"] == "snap_20260427_153000_abcdef"
    assert reset_data["restart_scheduled"] is True
    assert restart_calls == [{"reason": "system-reset", "delay_seconds": 1.25}]

    actor_box["actor"] = {"id": 2, "username": "admin", "role": "manager"}
    denied = client.post("/api/admin/system-reset", json={"confirm": "RESET_RUNTIME_STATE"})
    assert denied.status_code == 403


def test_manual_restart_uses_restart_scheduler():
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    restart_calls = []
    client = _build_admin_app(actor_box, snapshot_service, restart_calls).test_client()

    res = client.post("/api/admin/restart")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["restart_scheduled"] is True
    assert restart_calls == [{"reason": "manual-restart", "delay_seconds": 1.25}]


def test_restart_launcher_waits_for_parent_exit_and_port_release():
    code = restart_launcher_code()
    assert "parent_alive()" in code
    assert "port_is_free()" in code
    assert "close_fds=True" in code
    assert "start_new_session=True" in code
    assert "subprocess.Popen([python_exe, script_path]" in code


def test_runtime_restart_wiring_preserves_current_bind_port():
    server_py = (ROOT / "server.py").read_text(encoding="utf-8")
    system_admin_py = (ROOT / "routes" / "system_admin.py").read_text(encoding="utf-8")

    assert "CURRENT_SERVER_BIND_STATE = SERVER_BIND_STATE" in server_py
    assert 'os.environ.get("HTML_LEARNING_PORT")' in system_admin_py


def test_security_profile_api_validates_and_server_mode_lists_profiles():
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    client = _build_admin_app(actor_box, snapshot_service).test_client()

    mode = client.get("/api/admin/server-mode")
    assert mode.status_code == 200
    assert mode.get_json()["profiles"][0]["name"] == "dev_ready"

    invalid = client.post("/api/admin/security-center/profiles", json={
        "name": "custom_lock",
        "label": "Custom Lock",
        "settings": {"not_a_setting": True},
        "thresholds": {},
    })
    assert invalid.status_code == 400
    assert "不支援的 settings key" in invalid.get_json()["msg"]

    saved = client.post("/api/admin/security-center/profiles", json={
        "name": "custom_lock",
        "label": "Custom Lock",
        "settings": {"ip_blocking_enabled": True},
        "thresholds": {"security_pending_chat_reports_threshold": 2},
    })
    assert saved.status_code == 200
    assert saved.get_json()["profile"]["settings"]["ip_blocking_enabled"] is True


def test_server_mode_v2_root_api_is_root_only_and_exposes_requirements():
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    client = _build_admin_app(actor_box, snapshot_service).test_client()

    status = client.get("/api/root/server-mode")
    assert status.status_code == 200
    assert status.get_json()["production_requirements"]["missing"] == ["stress"]

    checkpoint = client.post("/api/root/server-mode/checkpoint", json={"target_mode": "maintenance", "reason": "test"})
    assert checkpoint.status_code == 200
    assert checkpoint.get_json()["checkpoint_id"] == "chk_test"

    report = client.post("/api/root/production-report/upload", json={
        "report_type": "stress",
        "report_hash": "hash",
        "passed": True,
    })
    assert report.status_code == 200
    assert report.get_json()["report_id"] == "prodrep_test"

    doc = client.get("/api/root/launch-check/doc?path=docs/API_REFERENCE.md")
    assert doc.status_code == 200
    assert "API Reference" in doc.get_json()["content"]

    traversal = client.get("/api/root/launch-check/doc?path=../server.py")
    assert traversal.status_code == 400

    logs = client.get("/api/root/server-mode/logs")
    assert logs.status_code == 200
    assert logs.get_json()["logs"][0]["id"] == "mode_test"

    restore_check = client.post("/api/root/server-mode/restore-check", json={"checkpoint_id": "chk_test"})
    assert restore_check.status_code == 200
    assert restore_check.get_json()["checks"]["snapshot_verified"] is True

    token = client.post("/api/root/tester-token/create", json={
        "tester_user_id": 2,
        "expires_at": "2026-05-03T00:00:00",
        "allowed_routes": ["/api/me"],
    })
    assert token.status_code == 200
    assert token.get_json()["token_id"] == "tester_token_test"

    tokens = client.get("/api/root/tester-token/list")
    assert tokens.status_code == 200
    assert tokens.get_json()["tokens"][0]["id"] == "tester_token_test"

    actor_box["actor"] = {"id": 2, "username": "test", "role": "user"}
    shadow_state = client.get("/api/tester/shadow-state", headers={"X-Tester-Token": "hmt_test"})
    assert shadow_state.status_code == 200
    shadow_role_read = client.get("/api/tester/shadow-role", headers={"X-Tester-Token": "hmt_test"})
    assert shadow_role_read.status_code == 200
    assert shadow_role_read.get_json()["ok"] is True
    shadow_wallet_read = client.get("/api/tester/shadow-wallet", headers={"X-Tester-Token": "hmt_test"})
    assert shadow_wallet_read.status_code == 200
    assert shadow_wallet_read.get_json()["ok"] is True
    shadow_role = client.post("/api/tester/shadow-role", json={"role": "manager"}, headers={"X-Tester-Token": "hmt_test"})
    assert shadow_role.status_code == 200
    shadow_wallet = client.post("/api/tester/shadow-wallet", json={"delta_points": 10}, headers={"X-Tester-Token": "hmt_test"})
    assert shadow_wallet.status_code == 200
    assert shadow_wallet.get_json()["formal_points_chain_changed"] is False
    shadow_role_after = client.get("/api/tester/shadow-role", headers={"X-Tester-Token": "hmt_test"})
    assert shadow_role_after.status_code == 200
    assert shadow_role_after.get_json()["shadow_role"]["shadow_role"] == "manager"
    shadow_wallet_after = client.get("/api/tester/shadow-wallet", headers={"X-Tester-Token": "hmt_test"})
    assert shadow_wallet_after.status_code == 200
    assert shadow_wallet_after.get_json()["shadow_wallet"]["balance_points"] == 10

    actor_box["actor"] = {"id": 2, "username": "admin", "role": "manager"}
    denied = client.get("/api/root/server-mode")
    assert denied.status_code == 403


def test_security_center_and_server_output_are_root_only():
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 2, "username": "admin", "role": "manager"}}
    client = _build_admin_app(actor_box, snapshot_service).test_client()

    denied_center = client.get("/api/admin/security-center")
    denied_output = client.get("/api/admin/server-output")
    assert denied_center.status_code == 403
    assert denied_output.status_code == 403

    actor_box["actor"] = {"id": 1, "username": "root", "role": "super_admin"}
    output = client.get("/api/admin/server-output?limit=10")
    assert output.status_code == 200
    assert output.get_json()["ok"] is True


def test_security_center_payload_includes_full_production_gate_settings(tmp_path):
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    settings = {
        "allow_register": False,
        "audit_chain_enabled": True,
        "browser_only_mode_enabled": True,
        "captcha_mode": "math",
        "feature_audit_log_enabled": True,
        "feature_economy_enabled": True,
        "integrity_guard_enabled": True,
        "integrity_guard_strict_mode": True,
        "ip_blocking_enabled": True,
        "login_violation_enabled": True,
        "maintenance_mode": False,
        "production_single_account_ip_lock_enabled": False,
        "production_single_ip_account_lock_enabled": False,
        "rate_limit_violation_enabled": True,
        "root_ip_whitelist": "",
        "root_ip_whitelist_enabled": False,
        "server_ssl_enabled": True,
    }
    db_path = tmp_path / "database.db"
    auth_db_path = tmp_path / "auth.db"
    log_dir = tmp_path / "logs"
    chat_dir = tmp_path / "chats"
    anchor_dir = tmp_path / "anchors"
    storage_dir = tmp_path / "storage"
    for path in (log_dir, chat_dir, anchor_dir, storage_dir):
        path.mkdir()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        INSERT INTO users (id, username, status) VALUES (1, 'root', 'active');
        """
    )
    conn.commit()
    conn.close()
    auth_conn = sqlite3.connect(auth_db_path)
    auth_conn.row_factory = sqlite3.Row
    auth_conn.executescript(
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY,
            expires_at TEXT,
            is_revoked INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    auth_conn.commit()
    auth_conn.close()

    def _get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_auth_db():
        conn = sqlite3.connect(auth_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    client = _build_admin_app(
        actor_box,
        snapshot_service,
        extra_deps={
            "ANCHOR_DIR": str(anchor_dir),
            "CHAT_DIR": str(chat_dir),
            "DB_PATH": str(db_path),
            "LOG_DIR": str(log_dir),
            "SERVER_LOG_PATH": str(log_dir / "server.log"),
            "STORAGE_DIR": str(storage_dir),
            "get_auth_db": _get_auth_db,
            "get_db": _get_db,
            "get_system_settings": lambda: settings,
            "get_feature_settings": lambda: {"feature_audit_log_enabled": True, "feature_economy_enabled": True},
            "verify_audit_integrity": lambda: (True, None, "ok"),
        },
    ).test_client()

    res = client.get("/api/admin/security-center")
    assert res.status_code == 200
    payload = res.get_json()["security_center"]["settings"]
    for key in (
        "allow_register",
        "captcha_mode",
        "production_single_account_ip_lock_enabled",
        "production_single_ip_account_lock_enabled",
    ):
        assert key in payload
        assert payload[key] == settings[key]


def test_server_output_falls_back_to_gunicorn_error_log_when_runtime_buffer_is_empty(tmp_path):
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "server.log").write_text("", encoding="utf-8")
    (log_dir / "gunicorn_error.log").write_text(
        "[2026-05-08 10:52:48 +0800] [837961] [INFO] Starting gunicorn 26.0.0\n",
        encoding="utf-8",
    )
    client = _build_admin_app(
        actor_box,
        snapshot_service,
        extra_deps={
            "LOG_DIR": str(log_dir),
            "SERVER_LOG_PATH": str(log_dir / "server.log"),
            "get_server_output": lambda limit=200: {"lines": [], "max_lines": 2000},
        },
    ).test_client()

    response = client.get("/api/admin/server-output?limit=10")

    assert response.status_code == 200
    payload = response.get_json()["server_output"]
    assert payload["lines"]
    assert payload["lines"][0]["stream"] == "info"
    assert "Starting gunicorn 26.0.0" in payload["lines"][0]["line"]


def test_server_output_falls_back_to_server_direct_log_when_gunicorn_error_missing(tmp_path):
    snapshot_service = _FakeSnapshotService()
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "server.log").write_text("", encoding="utf-8")
    (log_dir / "server_direct.out").write_text(
        "127.0.0.1 - - [25/May/2026:11:39:24 +0800] \"GET /api/version HTTP/1.1\" 200 412\n",
        encoding="utf-8",
    )
    client = _build_admin_app(
        actor_box,
        snapshot_service,
        extra_deps={
            "LOG_DIR": str(log_dir),
            "SERVER_LOG_PATH": str(log_dir / "server.log"),
            "get_server_output": lambda limit=200: {"lines": [], "max_lines": 2000},
        },
    ).test_client()

    response = client.get("/api/admin/server-output?limit=10")

    assert response.status_code == 200
    payload = response.get_json()["server_output"]
    assert payload["source"] == "server_direct.out"
    assert payload["lines"]
    assert "GET /api/version" in payload["lines"][0]["line"]
