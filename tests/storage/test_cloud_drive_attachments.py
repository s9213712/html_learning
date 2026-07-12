import gzip
import io
import json
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from flask import Flask, jsonify, make_response

import routes.files as files_routes
import routes.file_sections.share_preview_routes as share_preview_routes
from routes.files import register_file_routes
from services.storage.cloud_drive import (
    decrypt_server_encrypted_bytes,
    ensure_cloud_drive_attachment_schema,
    share_e2ee_file,
)
from services.media.streaming import ensure_media_stream_schema
from services.users.member_levels import ensure_member_level_rules_schema
from services.storage.storage_albums import ensure_storage_album_schema
from services.security.upload_security import ensure_upload_security_schema, update_cloud_drive_security_policy
from services.users.friends import ensure_social_schema
from services.job_center import create_job


@pytest.fixture(autouse=True)
def _clear_remote_download_globals():
    """Reset module-level remote-download state before every test so tests don't bleed into each other."""
    with files_routes._REMOTE_DOWNLOAD_TASKS_LOCK:
        files_routes._REMOTE_DOWNLOAD_TASKS.clear()
        files_routes._REMOTE_DOWNLOAD_ACTIVE_USERS.clear()
    yield
    # Also clear after the test so lingering background threads don't affect later tests.
    with files_routes._REMOTE_DOWNLOAD_TASKS_LOCK:
        files_routes._REMOTE_DOWNLOAD_TASKS.clear()
        files_routes._REMOTE_DOWNLOAD_ACTIVE_USERS.clear()


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _passthrough(fn):
    return fn


def _build_app(db_path, storage_root, actor_box, points_service=None, server_file_fernet=None, settings=None):
    app = Flask(__name__)
    app.testing = True

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    register_file_routes(app, {
        "STORAGE_DIR": str(storage_root),
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": get_db,
        "get_system_settings": lambda: settings or {"storage_trash_retention_days": 30},
        "get_member_level_rule": lambda conn, level: {
            "can_upload_attachment": True,
            "attachment_quota_mb": 1,
            "max_attachment_size_mb": 1,
            "upload_rate_limit_per_day": 10,
        },
        "get_ua": lambda: "test-agent",
        "json_resp": _json_resp,
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "points_service": points_service,
        "server_file_fernet": server_file_fernet or Fernet(Fernet.generate_key()),
    })
    return app


def _init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL
        );
        INSERT INTO users (id, username, role) VALUES
          (1, 'alice', 'user'),
          (2, 'bob', 'user'),
          (3, 'mallory', 'user'),
          (4, 'admin', 'manager'),
          (5, 'root', 'super_admin');
        CREATE TABLE announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            author_user_id INTEGER NOT NULL,
            author_username TEXT NOT NULL,
            is_pinned INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO announcements (id, title, content, author_user_id, author_username, created_at, updated_at)
        VALUES (1, '公告', '內容', 4, 'admin', '2026-01-01T00:00:00', '2026-01-01T00:00:00');
        """
    )
    ensure_member_level_rules_schema(conn)
    ensure_upload_security_schema(conn)
    ensure_cloud_drive_attachment_schema(conn)
    ensure_storage_album_schema(conn)
    ensure_social_schema(conn)
    conn.execute(
        """
        INSERT INTO user_friends (user_id, friend_user_id, status, requested_by, created_at, updated_at)
        VALUES (1, 2, 'accepted', 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """
    )
    update_cloud_drive_security_policy(conn, {"scanner_enabled": False})
    conn.commit()
    conn.close()


def _actor(user_id, username, role="user"):
    return {
        "id": user_id,
        "username": username,
        "role": role,
        "member_level": "trusted",
        "effective_level": "trusted",
        "sanction_status": "none",
    }


def test_privacy_modes_explain_server_readability_and_e2ee_tradeoffs(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    res = client.get("/api/files/privacy-modes")

    assert res.status_code == 200
    body = res.get_json()
    modes = body["modes"]
    assert modes["standard_plain"]["server_can_read"] is True
    assert modes["standard_plain"]["stored_at_rest"] == "plaintext"
    assert modes["server_encrypted"]["server_can_read"] == "decryptable"
    assert modes["server_encrypted"]["stored_at_rest"] == "encrypted"
    assert "不是端到端加密" in modes["server_encrypted"]["warning"]
    assert modes["e2ee"]["server_can_read"] is False
    assert modes["e2ee"]["stored_at_rest"] == "encrypted"
    assert "E2EE 密碼" in modes["e2ee"]["warning"]


def test_storage_upgrade_catalog_falls_back_when_points_schema_is_locked(tmp_path):
    class LockedPointsService:
        def ensure_schema(self, conn):
            raise sqlite3.OperationalError("database is locked")

        def list_catalog(self):
            raise AssertionError("storage upgrade catalog should not open a second connection")

    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box, points_service=LockedPointsService()).test_client()

    res = client.get("/api/cloud-drive/storage-upgrades")
    body = res.get_json()

    assert res.status_code == 200
    assert body["ok"] is True
    assert [item["item_key"] for item in body["catalog"]] == ["cloud_storage_1gb_7d", "cloud_storage_1gb_30d"]
    assert body["catalog"][0]["duration_days"] == 7
    assert body["catalog"][0]["label"] == "雲端容量 1GB / 7 天"
    assert body["catalog"][1]["duration_days"] == 30
    assert body["catalog"][1]["label"] == "雲端容量 1GB / 30 天"


def test_encrypted_cloud_uploads_hide_physical_filenames_but_keep_display_name(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    fernet = Fernet(Fernet.generate_key())
    client = _build_app(db_path, storage_root, actor_box, server_file_fernet=fernet).test_client()

    e2ee = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(b"browser ciphertext"), "secret-family-photo.jpg"),
            "privacy_mode": "e2ee",
            "encrypted_metadata": "client-encrypted-name-and-mime",
            "encrypted_file_key": "client-wrapped-key",
        },
        content_type="multipart/form-data",
    )
    assert e2ee.status_code == 200
    server_encrypted = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"server secret"), "payroll-2026.pdf"), "privacy_mode": "server_encrypted"},
        content_type="multipart/form-data",
    )
    assert server_encrypted.status_code == 200

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        e2ee_id = e2ee.get_json()["file"]["file_id"]
        server_id = server_encrypted.get_json()["file"]["file_id"]
        e2ee_row = conn.execute("SELECT storage_path, original_filename_plain_for_public FROM uploaded_files WHERE id=?", (e2ee_id,)).fetchone()
        server_row = conn.execute("SELECT storage_path, original_filename_plain_for_public FROM uploaded_files WHERE id=?", (server_id,)).fetchone()
    finally:
        conn.close()

    assert e2ee_row["original_filename_plain_for_public"] == "secret-family-photo.jpg"
    assert server_row["original_filename_plain_for_public"] == "payroll-2026.pdf"
    assert Path(e2ee_row["storage_path"]).name.endswith(".e2ee")
    assert Path(server_row["storage_path"]).name.endswith(".server_encrypt")
    assert "secret-family-photo" not in e2ee_row["storage_path"]
    assert "payroll-2026" not in server_row["storage_path"]
    assert (storage_root / e2ee_row["storage_path"]).exists()
    assert (storage_root / server_row["storage_path"]).exists()


def test_cloud_drive_delete_removes_physical_file_and_releases_quota(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"delete me"), "delete-me.txt"), "privacy_mode": "standard_plain"},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT storage_path FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
    finally:
        conn.close()
    stored_path = storage_root / row["storage_path"]
    assert stored_path.exists()
    assert client.get("/api/files/quota").get_json()["quota"]["used_bytes"] == len(b"delete me")

    deleted = client.delete(f"/api/cloud-drive/files/{file_id}")
    body = deleted.get_json()

    assert deleted.status_code == 200
    assert body["ok"] is True
    assert body["deleted"]["removed_physical_file"] is True
    assert not stored_path.exists()
    assert client.get("/api/files/quota").get_json()["quota"]["used_bytes"] == 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT deleted_at FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
    finally:
        conn.close()
    assert row["deleted_at"]


def test_dm_upload_enters_owner_drive_and_grants_counterparty_download(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(b"hello bob"), "hello.txt"),
            "context_type": "dm",
            "context_id": "room-1",
            "grant_user_ids": "2",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    files = client.get("/api/cloud-drive/files")
    assert files.status_code == 200
    assert files.get_json()["files"][0]["id"] == file_id

    quota = client.get("/api/files/quota").get_json()["quota"]
    assert quota["used_bytes"] == len(b"hello bob")

    actor_box["actor"] = _actor(2, "bob")
    download = client.get(f"/api/cloud-drive/files/{file_id}/download")
    assert download.status_code == 200
    assert download.data == b"hello bob"

    actor_box["actor"] = _actor(3, "mallory")
    denied = client.get(f"/api/cloud-drive/files/{file_id}/download")
    assert denied.status_code == 403

    actor_box["actor"] = _actor(4, "admin", "manager")
    manager_download = client.get(f"/api/cloud-drive/files/{file_id}/download")
    assert manager_download.status_code == 403
    manager_status = client.get(f"/api/files/{file_id}/status")
    assert manager_status.status_code == 403
    manager_refs = client.get("/api/cloud-drive/refs?context_type=dm&context_id=room-1")
    assert manager_refs.status_code == 200
    assert manager_refs.get_json()["refs"] == []


def test_plain_cloud_drive_download_can_use_x_accel_offload(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    monkeypatch.setenv("HACKME_CLOUD_DRIVE_X_ACCEL_PREFIX", "/_protected/storage")
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"plain offload"), "plain.txt")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT storage_path FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
    finally:
        conn.close()

    download = client.get(f"/api/cloud-drive/files/{file_id}/download")

    assert download.status_code == 200
    assert download.headers["X-Accel-Redirect"] == f"/_protected/storage/{row['storage_path']}"
    assert download.headers["Accept-Ranges"] == "bytes"
    assert download.headers["X-Hackme-Transfer-Mode"] == "x_accel"
    assert download.headers["X-Hackme-Transfer-Offload"] == "x_accel"
    assert "attachment" in download.headers["Content-Disposition"]
    assert download.data == b""


def test_e2ee_preview_ciphertext_can_use_x_accel_offload(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    monkeypatch.setenv("HACKME_CLOUD_DRIVE_X_ACCEL_PREFIX", "/_protected/storage")
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(b"browser ciphertext"), "vault.bin"),
            "privacy_mode": "e2ee",
            "encrypted_metadata": '{"ciphertext":"metadata"}',
            "encrypted_file_key": '{"ciphertext":"wrapped-key"}',
            "wrapped_by": "browser_passphrase_pbkdf2_v2",
            "ciphertext_sha256": "0" * 64,
            "encryption_algorithm": "AES-GCM",
            "encryption_version": "browser-passphrase-v2",
            "nonce": "nonce",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT storage_path FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
    finally:
        conn.close()

    preview = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")

    assert preview.status_code == 200
    assert preview.headers["X-Accel-Redirect"] == f"/_protected/storage/{row['storage_path']}"
    assert preview.headers["X-Hackme-Transfer-Mode"] == "x_accel"
    assert preview.data == b""


def test_server_encrypted_download_ignores_x_accel_and_streams_plaintext(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    monkeypatch.setenv("HACKME_CLOUD_DRIVE_X_ACCEL_PREFIX", "/_protected/storage")
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"secret offload guard"), "secret.txt"), "privacy_mode": "server_encrypted"},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    download = client.get(f"/api/cloud-drive/files/{file_id}/download")

    assert download.status_code == 200
    assert "X-Accel-Redirect" not in download.headers
    assert download.headers["X-Hackme-Transfer-Mode"] == "python_chunked_decrypt"
    assert download.data == b"secret offload guard"


def test_cloud_drive_upload_failure_returns_specific_reason(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    res = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"cipher"), "vault.bin"), "privacy_mode": "e2ee"},
        content_type="multipart/form-data",
    )

    assert res.status_code == 400
    body = res.get_json()
    assert body["ok"] is False
    assert "encrypted_file_key is required" in body["msg"]
    assert body["error_code"] == "ValueError"


def test_cloud_drive_upload_records_job_center_entry(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"job center upload"), "job-center.txt")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT * FROM job_center_jobs
            WHERE source_module='cloud_drive_upload' AND source_ref=?
            """,
            (f"cloud_file:{file_id}",),
        ).fetchone()
        assert job is not None
        assert job["owner_user_id"] == 1
        assert job["status"] == "succeeded"
        assert job["progress_percent"] == 100
        assert "job-center.txt" in job["title"]
    finally:
        conn.close()


def test_cloud_drive_resumable_upload_can_resume_chunks_and_complete(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    chunk_size = 256 * 1024
    payload = (b"A" * chunk_size) + (b"B" * chunk_size) + b"IJ"
    started = client.post(
        "/api/cloud-drive/resumable-upload/start",
        json={
            "filename": "resume.txt",
            "mime_type": "text/plain",
            "total_bytes": len(payload),
            "chunk_size": chunk_size,
            "privacy_mode": "standard_plain",
        },
    )
    assert started.status_code == 200
    session = started.get_json()["session"]
    session_id = session["session_id"]
    assert session["total_chunks"] == 3

    chunk1 = client.post(
        f"/api/cloud-drive/resumable-upload/{session_id}/chunks/1",
        data={"chunk": (io.BytesIO(b"B" * chunk_size), "part1")},
        content_type="multipart/form-data",
    )
    assert chunk1.status_code == 200
    chunk1_body = chunk1.get_json()
    assert chunk1_body["chunk"]["bytes_received"] == chunk_size
    assert chunk1_body["chunk"]["storage_mode"] == "streamed_to_disk"
    status = client.get(f"/api/cloud-drive/resumable-upload/{session_id}/status")
    assert status.get_json()["session"]["received_chunks"] == [1]

    incomplete = client.post(f"/api/cloud-drive/resumable-upload/{session_id}/complete")
    assert incomplete.status_code == 409
    assert incomplete.get_json()["missing_chunks"] == [0, 2]

    chunk0 = client.post(
        f"/api/cloud-drive/resumable-upload/{session_id}/chunks/0",
        data={"chunk": (io.BytesIO(b"A" * chunk_size), "part0")},
        content_type="multipart/form-data",
    )
    assert chunk0.status_code == 200
    assert chunk0.get_json()["chunk"]["storage_mode"] == "streamed_to_disk"
    chunk2 = client.post(
        f"/api/cloud-drive/resumable-upload/{session_id}/chunks/2",
        data={"chunk": (io.BytesIO(b"IJ"), "part2")},
        content_type="multipart/form-data",
    )
    assert chunk2.status_code == 200
    assert chunk2.get_json()["chunk"]["bytes_received"] == 2
    assert chunk2.get_json()["chunk"]["storage_mode"] == "streamed_to_disk"

    completed = client.post(f"/api/cloud-drive/resumable-upload/{session_id}/complete")
    assert completed.status_code == 200
    body = completed.get_json()
    file_id = body["file"]["file_id"]
    assert body["session"]["status"] == "completed"
    assert client.get(f"/api/cloud-drive/files/{file_id}/download").data == payload

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        session_row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (session_id,)).fetchone()
        assert session_row["status"] == "completed"
        upload_job = conn.execute(
            "SELECT * FROM job_center_jobs WHERE source_module='cloud_drive_resumable_upload' AND source_ref=?",
            (f"upload_session:{session_id}",),
        ).fetchone()
        assert upload_job is not None
        assert upload_job["status"] == "succeeded"
        assert upload_job["progress_percent"] == 100
    finally:
        conn.close()


def test_cloud_drive_resumable_upload_rejects_raw_chunk_size_mismatch(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    started = client.post(
        "/api/cloud-drive/resumable-upload/start",
        json={
            "filename": "bad-chunk.bin",
            "mime_type": "application/octet-stream",
            "total_bytes": 8,
            "chunk_size": 4,
            "privacy_mode": "standard_plain",
        },
    )
    assert started.status_code == 200
    session_id = started.get_json()["session"]["session_id"]

    bad = client.post(
        f"/api/cloud-drive/resumable-upload/{session_id}/chunks/0",
        data=b"abc",
        content_type="application/octet-stream",
    )

    assert bad.status_code == 400
    body = bad.get_json()
    assert body["error"] == "invalid_chunk_size"
    assert body["ok"] is False


def test_cloud_drive_resumable_complete_reuses_existing_merged_file_after_interruption(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    chunk_size = 256 * 1024
    payload = (b"A" * chunk_size) + (b"B" * chunk_size) + b"tail"
    started = client.post(
        "/api/cloud-drive/resumable-upload/start",
        json={
            "filename": "resume-existing-complete.txt",
            "mime_type": "text/plain",
            "total_bytes": len(payload),
            "chunk_size": chunk_size,
            "privacy_mode": "standard_plain",
        },
    )
    assert started.status_code == 200
    session_id = started.get_json()["session"]["session_id"]
    chunks = (b"A" * chunk_size, b"B" * chunk_size, b"tail")
    for index, chunk in enumerate(chunks):
        assert client.post(
            f"/api/cloud-drive/resumable-upload/{session_id}/chunks/{index}",
            data={"chunk": (io.BytesIO(chunk), f"part{index}")},
            content_type="multipart/form-data",
        ).status_code == 200

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        session_row = conn.execute(
            "SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        temp_dir = Path(session_row["temp_dir"])
        (temp_dir / "complete.upload").write_bytes(payload)
        for part in temp_dir.glob("*.part"):
            part.unlink()
        conn.execute(
            "UPDATE cloud_resumable_upload_sessions SET status='completing' WHERE session_id=?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()

    completed = client.post(f"/api/cloud-drive/resumable-upload/{session_id}/complete")
    assert completed.status_code == 200
    body = completed.get_json()
    file_id = body["file"]["file_id"]
    assert body["session"]["status"] == "completed"
    assert client.get(f"/api/cloud-drive/files/{file_id}/download").data == payload
    assert not temp_dir.exists()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        session_row = conn.execute("SELECT * FROM cloud_resumable_upload_sessions WHERE session_id=?", (session_id,)).fetchone()
        assert session_row["status"] == "completed"
        upload_job = conn.execute(
            "SELECT * FROM job_center_jobs WHERE source_module='cloud_drive_resumable_upload' AND source_ref=?",
            (f"upload_session:{session_id}",),
        ).fetchone()
        assert upload_job is not None
        assert upload_job["status"] == "succeeded"
        assert upload_job["progress_percent"] == 100
    finally:
        conn.close()


def test_cloud_drive_resumable_upload_sessions_restore_active_uploads(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    chunk_size = 256 * 1024
    payload = (b"A" * chunk_size) + b"tail"
    started = client.post(
        "/api/cloud-drive/resumable-upload/start",
        json={
            "filename": "restore-active.txt",
            "mime_type": "text/plain",
            "total_bytes": len(payload),
            "chunk_size": chunk_size,
            "privacy_mode": "standard_plain",
        },
    )
    assert started.status_code == 200
    session_id = started.get_json()["session"]["session_id"]

    uploaded = client.post(
        f"/api/cloud-drive/resumable-upload/{session_id}/chunks/0",
        data={"chunk": (io.BytesIO(b"A" * chunk_size), "part0")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200

    listing = client.get("/api/cloud-drive/resumable-upload/sessions")
    assert listing.status_code == 200
    sessions = listing.get_json()["sessions"]
    restored = next((session for session in sessions if session["session_id"] == session_id), None)
    assert restored is not None
    assert restored["status"] == "uploading"
    assert restored["received_chunks"] == [0]
    assert restored["received_bytes"] == chunk_size

    completed_tail = client.post(
        f"/api/cloud-drive/resumable-upload/{session_id}/chunks/1",
        data={"chunk": (io.BytesIO(b"tail"), "part1")},
        content_type="multipart/form-data",
    )
    assert completed_tail.status_code == 200
    completed = client.post(f"/api/cloud-drive/resumable-upload/{session_id}/complete")
    assert completed.status_code == 200

    after_complete = client.get("/api/cloud-drive/resumable-upload/sessions")
    assert all(session["session_id"] != session_id for session in after_complete.get_json()["sessions"])


def test_storage_resumable_upload_creates_storage_file_entry(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    chunk_size = 256 * 1024
    payload = (b"a" * chunk_size) + b"def"
    started = client.post(
        "/api/cloud-drive/resumable-upload/start",
        json={
            "target": "storage",
            "filename": "folder-note.txt",
            "mime_type": "text/plain",
            "total_bytes": len(payload),
            "chunk_size": chunk_size,
            "virtual_path": "/docs/folder-note.txt",
            "display_name": "Folder Note",
        },
    )
    assert started.status_code == 200
    session_id = started.get_json()["session"]["session_id"]
    assert client.post(
        f"/api/cloud-drive/resumable-upload/{session_id}/chunks/0",
        data={"chunk": (io.BytesIO(b"a" * chunk_size), "part0")},
        content_type="multipart/form-data",
    ).status_code == 200
    assert client.post(
        f"/api/cloud-drive/resumable-upload/{session_id}/chunks/1",
        data={"chunk": (io.BytesIO(b"def"), "part1")},
        content_type="multipart/form-data",
    ).status_code == 200

    completed = client.post(f"/api/cloud-drive/resumable-upload/{session_id}/complete")
    assert completed.status_code == 200
    body = completed.get_json()
    storage_file = body["storage_file"]
    assert storage_file["virtual_path"] == "/docs/folder-note.txt"
    assert storage_file["display_name"] == "Folder Note"
    assert client.get(f"/api/storage/files/{storage_file['id']}/download").data == payload


def test_resumable_server_encrypted_upload_finishes_through_chunked_encryption_path(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box, server_file_fernet=fernet).test_client()

    chunk_size = 256 * 1024
    payload = (b"s" * chunk_size) + b"ecret note"
    started = client.post(
        "/api/cloud-drive/resumable-upload/start",
        json={
            "filename": "secret.txt",
            "mime_type": "text/plain",
            "total_bytes": len(payload),
            "chunk_size": chunk_size,
            "privacy_mode": "server_encrypted",
        },
    )
    assert started.status_code == 200
    session_id = started.get_json()["session"]["session_id"]
    for index, chunk in enumerate((b"s" * chunk_size, b"ecret note")):
        assert client.post(
            f"/api/cloud-drive/resumable-upload/{session_id}/chunks/{index}",
            data={"chunk": (io.BytesIO(chunk), f"part{index}")},
            content_type="multipart/form-data",
        ).status_code == 200

    completed = client.post(f"/api/cloud-drive/resumable-upload/{session_id}/complete")
    assert completed.status_code == 200
    file_id = completed.get_json()["file"]["file_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
        stored = storage_root / row["storage_path"]
        assert row["privacy_mode"] == "server_encrypted"
        assert row["encryption_version"] == "server-side-chunked-v1"
        assert stored.read_bytes() != payload
        assert decrypt_server_encrypted_bytes(stored, fernet) == payload
    finally:
        conn.close()


def test_storage_resumable_e2ee_upload_preserves_folder_entry_and_key(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    chunk_size = 256 * 1024
    payload = (b"cipher" * 1024) + b"-folder-e2ee"
    started = client.post(
        "/api/cloud-drive/resumable-upload/start",
        json={
            "target": "storage",
            "filename": "secret.txt",
            "mime_type": "application/octet-stream",
            "total_bytes": len(payload),
            "chunk_size": chunk_size,
            "privacy_mode": "e2ee",
            "virtual_path": "/vault/sub/secret.txt",
            "display_name": "vault/sub/secret.txt",
            "encrypted_metadata": '{"nonce":"meta","ciphertext":"sealed-metadata"}',
            "encrypted_file_key": '{"wrapped_by":"browser_passphrase_pbkdf2_v2","ciphertext":"sealed-key"}',
            "wrapped_by": "browser_passphrase_pbkdf2_v2",
            "ciphertext_sha256": "0" * 64,
            "encryption_algorithm": "AES-GCM",
            "encryption_version": "browser-passphrase-v2",
            "nonce": "nonce",
        },
    )
    assert started.status_code == 200
    session_id = started.get_json()["session"]["session_id"]

    for index, start in enumerate(range(0, len(payload), chunk_size)):
        chunk = payload[start : start + chunk_size]
        assert client.post(
            f"/api/cloud-drive/resumable-upload/{session_id}/chunks/{index}",
            data={"chunk": (io.BytesIO(chunk), f"part{index}")},
            content_type="multipart/form-data",
        ).status_code == 200

    completed = client.post(f"/api/cloud-drive/resumable-upload/{session_id}/complete")
    assert completed.status_code == 200
    body = completed.get_json()
    storage_file = body["storage_file"]
    file_id = body["file"]["file_id"]
    assert storage_file["virtual_path"] == "/vault/sub/secret.txt"
    assert storage_file["display_name"] == "vault/sub/secret.txt"
    assert client.get(f"/api/storage/files/{storage_file['id']}/download").data == payload

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
        key = conn.execute("SELECT * FROM encrypted_file_keys WHERE file_id=?", (file_id,)).fetchone()
        assert row["privacy_mode"] == "e2ee"
        assert row["original_filename_encrypted"] == '{"nonce":"meta","ciphertext":"sealed-metadata"}'
        assert row["mime_type_plain_for_public"] is None
        assert key["wrapped_by"] == "browser_passphrase_pbkdf2_v2"
        assert key["encrypted_file_key"] == '{"wrapped_by":"browser_passphrase_pbkdf2_v2","ciphertext":"sealed-key"}'
    finally:
        conn.close()


def test_storage_file_e2ee_upload_failure_returns_client_error(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    res = client.post(
        "/api/storage/files",
        data={"file": (io.BytesIO(b"cipher"), "vault.bin"), "privacy_mode": "e2ee"},
        content_type="multipart/form-data",
    )

    assert res.status_code == 400
    body = res.get_json()
    assert body["ok"] is False
    assert "encrypted_file_key is required" in body["msg"]
    assert body["error_code"] == "ValueError"


def test_capacity_error_takes_priority_when_upload_exceeds_quota_and_single_file_limit(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()
    payload = b"x" * (1024 * 1024 + 1)

    for endpoint in ("/api/cloud-drive/upload", "/api/storage/files"):
        res = client.post(
            endpoint,
            data={"file": (io.BytesIO(payload), "too-large.bin")},
            content_type="multipart/form-data",
        )
        body = res.get_json()

        assert res.status_code == 400
        assert body["ok"] is False
        assert "超過雲端硬碟容量上限" in body["msg"]
        assert "單檔" not in body["msg"]


def test_server_encrypted_upload_stores_ciphertext_but_downloads_plaintext(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    res = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"secret note"), "note.txt"), "privacy_mode": "server_encrypted"},
        content_type="multipart/form-data",
    )

    assert res.status_code == 200
    file_id = res.get_json()["file"]["file_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
    conn.close()
    assert row["privacy_mode"] == "server_encrypted"
    assert row["encryption_version"] == "server-side-chunked-v1"
    stored = storage_root / row["storage_path"]
    assert stored.read_bytes() != b"secret note"

    preview = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert preview.status_code == 200
    assert preview.get_json()["preview"]["text"] == "secret note"

    download = client.get(f"/api/cloud-drive/files/{file_id}/download")
    assert download.status_code == 200
    assert download.data == b"secret note"


def test_server_encrypted_upload_always_uses_chunked_encryption(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    monkeypatch.setenv("HACKME_SERVER_ENCRYPTED_CHUNK_BYTES", "4")
    fernet = Fernet(Fernet.generate_key())
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box, server_file_fernet=fernet).test_client()
    payload = b"too-large"

    res = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(payload), "large.bin"), "privacy_mode": "server_encrypted"},
        content_type="multipart/form-data",
    )

    assert res.status_code == 200
    body = res.get_json()
    file_id = body["file"]["file_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
        stored = storage_root / row["storage_path"]
        assert row["privacy_mode"] == "server_encrypted"
        assert row["encryption_version"] == "server-side-chunked-v1"
        assert stored.read_bytes() != payload
        assert decrypt_server_encrypted_bytes(stored, fernet) == payload
    finally:
        conn.close()

    download = client.get(f"/api/cloud-drive/files/{file_id}/download")
    assert download.status_code == 200
    assert download.data == payload
    ranged = client.get(f"/api/cloud-drive/files/{file_id}/download", headers={"Range": "bytes=4-8"})
    assert ranged.status_code == 206
    assert ranged.headers["Content-Range"] == "bytes 4-8/9"
    assert ranged.data == b"large"


def test_server_encrypted_upload_scans_plaintext_temp_not_final_storage_path(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    from services.storage import cloud_drive

    observed = {}

    def fake_scan_uploaded_file(conn, *, file_id, file_path, filename=None, declared_mime=None):
        row = conn.execute("SELECT storage_path FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
        final_path = storage_root / row["storage_path"]
        observed["scan_path"] = str(file_path)
        observed["final_path"] = str(final_path)
        assert os.path.exists(file_path)
        with open(file_path, "rb") as handle:
            assert handle.read() == b"secret note"
        assert final_path.exists() is False
        return {"scan_status": "clean", "risk_level": "low", "results": []}

    monkeypatch.setattr(cloud_drive, "scan_uploaded_file", fake_scan_uploaded_file)

    res = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"secret note"), "note.txt"), "privacy_mode": "server_encrypted"},
        content_type="multipart/form-data",
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body["file"]["privacy_mode"] == "server_encrypted"
    assert observed["scan_path"] != observed["final_path"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (body["file"]["file_id"],)).fetchone()
    conn.close()
    stored = storage_root / row["storage_path"]
    assert row["encryption_version"] == "server-side-chunked-v1"
    assert stored.read_bytes() != b"secret note"


def test_runtime_engineer_can_decrypt_server_encrypted_but_not_e2ee(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    fernet = Fernet(Fernet.generate_key())
    client = _build_app(db_path, storage_root, actor_box, server_file_fernet=fernet).test_client()

    server_res = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"server secret"), "note.txt"), "privacy_mode": "server_encrypted"},
        content_type="multipart/form-data",
    )
    assert server_res.status_code == 200
    server_file_id = server_res.get_json()["file"]["file_id"]

    e2ee_ciphertext = b"browser-side-ciphertext"
    e2ee_res = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(e2ee_ciphertext), "vault.bin"),
            "privacy_mode": "e2ee",
            "encrypted_metadata": "sealed:metadata",
            "encrypted_file_key": "sealed:owner-key",
            "wrapped_by": "browser_passphrase_pbkdf2_v2",
            "ciphertext_sha256": "a" * 64,
            "encryption_algorithm": "AES-GCM",
            "encryption_version": "browser-passphrase-v2",
            "nonce": "nonce",
        },
        content_type="multipart/form-data",
    )
    assert e2ee_res.status_code == 200
    e2ee_file_id = e2ee_res.get_json()["file"]["file_id"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (server_file_id,)).fetchone()
    e2ee_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (e2ee_file_id,)).fetchone()
    key_row = conn.execute(
        "SELECT encrypted_file_key, wrapped_by FROM encrypted_file_keys WHERE file_id=?",
        (e2ee_file_id,),
    ).fetchone()
    conn.close()

    server_path = storage_root / server_row["storage_path"]
    e2ee_path = storage_root / e2ee_row["storage_path"]

    assert decrypt_server_encrypted_bytes(server_path, fernet) == b"server secret"
    assert server_path.read_bytes() != b"server secret"

    assert e2ee_path.read_bytes() == e2ee_ciphertext
    assert key_row["encrypted_file_key"] == "sealed:owner-key"
    assert key_row["wrapped_by"] == "browser_passphrase_pbkdf2_v2"
    with pytest.raises(ValueError):
        decrypt_server_encrypted_bytes(e2ee_path, fernet)


def test_server_encrypted_media_preview_content_supports_range_requests(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    media_bytes = b"not-a-real-mp4-but-route-test"
    res = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(media_bytes), "clip.mp4", "video/mp4"), "privacy_mode": "server_encrypted"},
        content_type="multipart/form-data",
    )

    assert res.status_code == 200
    file_id = res.get_json()["file"]["file_id"]

    content = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    assert content.status_code == 200
    assert content.headers["Accept-Ranges"] == "bytes"
    assert content.headers["X-Hackme-Transfer-Mode"] == "python_chunked_decrypt"
    assert content.data == media_bytes

    ranged = client.get(f"/api/cloud-drive/files/{file_id}/preview/content", headers={"Range": "bytes=4-11"})
    assert ranged.status_code == 206
    assert ranged.headers["Content-Range"] == "bytes 4-11/29"
    assert ranged.headers["Accept-Ranges"] == "bytes"
    assert ranged.headers["X-Hackme-Transfer-Mode"] == "python_chunked_decrypt"
    assert ranged.data == b"a-real-m"


def test_server_encrypted_media_preview_content_encodes_unicode_filename_safely(tmp_path):
    db_path = tmp_path / "unicode-drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    media_bytes = b"unicode-mp3-preview"
    res = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(media_bytes), "測試音樂.mp3", "audio/mpeg"), "privacy_mode": "server_encrypted"},
        content_type="multipart/form-data",
    )

    assert res.status_code == 200
    file_id = res.get_json()["file"]["file_id"]

    content = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    assert content.status_code == 200
    assert content.data == media_bytes
    disposition = content.headers["Content-Disposition"]
    assert 'filename="' in disposition
    assert "filename*=UTF-8''" in disposition
    assert "%E6%B8%AC%E8%A9%A6%E9%9F%B3%E6%A8%82.mp3" in disposition

    ranged = client.get(f"/api/cloud-drive/files/{file_id}/preview/content", headers={"Range": "bytes=0-6"})
    assert ranged.status_code == 206
    assert ranged.data == b"unicode"
    assert "filename*=UTF-8''" in ranged.headers["Content-Disposition"]


def test_server_encrypted_pdf_preview_content_encodes_unicode_filename_safely(tmp_path):
    db_path = tmp_path / "unicode-pdf-drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    pdf_bytes = b"%PDF-1.4\n% unicode pdf preview\n"
    res = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(pdf_bytes), "日本語資料.pdf", "application/pdf"), "privacy_mode": "server_encrypted"},
        content_type="multipart/form-data",
    )

    assert res.status_code == 200
    file_id = res.get_json()["file"]["file_id"]

    metadata = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert metadata.status_code == 200
    assert metadata.get_json()["preview"]["category"] == "pdf"

    content = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    assert content.status_code == 200
    assert content.data == pdf_bytes
    assert content.mimetype == "application/pdf"
    disposition = content.headers["Content-Disposition"]
    assert 'filename="' in disposition
    assert "filename*=UTF-8''" in disposition
    assert "%E6%97%A5%E6%9C%AC%E8%AA%9E%E8%B3%87%E6%96%99.pdf" in disposition


def test_streaming_preview_survives_file_unlink_after_first_chunk(tmp_path):
    db_path = tmp_path / "stream-unlink.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    settings = {
        "storage_trash_retention_days": 30,
        "cloud_drive_transfer_limits_enabled": True,
        "cloud_drive_transfer_limits_json": {
            "trusted": {"download_kb_per_sec": 1, "upload_kb_per_sec": 1024, "priority": 50},
            "normal": {"download_kb_per_sec": 1, "upload_kb_per_sec": 1024, "priority": 50},
        },
    }
    client = _build_app(db_path, storage_root, actor_box, settings=settings).test_client()

    payload = b"%PDF-1.4\n" + b"A" * 12000 + b"\n%%EOF\n" + b"B" * 12000
    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(payload), "large.pdf", "application/pdf"), "privacy_mode": "standard_plain"},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
    finally:
        conn.close()
    target = storage_root / row["storage_path"]

    response = client.get(f"/api/cloud-drive/files/{file_id}/preview/content", buffered=False)
    chunks = response.response
    first = next(chunks)
    target.unlink()
    rest = b"".join(chunks)

    assert response.status_code == 200
    assert first + rest == payload


def test_cloud_drive_upload_repairs_legacy_uploaded_files_schema(tmp_path):
    db_path = tmp_path / "legacy-drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL
        );
        INSERT INTO users (id, username, role) VALUES (1, 'alice', 'user');
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY
        );
        """
    )
    ensure_member_level_rules_schema(conn)
    ensure_upload_security_schema(conn)
    update_cloud_drive_security_policy(conn, {"scanner_enabled": False})
    ensure_cloud_drive_attachment_schema(conn)
    conn.commit()
    conn.close()

    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()
    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"legacy schema upload"), "legacy.txt")},
        content_type="multipart/form-data",
    )

    assert uploaded.status_code == 200
    body = uploaded.get_json()
    assert body["ok"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(uploaded_files)").fetchall()}
        saved = conn.execute("SELECT storage_path, size_bytes, deleted_at FROM uploaded_files").fetchone()
    finally:
        conn.close()
    assert {"owner_user_id", "storage_path", "privacy_mode", "scan_status", "size_bytes", "deleted_at"} <= cols
    assert saved["storage_path"].endswith("/legacy.txt")
    assert saved["size_bytes"] == len(b"legacy schema upload")
    assert saved["deleted_at"] is None


def test_storage_upload_creates_logical_file_and_downloads_through_original_record(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"storage data"), "note.txt"),
            "virtual_path": "docs/note.txt",
            "display_name": "note.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file = uploaded.get_json()["storage_file"]
    assert storage_file["virtual_path"] == "/docs/note.txt"

    listing = client.get("/api/storage/files")
    assert listing.status_code == 200
    body = listing.get_json()
    assert body["files"][0]["id"] == storage_file["id"]
    assert body["storage"]["used_bytes"] == len(b"storage data")

    download = client.get(f"/api/storage/files/{storage_file['id']}/download")
    assert download.status_code == 200
    assert download.data == b"storage data"


def test_storage_upload_rejects_path_traversal(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"bad path"), "bad.txt"),
            "virtual_path": "../bad.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 400
    assert "path" in uploaded.get_json()["msg"]


def test_storage_trash_restore_and_purge_updates_listing_and_quota(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"trash me"), "trash.txt"),
            "virtual_path": "trash.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file_id = uploaded.get_json()["storage_file"]["id"]

    trashed = client.delete(f"/api/storage/files/{storage_file_id}")
    assert trashed.status_code == 200
    assert trashed.get_json()["storage_file"]["is_trashed"] == 1

    assert client.get(f"/api/storage/files/{storage_file_id}/download").status_code == 404
    listing = client.get("/api/storage/files").get_json()
    assert listing["files"] == []
    trash = client.get("/api/storage/trash").get_json()
    assert trash["files"][0]["id"] == storage_file_id
    assert trash["storage"]["used_bytes"] == len(b"trash me")

    restored_all = client.post("/api/storage/trash/restore")
    assert restored_all.status_code == 200
    assert restored_all.get_json()["trash"]["restored"] == 1
    assert client.get(f"/api/storage/files/{storage_file_id}/download").status_code == 200

    assert client.delete(f"/api/storage/files/{storage_file_id}").status_code == 200
    restored = client.post(f"/api/storage/files/{storage_file_id}/restore")
    assert restored.status_code == 200
    assert restored.get_json()["storage_file"]["is_trashed"] == 0
    assert client.get(f"/api/storage/files/{storage_file_id}/download").status_code == 200

    assert client.delete(f"/api/storage/files/{storage_file_id}").status_code == 200
    purged_all = client.delete("/api/storage/trash/purge")
    assert purged_all.status_code == 200
    assert purged_all.get_json()["trash"]["purged"] == 1
    assert client.get(f"/api/storage/files/{storage_file_id}/download").status_code == 404

    uploaded_again = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"purge me"), "purge.txt"),
            "virtual_path": "purge.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded_again.status_code == 200
    storage_file_id = uploaded_again.get_json()["storage_file"]["id"]
    purged = client.delete(f"/api/storage/files/{storage_file_id}/purge")
    assert purged.status_code == 200
    assert purged.get_json()["purged"]["storage"]["used_bytes"] == 0
    assert client.get(f"/api/storage/files/{storage_file_id}/download").status_code == 404


def test_storage_folders_and_file_organize_flow(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    created_folder = client.post("/api/storage/folders", json={"path": "/photos/raw"})
    assert created_folder.status_code == 200
    assert created_folder.get_json()["folder"]["virtual_path"] == "/photos/raw"

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"folder move"), "image.txt"),
            "virtual_path": "/photos/raw/image.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file_id = uploaded.get_json()["storage_file"]["id"]

    folders = client.get("/api/storage/folders")
    assert folders.status_code == 200
    folder_paths = {folder["virtual_path"] for folder in folders.get_json()["folders"]}
    assert {"/photos", "/photos/raw"} <= folder_paths

    moved_file = client.put(f"/api/storage/files/{storage_file_id}/organize", json={"virtual_path": "/photos/final/image.txt"})
    assert moved_file.status_code == 200
    assert moved_file.get_json()["storage_file"]["virtual_path"] == "/photos/final/image.txt"

    moved_folder = client.put("/api/storage/folders/move", json={"old_path": "/photos/final", "new_path": "/archive/final"})
    assert moved_folder.status_code == 200
    assert moved_folder.get_json()["folder_move"]["moved_files"] == 1

    listing = client.get("/api/storage/files").get_json()["files"]
    assert listing[0]["virtual_path"] == "/archive/final/image.txt"

    deleted_folder = client.post("/api/storage/folders/trash", json={"path": "/archive"})
    assert deleted_folder.status_code == 200
    assert deleted_folder.get_json()["folder_trash"]["trashed_files"] == 1
    assert client.get("/api/storage/files").get_json()["files"] == []
    trash = client.get("/api/storage/trash").get_json()["files"]
    assert trash[0]["virtual_path"] == "/archive/final/image.txt"


def test_storage_file_and_folder_can_be_renamed_with_unicode_paths(tmp_path):
    db_path = tmp_path / "rename-drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    created_folder = client.post("/api/storage/folders", json={"path": "/音樂"})
    assert created_folder.status_code == 200

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"rename me"), "sample.mp3", "audio/mpeg"),
            "virtual_path": "/音樂/sample.mp3",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file_id = uploaded.get_json()["storage_file"]["id"]

    renamed_file = client.put(
        f"/api/storage/files/{storage_file_id}/organize",
        json={"virtual_path": "/音樂/テスト音楽.mp3"},
    )
    assert renamed_file.status_code == 200
    renamed_file_body = renamed_file.get_json()["storage_file"]
    assert renamed_file_body["display_name"] == "テスト音楽.mp3"
    assert renamed_file_body["virtual_path"] == "/音樂/テスト音楽.mp3"

    renamed_folder = client.put("/api/storage/folders/move", json={"old_path": "/音樂", "new_path": "/音楽"})
    assert renamed_folder.status_code == 200
    assert renamed_folder.get_json()["folder_move"]["moved_files"] == 1

    listing = client.get("/api/storage/files").get_json()["files"]
    assert listing[0]["display_name"] == "テスト音楽.mp3"
    assert listing[0]["virtual_path"] == "/音楽/テスト音楽.mp3"

    folders = client.get("/api/storage/folders").get_json()["folders"]
    folder_paths = {folder["virtual_path"] for folder in folders}
    assert "/音楽" in folder_paths


def test_storage_folder_trash_endpoint_handles_empty_explicit_folder(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    created_folder = client.post("/api/storage/folders", json={"path": "/empty"})
    assert created_folder.status_code == 200

    deleted_folder = client.post("/api/storage/folders/trash", json={"path": "/empty"})

    assert deleted_folder.status_code == 200
    body = deleted_folder.get_json()
    assert body["folder_trash"]["path"] == "/empty"
    assert body["folder_trash"]["trashed_files"] == 0
    assert body["folder_trash"]["deleted_folders"] == 1


def test_storage_folder_can_be_converted_to_album_when_all_files_are_media(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    for filename, payload, virtual_path in (
        ("cover.png", b"\x89PNG\r\n\x1a\n", "/gallery/cover.png"),
        ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "/gallery/nested/clip.mp4"),
    ):
        uploaded = client.post(
            "/api/storage/files",
            data={"file": (io.BytesIO(payload), filename), "virtual_path": virtual_path},
            content_type="multipart/form-data",
        )
        assert uploaded.status_code == 200

    created = client.post("/api/storage/folders/album", json={"path": "/gallery", "title": "Gallery"})

    assert created.status_code == 200
    album = created.get_json()["album"]
    assert album["title"] == "Gallery"
    assert album["source_folder"] == "/gallery"
    assert album["added_count"] == 2
    assert [item["display_name"] for item in album["files"]] == ["cover.png", "clip.mp4"]


def test_storage_folder_album_rejects_non_media_files(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    for filename, virtual_path in (
        ("cover.png", "/mixed/cover.png"),
        ("note.txt", "/mixed/note.txt"),
    ):
        uploaded = client.post(
            "/api/storage/files",
            data={"file": (io.BytesIO(b"file body"), filename), "virtual_path": virtual_path},
            content_type="multipart/form-data",
        )
        assert uploaded.status_code == 200

    created = client.post("/api/storage/folders/album", json={"path": "/mixed", "title": "Mixed"})

    assert created.status_code == 400
    assert "非圖片/影片" in created.get_json()["msg"]
    assert all(album["title"] != "Mixed" for album in client.get("/api/storage/albums").get_json()["albums"])


def test_storage_album_crud_and_file_membership(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"album image"), "image.txt"),
            "virtual_path": "photos/image.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file_id = uploaded.get_json()["storage_file"]["id"]

    created = client.post("/api/storage/albums", json={"title": "Trip", "visibility": "unlisted"})
    assert created.status_code == 200
    album = created.get_json()["album"]
    assert album["title"] == "Trip"
    assert album["visibility"] == "unlisted"
    assert album["share_url"].startswith("/shared/albums/")
    assert album["share_link"]["url"] == album["share_url"]

    added = client.post(
        f"/api/storage/albums/{album['id']}/files",
        json={"storage_file_id": storage_file_id, "caption": "cover", "sort_order": 2},
    )
    assert added.status_code == 200
    files = added.get_json()["album"]["files"]
    assert len(files) == 1
    assert files[0]["caption"] == "cover"
    assert files[0]["original_filename_plain_for_public"] == "image.txt"
    assert "mime_type_plain_for_public" in files[0]

    public_album = client.get(f"/api/storage/shared/albums/{album['share_url'].rsplit('/', 1)[-1]}")
    assert public_album.status_code == 200
    assert public_album.get_json()["album"]["files"][0]["display_name"] == "image.txt"

    public_file = client.get(public_album.get_json()["album"]["files"][0]["download_url"])
    assert public_file.status_code == 200
    assert public_file.data == b"album image"

    protected = client.post(
        "/api/storage/albums",
        json={"title": "Protected Trip", "visibility": "unlisted", "share_password": "AlbumPass123"},
    )
    assert protected.status_code == 200
    protected_album = protected.get_json()["album"]
    assert protected_album["share_link"]["password_required"] is True
    protected_token = protected_album["share_url"].rsplit("/", 1)[-1]
    protected_added = client.post(
        f"/api/storage/albums/{protected_album['id']}/files",
        json={"storage_file_id": storage_file_id},
    )
    assert protected_added.status_code == 200
    assert client.get(f"/api/storage/shared/albums/{protected_token}").status_code == 401
    assert client.get(
        f"/api/storage/shared/albums/{protected_token}",
        headers={"X-Album-Share-Password": "wrong"},
    ).status_code == 403
    protected_public = client.get(
        f"/api/storage/shared/albums/{protected_token}",
        headers={"X-Album-Share-Password": "AlbumPass123"},
    )
    assert protected_public.status_code == 200
    protected_download = protected_public.get_json()["album"]["files"][0]["download_url"]
    assert client.get(protected_download).status_code == 401
    assert client.get(protected_download, query_string={"password": "AlbumPass123"}).status_code == 200

    limited = client.post("/api/storage/albums", json={"title": "Limited Trip", "visibility": "unlisted"})
    assert limited.status_code == 200
    limited_album = limited.get_json()["album"]
    limited_token = limited_album["share_url"].rsplit("/", 1)[-1]
    expired = client.post("/api/storage/albums", json={"title": "Expired Trip", "visibility": "unlisted"})
    assert expired.status_code == 200
    expired_album = expired.get_json()["album"]
    expired_token = expired_album["share_url"].rsplit("/", 1)[-1]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE album_share_links SET max_views=1, access_count=0 WHERE id=?",
            (limited_album["share_link"]["id"],),
        )
        conn.execute(
            "UPDATE album_share_links SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00", expired_album["share_link"]["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    assert client.get(f"/api/storage/shared/albums/{limited_token}").status_code == 200
    limited_exhausted = client.get(f"/api/storage/shared/albums/{limited_token}")
    assert limited_exhausted.status_code == 410
    assert limited_exhausted.get_json()["reason"] == "view_limit_reached"
    expired_public = client.get(f"/api/storage/shared/albums/{expired_token}")
    assert expired_public.status_code == 410
    assert expired_public.get_json()["reason"] == "expired"

    updated = client.put(f"/api/storage/albums/{album['id']}", json={"title": "Trip 2", "visibility": "public"})
    assert updated.status_code == 200
    assert updated.get_json()["album"]["title"] == "Trip 2"
    assert "share_url" not in updated.get_json()["album"]
    assert client.get(f"/api/storage/shared/albums/{album['share_url'].rsplit('/', 1)[-1]}").status_code == 404

    listed = client.get("/api/storage/albums")
    assert listed.status_code == 200
    listed_album = next(row for row in listed.get_json()["albums"] if row["id"] == album["id"])
    assert listed_album["file_count"] == 1

    album_file_id = files[0]["id"]
    removed = client.delete(f"/api/storage/albums/{album['id']}/files/{album_file_id}")
    assert removed.status_code == 200
    assert removed.get_json()["album"]["files"] == []

    deleted = client.delete(f"/api/storage/albums/{album['id']}")
    assert deleted.status_code == 200
    assert client.get(f"/api/storage/albums/{album['id']}").status_code == 404


def test_storage_album_batch_share_is_atomic_and_complete(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    storage_file_ids = []
    for name in ("batch-a.txt", "batch-b.txt"):
        uploaded = client.post(
            "/api/storage/files",
            data={"file": (io.BytesIO(name.encode("utf-8")), name), "virtual_path": f"batch/{name}"},
            content_type="multipart/form-data",
        )
        assert uploaded.status_code == 200
        storage_file_ids.append(uploaded.get_json()["storage_file"]["id"])

    rolled_back = client.post(
        "/api/storage/albums/batch-share",
        json={
            "title": "Must Roll Back",
            "storage_file_ids": [storage_file_ids[0], "missing-storage-file"],
        },
    )
    assert rolled_back.status_code == 400
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM albums WHERE title='Must Roll Back'").fetchone()[0] == 0
    finally:
        conn.close()

    created = client.post(
        "/api/storage/albums/batch-share",
        json={"title": "Atomic Batch", "storage_file_ids": storage_file_ids},
    )
    assert created.status_code == 200
    album = created.get_json()["album"]
    assert album["visibility"] == "unlisted"
    assert album["share_link"]["url"] == album["share_url"]
    assert len(album["files"]) == 2
    assert {item["storage_file_id"] for item in album["files"]} == set(storage_file_ids)


def test_storage_album_smart_organize_groups_media_by_folder_without_duplicates(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    for filename, content, virtual_path in [
        ("cover.jpg", b"jpg bytes", "/photos/trip/cover.jpg"),
        ("clip.mp4", b"mp4 bytes", "/photos/trip/clip.mp4"),
        ("notes.txt", b"not media", "/photos/trip/notes.txt"),
        ("root.png", b"png bytes", "/root.png"),
    ]:
        uploaded = client.post(
            "/api/storage/files",
            data={"file": (io.BytesIO(content), filename), "virtual_path": virtual_path},
            content_type="multipart/form-data",
        )
        assert uploaded.status_code == 200

    organized = client.post("/api/storage/albums/smart-organize", json={"strategy": "folder"})
    assert organized.status_code == 200
    result = organized.get_json()["result"]
    assert result["media_count"] == 3
    assert result["album_count"] == 2
    assert result["created_count"] == 2
    assert result["added_count"] == 3
    titles = {album["title"] for album in result["albums"]}
    assert {"智慧整理 - /photos/trip", "智慧整理 - 根目錄"} <= titles

    rerun = client.post("/api/storage/albums/smart-organize", json={"strategy": "folder"})
    assert rerun.status_code == 200
    rerun_result = rerun.get_json()["result"]
    assert rerun_result["created_count"] == 0
    assert rerun_result["updated_count"] == 2
    assert rerun_result["added_count"] == 0

    albums = client.get("/api/storage/albums").get_json()["albums"]
    trip_album = next(album for album in albums if album["title"] == "智慧整理 - /photos/trip")
    detail = client.get(f"/api/storage/albums/{trip_album['id']}")
    assert detail.status_code == 200
    files = detail.get_json()["album"]["files"]
    assert [file["display_name"] for file in files] == ["clip.mp4", "cover.jpg"]


def test_album_preview_uses_storage_display_name_when_uploaded_metadata_missing(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"fake png bytes"), "opaque.bin"),
            "virtual_path": "photos/real-photo.png",
            "display_name": "real-photo.png",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE uploaded_files SET original_filename_plain_for_public=NULL, mime_type_plain_for_public=NULL WHERE id=?",
            (file_id,),
        )
        conn.commit()
    finally:
        conn.close()

    created = client.post("/api/storage/albums", json={"title": "Recovered Preview"})
    assert created.status_code == 200
    album_id = created.get_json()["album"]["id"]

    added = client.post(f"/api/storage/albums/{album_id}/files", json={"file_id": file_id})
    assert added.status_code == 200
    album_file = added.get_json()["album"]["files"][0]
    assert album_file["display_name"] == "real-photo.png"
    assert album_file["original_filename_plain_for_public"] is None
    assert album_file["storage_path"]

    preview = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert preview.status_code == 200
    preview_body = preview.get_json()["preview"]
    assert preview_body["filename"] == "real-photo.png"
    assert preview_body["category"] == "image"
    assert preview_body["render_mode"] == "media"
    assert preview_body["mime_type"] == "image/png"

    content = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    assert content.status_code == 200
    assert content.content_type.startswith("image/png")


def test_storage_album_rejects_other_users_file(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"private"), "private.txt"),
            "virtual_path": "private.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file_id = uploaded.get_json()["storage_file"]["id"]

    actor_box["actor"] = _actor(2, "bob")
    created = client.post("/api/storage/albums", json={"title": "Bob"})
    album_id = created.get_json()["album"]["id"]
    denied = client.post(f"/api/storage/albums/{album_id}/files", json={"storage_file_id": storage_file_id})
    assert denied.status_code == 400


def test_storage_share_link_download_and_revoke(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"shared data"), "shared.txt"),
            "virtual_path": "shared.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file_id = uploaded.get_json()["storage_file"]["id"]

    created = client.post("/api/storage/share-links", json={"storage_file_id": storage_file_id})
    assert created.status_code == 200
    share_link = created.get_json()["share_link"]
    assert share_link["token"]
    assert share_link["share_url"] == f"/shared/files/{share_link['token']}"
    assert share_link["download_url"] == f"/api/storage/shared/{share_link['token']}/download"
    assert share_link["preview_url"] == f"/api/storage/shared/{share_link['token']}/preview"
    assert share_link["preview_content_url"] == f"/api/storage/shared/{share_link['token']}/preview/content"
    assert share_link["can_preview"] == 1

    listed = client.get("/api/storage/share-links").get_json()["share_links"]
    assert listed[0]["id"] == share_link["id"]
    assert "token" not in listed[0]
    assert listed[0]["share_url"] == share_link["share_url"]
    assert listed[0]["preview_url"] == share_link["preview_url"]

    page = client.get(share_link["share_url"])
    assert page.status_code == 200
    assert b"/js/shared-file.js" in page.data
    assert b"shared-file-preview-btn" in page.data

    meta = client.get(f"/api/storage/shared/{share_link['token']}")
    assert meta.status_code == 200
    file_meta = meta.get_json()["file"]
    assert file_meta["display_name"] == "shared.txt"
    assert file_meta["download_url"] == share_link["download_url"]
    assert file_meta["preview_url"] == share_link["preview_url"]
    assert file_meta["preview_content_url"] == share_link["preview_content_url"]
    assert file_meta["can_preview"] is True

    preview = client.get(f"/api/storage/shared/{share_link['token']}/preview")
    assert preview.status_code == 200
    preview_json = preview.get_json()["preview"]
    assert preview_json["render_mode"] == "text"
    assert "shared data" in preview_json["text"]

    actor_box["actor"] = _actor(3, "mallory")
    downloaded = client.get(f"/api/storage/shared/{share_link['token']}/download")
    assert downloaded.status_code == 200
    assert downloaded.data == b"shared data"

    actor_box["actor"] = _actor(1, "alice")
    revoked = client.post(f"/api/storage/share-links/{share_link['id']}/revoke")
    assert revoked.status_code == 200
    assert revoked.get_json()["share_link"]["revoked_at"]
    denied = client.get(f"/api/storage/shared/{share_link['token']}/download")
    assert denied.status_code == 404


def test_server_encrypted_mkv_share_preview_does_not_decrypt_entire_file(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"not-a-real-mkv-but-route-test"), "clip.mkv", "application/octet-stream"),
            "virtual_path": "clip.mkv",
            "privacy_mode": "server_encrypted",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file = uploaded.get_json()["storage_file"]
    storage_file_id = storage_file["id"]
    uploaded_file_id = storage_file["file_id"]
    created = client.post("/api/storage/share-links", json={"storage_file_id": storage_file_id})
    assert created.status_code == 200
    token = created.get_json()["share_link"]["token"]
    derivative_root = storage_root / "media_derivatives" / uploaded_file_id
    (derivative_root / "original").mkdir(parents=True)
    (derivative_root / "master.m3u8").write_text(
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1000000\n"
        "original/playlist.m3u8\n",
        encoding="utf-8",
    )
    (derivative_root / "original" / "playlist.m3u8").write_text(
        "#EXTM3U\n"
        "#EXT-X-MAP:URI=\"init.mp4\"\n"
        "#EXTINF:1.0,\n"
        "seg_00001.m4s\n"
        "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    (derivative_root / "subtitles").mkdir(parents=True)
    (derivative_root / "subtitles" / "sub01_zh.vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n字幕\n",
        encoding="utf-8",
    )
    (derivative_root / "original" / "init.mp4").write_bytes(b"init")
    (derivative_root / "original" / "seg_00001.m4s").write_bytes(b"seg")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_media_stream_schema(conn)
        cur = conn.execute(
            """
            INSERT INTO media_stream_assets (
                uploaded_file_id, source_mode, media_type, status, storage_mode,
                master_manifest_path, duration_seconds, source_mime_type,
                source_size_bytes, error_message, created_at, updated_at
            ) VALUES (?, 'server_encrypted', 'video', 'ready', 'acl_protected_plain',
                ?, 1.0, 'video/x-matroska', 28, '',
                '2026-01-01T00:00:00', '2026-01-01T00:00:00')
            """,
            (uploaded_file_id, f"media_derivatives/{uploaded_file_id}/master.m3u8"),
        )
        asset_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO media_stream_variants (
                asset_id, name, width, height, bitrate, codec, playlist_path,
                init_segment_path, created_at
            ) VALUES (?, 'original', 1920, 1080, 1000000, 'hvc1',
                ?, ?, '2026-01-01T00:00:00')
            """,
                (
                    asset_id,
                    f"media_derivatives/{uploaded_file_id}/original/playlist.m3u8",
                    f"media_derivatives/{uploaded_file_id}/original/init.mp4",
                ),
            )
        variant_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO media_stream_segments (
                variant_id, sequence_number, filename, path, duration_seconds,
                byte_size, created_at
            ) VALUES (?, 1, 'seg_00001.m4s', ?, 1.0, 3, '2026-01-01T00:00:00')
            """,
            (variant_id, f"media_derivatives/{uploaded_file_id}/original/seg_00001.m4s"),
        )
        conn.execute(
            """
            INSERT INTO media_stream_subtitles (
                asset_id, name, label, language, codec, path, is_default, created_at
            ) VALUES (?, 'sub01_zh', '繁中', 'zh', 'subrip', ?, 1, '2026-01-01T00:00:00')
            """,
            (asset_id, f"media_derivatives/{uploaded_file_id}/subtitles/sub01_zh.vtt"),
        )
        conn.commit()
    finally:
        conn.close()

    def fail_full_decrypt(*_args, **_kwargs):
        raise AssertionError("share preview metadata should not decrypt full server-encrypted media")

    monkeypatch.setattr(files_routes, "write_decrypted_server_encrypted_file", fail_full_decrypt)

    preview = client.get(f"/api/storage/shared/{token}/preview")
    assert preview.status_code == 200
    preview_json = preview.get_json()["preview"]
    assert preview_json["category"] == "video"
    assert preview_json["render_mode"] == "media"

    file_api = client.get(f"/api/storage/shared/{token}")
    file_json = file_api.get_json()["file"]
    assert file_json["stream_asset"]["status"] == "ready"
    assert file_json["stream_asset"]["master_url"] == f"/api/storage/shared/{token}/hls/master.m3u8"
    assert file_json["stream_asset"]["subtitles"][0]["url"] == f"/api/storage/shared/{token}/hls/subtitles/sub01_zh.vtt"

    own_preview = client.get(f"/api/cloud-drive/files/{uploaded_file_id}/preview")
    assert own_preview.status_code == 200
    own_stream = own_preview.get_json()["preview"]["stream_asset"]
    assert own_stream["status"] == "ready"
    assert own_stream["master_url"] == f"/api/cloud-drive/files/{uploaded_file_id}/hls/master.m3u8"
    assert own_stream["subtitles"][0]["url"] == f"/api/cloud-drive/files/{uploaded_file_id}/hls/subtitles/sub01_zh.vtt"

    own_master = client.get(f"/api/cloud-drive/files/{uploaded_file_id}/hls/master.m3u8")
    assert own_master.status_code == 200
    assert own_master.mimetype == "application/vnd.apple.mpegurl"

    own_subtitle = client.get(f"/api/cloud-drive/files/{uploaded_file_id}/hls/subtitles/sub01_zh.vtt")
    assert own_subtitle.status_code == 200
    assert own_subtitle.mimetype == "text/vtt"
    assert "WEBVTT" in own_subtitle.get_data(as_text=True)
    shifted_own_subtitle = client.get(f"/api/cloud-drive/files/{uploaded_file_id}/hls/subtitles/sub01_zh.vtt?shift_ms=500")
    assert shifted_own_subtitle.status_code == 200
    assert "00:00:00.500 --> 00:00:01.500" in shifted_own_subtitle.get_data(as_text=True)

    master = client.get(f"/api/storage/shared/{token}/hls/master.m3u8?password=demo")
    assert master.status_code == 200
    assert master.mimetype == "application/vnd.apple.mpegurl"
    assert "original/playlist.m3u8?password=demo" in master.get_data(as_text=True)

    playlist = client.get(f"/api/storage/shared/{token}/hls/original/playlist.m3u8?password=demo")
    assert playlist.status_code == 200
    playlist_text = playlist.get_data(as_text=True)
    assert 'URI="init.mp4?password=demo"' in playlist_text
    assert "seg_00001.m4s?password=demo" in playlist_text

    shared_subtitle = client.get(f"/api/storage/shared/{token}/hls/subtitles/sub01_zh.vtt?password=demo&shift_ms=500")
    assert shared_subtitle.status_code == 200
    assert shared_subtitle.mimetype == "text/vtt"
    assert "00:00:00.500 --> 00:00:01.500" in shared_subtitle.get_data(as_text=True)

    segment = client.get(f"/api/storage/shared/{token}/hls/original/seg_00001.m4s?password=demo")
    assert segment.status_code == 200
    assert segment.data == b"seg"


def test_cloud_drive_and_storage_share_realtime_proxy_routes_use_auth_and_audio_params(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"not-a-real-mkv-but-route-test"), "clip.mkv", "video/x-matroska"),
            "virtual_path": "clip.mkv",
            "privacy_mode": "standard_plain",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file = uploaded.get_json()["storage_file"]
    storage_file_id = storage_file["id"]
    uploaded_file_id = storage_file["file_id"]
    created = client.post(
        "/api/storage/share-links",
        json={"storage_file_id": storage_file_id, "share_password": "demo"},
    )
    assert created.status_code == 200
    token = created.get_json()["share_link"]["token"]

    calls = []

    def fake_open_realtime_proxy_stream(path, **kwargs):
        calls.append({"path": Path(path), "kwargs": kwargs})
        return {
            "chunks": iter([b"ftyp", b"moof"]),
            "mimetype": "video/mp4",
            "audio_track": {"name": str(kwargs.get("audio_track") or "")},
        }

    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_ENABLED", "1")
    monkeypatch.setattr(files_routes, "open_realtime_proxy_stream", fake_open_realtime_proxy_stream)
    monkeypatch.setattr(share_preview_routes, "open_realtime_proxy_stream", fake_open_realtime_proxy_stream)

    own = client.get(f"/api/cloud-drive/files/{uploaded_file_id}/realtime-proxy?audio=audio_02_eng&start=9.75")
    assert own.status_code == 200
    assert own.mimetype == "video/mp4"
    assert own.get_data() == b"ftypmoof"
    assert own.headers["X-Hackme-Streaming-Mode"] == "realtime_proxy"
    assert own.headers["X-Hackme-Transfer-Mode"] == "python_realtime_proxy"
    assert own.headers["X-Hackme-Audio-Track"] == "audio_02_eng"
    assert calls[-1]["path"].name == "clip.mkv"
    assert calls[-1]["kwargs"]["audio_track"] == "audio_02_eng"
    assert calls[-1]["kwargs"]["start_seconds"] == "9.75"

    shared = client.get(f"/api/storage/shared/{token}/realtime-proxy?password=demo&audio=audio_01_jpn&start=3.5")
    assert shared.status_code == 200
    assert shared.mimetype == "video/mp4"
    assert shared.get_data() == b"ftypmoof"
    assert shared.headers["X-Hackme-Streaming-Mode"] == "realtime_proxy"
    assert shared.headers["X-Hackme-Transfer-Mode"] == "python_realtime_proxy"
    assert shared.headers["X-Hackme-Audio-Track"] == "audio_01_jpn"
    assert calls[-1]["path"].name == "clip.mkv"
    assert calls[-1]["kwargs"]["audio_track"] == "audio_01_jpn"
    assert calls[-1]["kwargs"]["start_seconds"] == "3.5"

    denied = client.get(f"/api/storage/shared/{token}/realtime-proxy?audio=audio_01_jpn")
    assert denied.status_code == 401
    assert denied.get_json()["reason"] == "password_required"


def test_storage_share_link_account_scope_and_view_limit(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"bob only"), "bob.txt"),
            "virtual_path": "bob.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file_id = uploaded.get_json()["storage_file"]["id"]

    created = client.post(
        "/api/storage/share-links",
        json={"storage_file_id": storage_file_id, "access_scope": "account", "required_username": "bob", "max_views": 1},
    )
    assert created.status_code == 200
    share_link = created.get_json()["share_link"]
    assert share_link["access_scope"] == "account"
    assert share_link["required_user_id"] == 2
    assert share_link["max_views"] == 1

    actor_box["actor"] = None
    assert client.get(f"/api/storage/shared/{share_link['token']}").status_code == 401
    assert client.get(f"/api/storage/shared/{share_link['token']}/download").status_code == 401

    actor_box["actor"] = _actor(3, "mallory")
    assert client.get(f"/api/storage/shared/{share_link['token']}").status_code == 403

    actor_box["actor"] = _actor(2, "bob")
    downloaded = client.get(f"/api/storage/shared/{share_link['token']}/download")
    assert downloaded.status_code == 200
    assert downloaded.data == b"bob only"

    exhausted = client.get(f"/api/storage/shared/{share_link['token']}/download")
    assert exhausted.status_code == 410


def test_storage_share_link_account_scope_rejects_non_friend_target(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"mallory blocked"), "blocked.txt"),
            "virtual_path": "blocked.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file_id = uploaded.get_json()["storage_file"]["id"]

    blocked = client.post(
        "/api/storage/share-links",
        json={"storage_file_id": storage_file_id, "access_scope": "account", "required_username": "mallory"},
    )

    assert blocked.status_code == 403
    assert "好友" in blocked.get_json()["msg"]


def test_storage_share_link_e2ee_requires_browser_envelope(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(b"ciphertext"), "vault.bin"),
            "privacy_mode": "e2ee",
            "encrypted_metadata": "sealed:metadata",
            "encrypted_file_key": "sealed:owner-key",
            "ciphertext_sha256": "a" * 64,
            "encryption_algorithm": "XChaCha20-Poly1305",
            "encryption_version": "1",
            "nonce": "nonce",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    missing = client.post("/api/storage/share-links", json={"file_id": file_id})
    assert missing.status_code == 400
    assert "E2EE" in missing.get_json()["msg"]

    envelope = {"alg": "AES-GCM", "v": 1, "nonce": "nonce", "ciphertext": "wrapped"}
    leaky = client.post(
        "/api/storage/share-links",
        json={"file_id": file_id, "wrapped_file_key_envelope": envelope, "fragment_key": "must-stay-client-side"},
    )
    assert leaky.status_code == 400
    assert "不得送到伺服器" in leaky.get_json()["msg"]

    created = client.post(
        "/api/storage/share-links",
        json={"file_id": file_id, "wrapped_file_key_envelope": envelope},
    )
    assert created.status_code == 200
    share_link = created.get_json()["share_link"]
    assert share_link["share_url"] == f"/shared/files/{share_link['token']}"
    assert share_link["requires_fragment_key"] is True

    meta = client.get(f"/api/storage/shared/{share_link['token']}")
    assert meta.status_code == 200
    e2ee = meta.get_json()["file"]["e2ee"]
    assert e2ee["requires_fragment_key"] is True
    assert json.loads(e2ee["wrapped_file_key_envelope"]) == envelope


def test_cloud_drive_text_and_archive_preview(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    text_upload = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"hello preview"), "note.txt")},
        content_type="multipart/form-data",
    )
    assert text_upload.status_code == 200
    text_file_id = text_upload.get_json()["file"]["file_id"]
    text_preview = client.get(f"/api/cloud-drive/files/{text_file_id}/preview")
    assert text_preview.status_code == 200
    text_body = text_preview.get_json()["preview"]
    assert text_body["render_mode"] == "text"
    assert "hello preview" in text_body["text"]

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("docs/readme.txt", "archive preview")
    zip_buffer.seek(0)
    archive_upload = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (zip_buffer, "bundle.zip"),
            "privacy_mode": "standard_plain",
        },
        content_type="multipart/form-data",
    )
    assert archive_upload.status_code == 200
    archive_file_id = archive_upload.get_json()["file"]["file_id"]
    archive_preview = client.get(f"/api/cloud-drive/files/{archive_file_id}/preview")
    assert archive_preview.status_code == 200
    archive_body = archive_preview.get_json()["preview"]
    assert archive_body["render_mode"] == "archive"
    assert archive_body["entries"][0]["name"] == "docs/readme.txt"

    gz_upload = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(gzip.compress(b"compressed preview")), "single.txt.gz"),
            "privacy_mode": "standard_plain",
        },
        content_type="multipart/form-data",
    )
    assert gz_upload.status_code == 200
    gz_file_id = gz_upload.get_json()["file"]["file_id"]
    gz_preview = client.get(f"/api/cloud-drive/files/{gz_file_id}/preview")
    assert gz_preview.status_code == 200
    gz_body = gz_preview.get_json()["preview"]
    assert gz_body["render_mode"] == "archive"
    assert gz_body["entries"][0]["name"] == "single.txt"


def test_cloud_drive_pdf_preview_content_and_e2ee_denied(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    pdf_bytes = b"%PDF-1.4\n% minimal preview fixture\n"
    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(pdf_bytes), "manual.pdf"),
            "privacy_mode": "standard_plain",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    metadata = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert metadata.status_code == 200
    preview = metadata.get_json()["preview"]
    assert preview["category"] == "pdf"
    assert preview["render_mode"] == "media"

    content = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    assert content.status_code == 200
    assert content.data == pdf_bytes
    assert content.mimetype == "application/pdf"

    encrypted = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(pdf_bytes), "sealed.pdf"),
            "privacy_mode": "e2ee",
            "encrypted_metadata": "sealed:metadata",
            "encrypted_file_key": "sealed:owner-key",
            "wrapped_by": "browser_passphrase_pbkdf2_v2",
            "ciphertext_sha256": "a" * 64,
            "encryption_algorithm": "AES-GCM",
            "encryption_version": "browser-passphrase-v2",
            "nonce": "nonce",
        },
        content_type="multipart/form-data",
    )
    assert encrypted.status_code == 200
    encrypted_file_id = encrypted.get_json()["file"]["file_id"]
    denied = client.get(f"/api/cloud-drive/files/{encrypted_file_id}/preview")
    assert denied.status_code == 403
    assert "E2EE" in denied.get_json()["msg"]
    encrypted_content = client.get(f"/api/cloud-drive/files/{encrypted_file_id}/preview/content")
    assert encrypted_content.status_code == 200
    assert encrypted_content.data == pdf_bytes
    assert encrypted_content.mimetype == "application/octet-stream"


def test_cloud_drive_audio_preview_content_supports_streamable_music(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    audio_bytes = b"ID3fake-mp3-audio"
    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(audio_bytes), "song.mp3", "audio/mpeg"),
            "privacy_mode": "server_encrypted",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    metadata = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert metadata.status_code == 200
    preview = metadata.get_json()["preview"]
    assert preview["category"] == "audio"
    assert preview["render_mode"] == "media"
    assert preview["mime_type"] == "audio/mpeg"

    content = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    assert content.status_code == 200
    assert content.data == audio_bytes
    assert content.mimetype == "audio/mpeg"


def test_cloud_drive_text_file_can_be_edited_online(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"old text"), "note.txt")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    edited = client.put(f"/api/cloud-drive/files/{file_id}/text", json={"content": "new text"})
    assert edited.status_code == 200
    assert edited.get_json()["size_bytes"] == len("new text")
    preview = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert preview.status_code == 200
    assert preview.get_json()["preview"]["text"] == "new text"

    actor_box["actor"] = _actor(2, "bob")
    denied = client.put(f"/api/cloud-drive/files/{file_id}/text", json={"content": "take over"})
    assert denied.status_code == 403


def test_cloud_drive_can_create_text_document_and_preview_extensionless_file(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    created = client.post(
        "/api/cloud-drive/files/text",
        json={"filename": "notes", "content": "# hello\nbody", "privacy_mode": "standard_plain"},
    )
    assert created.status_code == 200
    created_json = created.get_json()
    file_id = created_json["file"]["file_id"]
    assert created_json["storage_file"]["file_id"] == file_id
    assert created_json["storage_file"]["virtual_path"] == "/notes"
    preview = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert preview.status_code == 200
    body = preview.get_json()["preview"]
    assert body["filename"] == "notes"
    assert body["render_mode"] == "text"
    assert body["mime_type"] == "text/plain"
    assert "# hello" in body["text"]
    storage_files = client.get("/api/storage/files")
    assert storage_files.status_code == 200
    assert [item["virtual_path"] for item in storage_files.get_json()["files"]] == ["/notes"]


def test_storage_files_auto_syncs_orphan_cloud_uploads(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"orphan text"), "orphan.txt")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    storage_files = client.get("/api/storage/files")
    assert storage_files.status_code == 200
    files = storage_files.get_json()["files"]
    assert len(files) == 1
    assert files[0]["file_id"] == file_id
    assert files[0]["virtual_path"] == "/orphan.txt"


def test_cloud_drive_delete_file_from_ui_api_invalidates_download(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"delete me"), "delete.txt")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT storage_path FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
    finally:
        conn.close()
    stored_path = storage_root / row["storage_path"]
    assert stored_path.exists()
    deleted = client.delete(f"/api/cloud-drive/files/{file_id}")
    assert deleted.status_code == 200
    assert deleted.get_json()["msg"] == "檔案已刪除並釋放容量"
    assert not stored_path.exists()
    assert client.get("/api/cloud-drive/files").get_json()["files"] == []
    trash = client.get("/api/storage/trash")
    assert trash.status_code == 200
    assert all(item["file_id"] != file_id for item in trash.get_json()["files"])
    assert client.get(f"/api/cloud-drive/files/{file_id}/download").status_code == 404


def test_cloud_drive_audio_preview_content(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"not real mp3 but preview route returns bytes"), "sound.mp3")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    metadata = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert metadata.status_code == 200
    assert metadata.get_json()["preview"]["category"] == "audio"
    content = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    assert content.status_code == 200
    assert content.data == b"not real mp3 but preview route returns bytes"
    assert content.mimetype.startswith("audio/")


def test_cloud_drive_image_preview_content(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    image_bytes = b"\x89PNG\r\n\x1a\nnot a full image but enough for route bytes"
    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(image_bytes), "preview.png")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    metadata = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert metadata.status_code == 200
    preview = metadata.get_json()["preview"]
    assert preview["category"] == "image"
    assert preview["render_mode"] == "media"
    content = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    assert content.status_code == 200
    assert content.data == image_bytes
    assert content.mimetype.startswith("image/")


def test_cloud_drive_svg_is_inert_text_and_download_only(tmp_path):
    db_path = tmp_path / "drive-svg.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script></svg>'

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(svg), "active.svg", "image/svg+xml")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    file_id = uploaded.get_json()["file"]["file_id"]

    metadata = client.get(f"/api/cloud-drive/files/{file_id}/preview")
    inline = client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    download = client.get(f"/api/cloud-drive/files/{file_id}/download")

    assert metadata.status_code == 200, metadata.get_json()
    preview = metadata.get_json()["preview"]
    assert preview["category"] == "text"
    assert preview["mime_type"] == "text/plain"
    assert "<script>alert(2)</script>" in preview["text"]
    assert "stream_asset" not in preview
    assert inline.status_code == 415
    assert download.status_code == 200
    assert download.data == svg
    assert download.mimetype == "application/octet-stream"
    assert download.headers["Content-Disposition"].startswith("attachment;")
    assert download.headers["X-Content-Type-Options"] == "nosniff"


def test_server_encrypted_preview_handles_rotated_key_without_500(tmp_path):
    from cryptography.fernet import Fernet

    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    writer_fernet = Fernet(Fernet.generate_key())
    writer_client = _build_app(db_path, storage_root, actor_box, server_file_fernet=writer_fernet).test_client()

    uploaded = writer_client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(b"\x89PNG\r\n\x1a\nold-encrypted-image"), "old.png", "image/png"),
            "privacy_mode": "server_encrypted",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    reader_fernet = Fernet(Fernet.generate_key())
    reader_client = _build_app(db_path, storage_root, actor_box, server_file_fernet=reader_fernet).test_client()

    preview = reader_client.get(f"/api/cloud-drive/files/{file_id}/preview")
    assert preview.status_code == 200
    preview_body = preview.get_json()["preview"]
    assert preview_body["decryption_unavailable"] is True
    assert preview_body["render_mode"] == "metadata"
    assert "重新上傳" in preview_body["message"]

    content = reader_client.get(f"/api/cloud-drive/files/{file_id}/preview/content")
    assert content.status_code == 200
    assert content.mimetype == "image/svg+xml"


def test_storage_admin_summary_sync_and_root_purge(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"admin visible"), "admin.txt"),
            "virtual_path": "admin.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    storage_file_id = uploaded.get_json()["storage_file"]["id"]
    assert client.delete(f"/api/storage/files/{storage_file_id}").status_code == 200

    actor_box["actor"] = _actor(4, "admin", "manager")
    summary = client.get("/api/admin/storage/summary")
    assert summary.status_code == 200
    assert summary.get_json()["summary"]["trashed_files"] == 1

    users = client.get("/api/admin/storage/users")
    assert users.status_code == 200
    assert any(row["username"] == "alice" for row in users.get_json()["users"])

    files = client.get("/api/admin/storage/files?include_trashed=1")
    assert files.status_code == 200
    assert files.get_json()["files"][0]["owner_username"] == "alice"

    synced = client.post("/api/admin/storage/sync-quota")
    assert synced.status_code == 200
    assert len(synced.get_json()["synced"]) >= 1

    denied = client.post("/api/admin/storage/trash/purge", json={"confirm": "PURGE STORAGE TRASH"})
    assert denied.status_code == 403

    actor_box["actor"] = _actor(5, "root", "super_admin")
    bad_confirm = client.post("/api/admin/storage/trash/purge", json={"confirm": "wrong"})
    assert bad_confirm.status_code == 400
    purged = client.post("/api/admin/storage/trash/purge", json={"confirm": "PURGE STORAGE TRASH"})
    assert purged.status_code == 200
    assert purged.get_json()["purged"] == 1

    maintenance = client.get("/api/admin/storage/maintenance")
    assert maintenance.status_code == 200
    assert "maintenance" in maintenance.get_json()
    run = client.post("/api/admin/storage/maintenance")
    assert run.status_code == 200
    assert "synced_users" in run.get_json()["maintenance"]


def test_root_storage_user_quota_override_api(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(4, "admin", "manager")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    denied = client.put(
        "/api/root/storage/users/1/quota-override",
        json={"quota_mb": 2, "reason": "manager should not set root override"},
    )
    assert denied.status_code == 403

    actor_box["actor"] = _actor(5, "root", "super_admin")
    listed = client.get("/api/root/storage/users")
    assert listed.status_code == 200
    listed_payload = listed.get_json()
    assert any(row["username"] == "alice" for row in listed_payload["users"])
    assert listed_payload["storage_capacity"]["disk"]["free_bytes"] >= 0
    assert listed_payload["storage_capacity"]["cloud_used_bytes"] >= 0

    saved = client.put(
        "/api/root/storage/users/1/quota-override",
        json={
            "quota_mb": 2,
            "max_file_size_mb": 1,
            "upload_rate_limit_per_day": 3,
            "can_upload": False,
            "reason": "root direct account setting",
        },
    )
    assert saved.status_code == 200
    payload = saved.get_json()
    assert payload["override"]["enabled"] is True
    assert payload["user"]["quota_source"] == "root_user_override"
    assert payload["user"]["total_bytes"] == 2 * 1024 * 1024
    assert payload["user"]["can_upload"] is False

    detail = client.get("/api/root/storage/users/1")
    assert detail.status_code == 200
    assert detail.get_json()["user"]["override"]["reason"] == "root direct account setting"

    cleared = client.delete("/api/root/storage/users/1/quota-override")
    assert cleared.status_code == 200
    assert cleared.get_json()["user"]["quota_source"] == "member_level_rules.attachment_quota_mb"


def test_storage_summaries_follow_live_quota_after_override(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(5, "root", "super_admin")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    saved = client.put(
        "/api/root/storage/users/1/quota-override",
        json={
            "quota_mb": 2,
            "max_file_size_mb": 1,
            "reason": "quota summary regression",
        },
    )
    assert saved.status_code == 200

    actor_box["actor"] = _actor(1, "alice")
    uploaded = client.post(
        "/api/storage/files",
        data={
            "file": (io.BytesIO(b"quota check"), "quota.txt"),
            "virtual_path": "docs/quota.txt",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200

    listing = client.get("/api/storage/files")
    assert listing.status_code == 200
    quota = client.get("/api/files/quota")
    assert quota.status_code == 200
    listing_payload = listing.get_json()
    quota_payload = quota.get_json()["quota"]
    assert listing_payload["storage"]["quota_bytes"] == 2 * 1024 * 1024
    assert listing_payload["storage"]["quota_bytes"] == quota_payload["total_bytes"]
    assert listing_payload["storage"]["remaining_bytes"] == quota_payload["remaining_bytes"]

    actor_box["actor"] = _actor(4, "admin", "manager")
    users = client.get("/api/admin/storage/users")
    assert users.status_code == 200
    alice = next(row for row in users.get_json()["users"] if row["username"] == "alice")
    assert alice["quota_bytes"] == 2 * 1024 * 1024
    assert alice["used_bytes"] == len(b"quota check")


def test_attach_existing_does_not_duplicate_file_and_delete_invalidates_reference(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"one copy"), "one.txt")},
        content_type="multipart/form-data",
    )
    file_id = uploaded.get_json()["file"]["file_id"]
    attached = client.post(
        "/api/cloud-drive/attach-existing",
        json={"file_id": file_id, "context_type": "forum_post", "context_id": "101", "grant_role": "user"},
    )
    assert attached.status_code == 200
    ref_id = attached.get_json()["attachment"]["ref_id"]

    conn = sqlite3.connect(db_path)
    file_count = conn.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0]
    ref_count = conn.execute("SELECT COUNT(*) FROM cloud_file_refs WHERE file_id=?", (file_id,)).fetchone()[0]
    conn.close()
    assert file_count == 1
    assert ref_count == 1

    actor_box["actor"] = _actor(2, "bob")
    assert client.get(f"/api/cloud-drive/files/{file_id}/download").status_code == 200

    actor_box["actor"] = _actor(1, "alice")
    removed_ref = client.delete(f"/api/cloud-drive/refs/{ref_id}")
    assert removed_ref.status_code == 200
    assert removed_ref.get_json()["msg"] == "附件已移除"
    refs_after_remove = client.get("/api/cloud-drive/refs?context_type=forum_post&context_id=101")
    assert refs_after_remove.status_code == 200
    assert refs_after_remove.get_json()["refs"] == []
    actor_box["actor"] = _actor(2, "bob")
    assert client.get(f"/api/cloud-drive/files/{file_id}/download").status_code == 403

    actor_box["actor"] = _actor(1, "alice")
    attached_again = client.post(
        "/api/cloud-drive/attach-existing",
        json={"file_id": file_id, "context_type": "forum_post", "context_id": "101", "grant_role": "user"},
    )
    assert attached_again.status_code == 200
    ref_id = attached_again.get_json()["attachment"]["ref_id"]
    removed_ref_compat = client.delete("/api/cloud-drive/refs/", json={"ref_id": ref_id})
    assert removed_ref_compat.status_code == 200

    attached_post_delete = client.post(
        "/api/cloud-drive/attach-existing",
        json={"file_id": file_id, "context_type": "forum_post", "context_id": "101", "grant_role": "user"},
    )
    assert attached_post_delete.status_code == 200
    ref_id = attached_post_delete.get_json()["attachment"]["ref_id"]
    removed_ref_post = client.post(f"/api/cloud-drive/refs/{ref_id}/delete", json={})
    assert removed_ref_post.status_code == 200

    deleted = client.delete(f"/api/cloud-drive/files/{file_id}")
    assert deleted.status_code == 200
    actor_box["actor"] = _actor(2, "bob")
    download = client.get(f"/api/cloud-drive/files/{file_id}/download")
    assert download.status_code == 404


def test_cloud_drive_refs_are_paginated_per_context(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"context attachment"), "attachment.txt")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO cloud_file_refs (
                id, file_id, owner_user_id, context_type, context_id,
                attached_by, created_at, permission_snapshot_json
            ) VALUES (?, ?, 1, 'forum_post', 'bulk', 1, ?, '{}')
            """,
            [(f"bulk-ref-{idx}", file_id, f"2026-01-01T00:00:0{idx}") for idx in range(5)],
        )
        conn.commit()
    finally:
        conn.close()

    first = client.get("/api/cloud-drive/refs?context_type=forum_post&context_id=bulk&limit=2")
    assert first.status_code == 200
    first_body = first.get_json()
    assert first_body["limit"] == 2
    assert first_body["offset"] == 0
    assert first_body["has_more"] is True
    assert first_body["next_offset"] == 2
    assert [row["id"] for row in first_body["refs"]] == ["bulk-ref-0", "bulk-ref-1"]

    last = client.get("/api/cloud-drive/refs?context_type=forum_post&context_id=bulk&limit=2&offset=4")
    assert last.status_code == 200
    last_body = last.get_json()
    assert last_body["has_more"] is False
    assert last_body["next_offset"] is None
    assert [row["id"] for row in last_body["refs"]] == ["bulk-ref-4"]


def test_cloud_drive_refs_requires_private_context_membership(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE dm_threads (
            id INTEGER PRIMARY KEY,
            participant_a_id INTEGER NOT NULL,
            participant_b_id INTEGER NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO dm_threads (id, participant_a_id, participant_b_id, created_at, updated_at) VALUES (1, 1, 2, '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"dm private"), "private.txt")},
        content_type="multipart/form-data",
    )
    file_id = uploaded.get_json()["file"]["file_id"]
    attached = client.post(
        "/api/cloud-drive/attach-existing",
        json={"file_id": file_id, "context_type": "dm", "context_id": "1", "grant_user_ids": [2]},
    )
    assert attached.status_code == 200
    owner_refs = client.get("/api/cloud-drive/refs?context_type=dm&context_id=1")
    assert owner_refs.status_code == 200
    assert owner_refs.get_json()["refs"][0]["can_remove"] is True

    actor_box["actor"] = _actor(3, "mallory")
    denied = client.get("/api/cloud-drive/refs?context_type=dm&context_id=1")
    assert denied.status_code == 403

    actor_box["actor"] = _actor(2, "bob")
    allowed = client.get("/api/cloud-drive/refs?context_type=dm&context_id=1")
    assert allowed.status_code == 200
    assert allowed.get_json()["refs"][0]["file_id"] == file_id
    assert allowed.get_json()["refs"][0]["can_remove"] is False


def test_announcement_attachment_requires_root_approval_before_visible(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(4, "admin", "manager")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={"file": (io.BytesIO(b"policy"), "policy.txt")},
        content_type="multipart/form-data",
    )
    file_id = uploaded.get_json()["file"]["file_id"]
    requested = client.post(
        "/api/cloud-drive/announcement-attachment-requests",
        json={"file_id": file_id, "announcement_id": 1, "reason": "公告附件"},
    )
    assert requested.status_code == 200
    request_id = requested.get_json()["request"]["id"]

    refs_before = client.get("/api/cloud-drive/refs?context_type=announcement&context_id=1")
    assert refs_before.status_code == 200
    assert refs_before.get_json()["refs"] == []

    actor_box["actor"] = _actor(5, "root", "super_admin")
    approved = client.post(
        f"/api/root/announcement-attachment-requests/{request_id}/review",
        json={"action": "approve", "reason": "合法公告文件"},
    )
    assert approved.status_code == 200
    conn = sqlite3.connect(db_path)
    owner_user_id = conn.execute("SELECT owner_user_id FROM uploaded_files WHERE id=?", (file_id,)).fetchone()[0]
    conn.close()
    assert owner_user_id == 5

    actor_box["actor"] = _actor(2, "bob")
    refs_after = client.get("/api/cloud-drive/refs?context_type=announcement&context_id=1")
    assert refs_after.status_code == 200
    assert refs_after.get_json()["refs"][0]["file_id"] == file_id


def test_legacy_files_api_upload_status_and_download_alias(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/files/upload",
        data={"file": (io.BytesIO(b"legacy alias"), "legacy.txt")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    status = client.get(f"/api/files/{file_id}/status")
    assert status.status_code == 200
    payload = status.get_json()["file"]
    assert payload["id"] == file_id
    assert payload["privacy_mode"] == "standard_plain"

    download = client.get(f"/api/files/{file_id}/download")
    assert download.status_code == 200
    assert download.data == b"legacy alias"


def test_e2ee_share_and_revoke_controls_download_grant(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/files/upload",
        data={
            "file": (io.BytesIO(b"ciphertext"), "vault.bin"),
            "privacy_mode": "e2ee",
            "encrypted_metadata": "sealed:filename",
            "encrypted_file_key": "sealed:owner-key",
            "ciphertext_sha256": "a" * 64,
            "encryption_algorithm": "XChaCha20-Poly1305",
            "encryption_version": "1",
            "nonce": "nonce",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result, msg = share_e2ee_file(
            conn,
            actor=_actor(1, "alice"),
            file_id=file_id,
            recipient_user_id=3,
            encrypted_file_key="sealed:mallory-key",
        )
        assert result is None
        assert "好友" in msg
    finally:
        conn.rollback()
        conn.close()

    nonfriend = client.post(
        f"/api/files/{file_id}/share",
        json={"recipient_user_id": 3, "encrypted_file_key": "sealed:mallory-key"},
    )
    assert nonfriend.status_code == 403
    assert nonfriend.get_json()["error"] == "friend_required"

    shared = client.post(
        f"/api/files/{file_id}/share",
        json={
            "recipient_user_id": 2,
            "encrypted_file_key": "sealed:bob-key",
            "context_type": "dm",
            "context_id": "room-1",
        },
    )
    assert shared.status_code == 200

    actor_box["actor"] = _actor(2, "bob")
    download = client.get(f"/api/files/{file_id}/download")
    assert download.status_code == 409
    assert download.get_json()["requires_confirmation"] is True

    download = client.get(f"/api/files/{file_id}/download?confirm_high_risk=1")
    assert download.status_code == 200
    assert download.data == b"ciphertext"

    actor_box["actor"] = _actor(4, "admin", "manager")
    manager_revoke = client.post(f"/api/files/{file_id}/share/revoke", json={"recipient_user_id": 2})
    assert manager_revoke.status_code == 400
    actor_box["actor"] = _actor(2, "bob")
    still_allowed = client.get(f"/api/files/{file_id}/download?confirm_high_risk=1")
    assert still_allowed.status_code == 200

    actor_box["actor"] = _actor(1, "alice")
    revoked = client.post(f"/api/files/{file_id}/share/revoke", json={"recipient_user_id": 2})
    assert revoked.status_code == 200
    assert revoked.get_json()["revoked"]["revoked_keys"] == 1

    actor_box["actor"] = _actor(2, "bob")
    denied = client.get(f"/api/files/{file_id}/download")
    assert denied.status_code == 403


def test_e2ee_key_endpoint_returns_only_recipient_key(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    uploaded = client.post(
        "/api/cloud-drive/upload",
        data={
            "file": (io.BytesIO(b"cipher"), "vault.bin"),
            "privacy_mode": "e2ee",
            "encrypted_metadata": "sealed:metadata",
            "encrypted_file_key": "sealed:owner-key",
            "wrapped_by": "browser_passphrase_pbkdf2_v2",
            "ciphertext_sha256": "a" * 64,
            "encryption_algorithm": "AES-GCM",
            "encryption_version": "browser-passphrase-v2",
            "nonce": "nonce",
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    file_id = uploaded.get_json()["file"]["file_id"]

    owner_key = client.get(f"/api/cloud-drive/files/{file_id}/e2ee-key")
    assert owner_key.status_code == 200
    body = owner_key.get_json()
    assert body["ok"] is True
    assert body["e2ee"]["encrypted_file_key"] == "sealed:owner-key"
    assert body["e2ee"]["encrypted_metadata"] == "sealed:metadata"
    assert body["e2ee"]["wrapped_by"] == "browser_passphrase_pbkdf2_v2"

    actor_box["actor"] = _actor(3, "mallory")
    denied = client.get(f"/api/cloud-drive/files/{file_id}/e2ee-key")
    assert denied.status_code == 403
    assert "沒有可用的解密金鑰" in denied.get_json()["msg"]



def test_remote_download_capabilities_reports_transmission_backend(monkeypatch):
    from services.storage import remote_downloads

    monkeypatch.setenv("HACKME_BT_BACKEND", "auto")
    monkeypatch.setattr(remote_downloads.shutil, "which", lambda name: None)
    monkeypatch.setattr(remote_downloads, "_transmission_rpc_call", lambda *args, **kwargs: {})

    caps = remote_downloads.remote_download_capabilities()

    assert caps["bt_magnet"] is True
    assert caps["bt_file"] is True
    assert caps["bt_backend"] == "auto"
    assert caps["bt_backend_active"] == "transmission"
    assert caps["transmission_rpc_available"] is True


def test_remote_download_magnet_prefers_transmission_backend(tmp_path, monkeypatch):
    from services.storage import remote_downloads

    calls = []

    def fake_rpc(method, arguments=None, **kwargs):
        arguments = arguments or {}
        calls.append((method, arguments))
        if method == "torrent-add":
            target = Path(arguments["download-dir"]) / "movie.mp4"
            target.write_bytes(b"video")
            return {"torrent-added": {"id": 7, "name": "movie.mp4"}}
        if method == "torrent-get":
            return {
                "torrents": [{
                    "id": 7,
                    "name": "movie.mp4",
                    "percentDone": 1,
                    "totalSize": 5,
                    "downloadedEver": 5,
                    "rateDownload": 0,
                    "error": 0,
                    "errorString": "",
                    "files": [],
                }]
            }
        if method in {"torrent-remove", "torrent-set"}:
            return {}
        raise AssertionError(method)

    monkeypatch.setenv("HACKME_BT_BACKEND", "auto")
    monkeypatch.setattr(remote_downloads, "_transmission_rpc_call", fake_rpc)
    monkeypatch.setattr(remote_downloads, "_bt_progress_interval_seconds", lambda: 0)
    monkeypatch.setattr(remote_downloads.shutil, "which", lambda name: "/usr/bin/aria2c")

    downloaded = remote_downloads.download_magnet_with_aria2(
        "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        timeout_seconds=5,
    )

    assert downloaded.filename == "movie.mp4"
    assert Path(downloaded.path).read_bytes() == b"video"
    torrent_add = next(call for call in calls if call[0] == "torrent-add")
    assert torrent_add[1]["filename"].startswith("magnet:")
    assert ("torrent-remove", {"ids": [7], "delete-local-data": False}) in calls


def test_remote_download_transmission_backend_resolves_magnet_metadata_with_aria2(tmp_path, monkeypatch):
    from services.storage import remote_downloads

    calls = []

    class MetadataPopen:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""
            target = Path(cmd[cmd.index("--dir") + 1]) / "payload.torrent"
            target.write_bytes(b"torrent-metainfo")

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return "", ""

    def fake_rpc(method, arguments=None, **kwargs):
        arguments = arguments or {}
        calls.append((method, arguments))
        if method == "torrent-add":
            assert "metainfo" in arguments
            assert "filename" not in arguments
            target = Path(arguments["download-dir"]) / "movie.mp4"
            target.write_bytes(b"video")
            return {"torrent-added": {"id": 8, "name": "movie.mp4"}}
        if method == "torrent-get":
            return {
                "torrents": [{
                    "id": 8,
                    "name": "movie.mp4",
                    "percentDone": 1,
                    "totalSize": 5,
                    "downloadedEver": 5,
                    "rateDownload": 0,
                    "error": 0,
                    "errorString": "",
                    "files": [],
                }]
            }
        if method in {"torrent-remove", "torrent-set"}:
            return {}
        raise AssertionError(method)

    monkeypatch.setenv("HACKME_BT_BACKEND", "transmission")
    monkeypatch.setattr(remote_downloads, "_transmission_rpc_call", fake_rpc)
    monkeypatch.setattr(remote_downloads, "_bt_progress_interval_seconds", lambda: 0)
    monkeypatch.setattr(remote_downloads.shutil, "which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr(remote_downloads.subprocess, "Popen", MetadataPopen)

    downloaded = remote_downloads.download_magnet_with_aria2(
        "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        timeout_seconds=5,
    )

    assert downloaded.filename == "movie.mp4"
    assert Path(downloaded.path).read_bytes() == b"video"
    assert ("torrent-remove", {"ids": [8], "delete-local-data": False}) in calls


def test_remote_download_transmission_stages_global_incomplete_file(tmp_path, monkeypatch):
    from services.storage import remote_downloads

    incomplete_dir = tmp_path / "transmission-incomplete"
    incomplete_dir.mkdir()
    calls = []

    def fake_rpc(method, arguments=None, **kwargs):
        arguments = arguments or {}
        calls.append((method, arguments))
        if method == "session-get":
            return {"incomplete-dir-enabled": True, "incomplete-dir": str(incomplete_dir)}
        if method == "torrent-add":
            (incomplete_dir / "movie.mp4").write_bytes(b"video")
            return {"torrent-added": {"id": 7, "name": "movie.mp4"}}
        if method == "torrent-get":
            download_dir = calls[1][1]["download-dir"]
            return {
                "torrents": [{
                    "id": 7,
                    "name": "movie.mp4",
                    "status": 4,
                    "percentDone": 1,
                    "leftUntilDone": 0,
                    "totalSize": 5,
                    "downloadedEver": 5,
                    "rateDownload": 0,
                    "error": 0,
                    "errorString": "",
                    "downloadDir": download_dir,
                    "files": [{"name": "movie.mp4", "length": 5, "bytesCompleted": 5}],
                }]
            }
        if method in {"torrent-remove", "torrent-set"}:
            return {}
        raise AssertionError(method)

    monkeypatch.setenv("HACKME_BT_BACKEND", "transmission")
    monkeypatch.setenv("HACKME_BT_DOWNLOAD_STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setattr(remote_downloads, "_transmission_rpc_call", fake_rpc)
    monkeypatch.setattr(remote_downloads, "_bt_progress_interval_seconds", lambda: 0)

    downloaded = remote_downloads.download_magnet_with_aria2(
        "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        timeout_seconds=5,
        owner_user_id=1,
        task_id="qa",
    )

    try:
        assert downloaded.filename == "movie.mp4"
        assert Path(downloaded.path).read_bytes() == b"video"
        assert not (incomplete_dir / "movie.mp4").exists()
        assert ("torrent-remove", {"ids": [7], "delete-local-data": False}) in calls
    finally:
        shutil.rmtree(downloaded.cleanup_dir, ignore_errors=True)


def test_remote_download_capabilities_and_rejects_local_paths(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    caps = client.get("/api/cloud-drive/remote-download/capabilities")
    assert caps.status_code == 200
    assert caps.get_json()["capabilities"]["direct_link"] is True

    blocked = client.post(
        "/api/cloud-drive/remote-download",
        json={"url": "file:///etc/passwd", "privacy_mode": "standard_plain"},
    )
    assert blocked.status_code == 400
    assert "http" in blocked.get_json()["msg"]

    blocked_task = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={"url": "file:///etc/passwd", "privacy_mode": "standard_plain"},
    )
    assert blocked_task.status_code == 400
    assert "http" in blocked_task.get_json()["msg"]


def test_remote_download_saves_to_cloud_drive_and_storage(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = tmp_path / "remote.txt"
    source.write_text("remote content", encoding="utf-8")

    class FakeDownloaded:
        path = str(source)
        filename = "remote.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    def fake_download(url, **kwargs):
        assert url == "https://example.test/remote.txt"
        assert kwargs["max_bytes"] > 0
        return FakeDownloaded()

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    res = client.post(
        "/api/cloud-drive/remote-download",
        json={
            "url": "https://example.test/remote.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/remote.txt",
        },
    )
    body = res.get_json()

    assert res.status_code == 200
    assert body["file"]["filename"] == "remote.txt"
    assert body["storage_file"]["virtual_path"] == "/Downloads/remote.txt"


def test_remote_download_task_reports_progress_and_completion(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = tmp_path / "remote-task.txt"
    source.write_text("remote task content", encoding="utf-8")

    class FakeDownloaded:
        path = str(source)
        filename = "remote-task.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    def fake_download(url, **kwargs):
        progress = kwargs.get("progress_callback")
        assert progress
        progress({"phase": "downloading", "filename": "remote-task.txt", "loaded_bytes": 5, "total_bytes": 20, "speed_bytes_per_sec": 1024})
        progress({"phase": "downloaded", "filename": "remote-task.txt", "loaded_bytes": 20, "total_bytes": 20, "speed_bytes_per_sec": 0})
        return FakeDownloaded()

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/remote-task.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/remote-task.txt",
        },
    )
    assert created.status_code == 202
    task_id = created.get_json()["task"]["id"]

    body = {}
    for _ in range(600):
        status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
        assert status.status_code == 200
        body = status.get_json()["task"]
        if body["status"] == "completed":
            break
        time.sleep(0.05)

    assert body["status"] == "completed"
    assert body["progress_percent"] == 100
    assert body["file"]["filename"] == "remote-task.txt"
    assert body["file"]["size_bytes"] == len(source.read_bytes())
    assert body["loaded_bytes"] == len(source.read_bytes())
    assert body["total_bytes"] == len(source.read_bytes())
    assert body["speed_bytes_per_sec"] == 0
    assert body["storage_file"]["virtual_path"] == "/Downloads/remote-task.txt"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT * FROM job_center_jobs
            WHERE source_module='cloud_drive_remote_download' AND source_ref=?
            """,
            (f"remote_download:{task_id}",),
        ).fetchone()
        assert job is not None
        assert job["owner_user_id"] == 1
        assert job["job_type"] == "cloud_drive.remote_download.direct"
        assert job["status"] == "succeeded"
        assert job["progress_percent"] == 100
    finally:
        conn.close()


def test_remote_download_task_status_falls_back_to_persisted_job(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()
    task_id = "persisted-task"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        create_job(
            conn,
            owner_user_id=1,
            created_by_user_id=1,
            job_type="cloud_drive.remote_download.bt.torrent_file",
            title="BT 下載：bad.torrent",
            description="遠端 direct link / BT 下載、掃描與保存",
            source_module="cloud_drive_remote_download",
            source_ref=f"remote_download:{task_id}",
            status="failed",
            progress_percent=100,
            stage="failed",
            stage_detail="BT/magnet 下載失敗：tracker 無回應",
            cancellable=False,
            metadata={
                "task_id": task_id,
                "source_type": "torrent_file",
                "filename": "bad.torrent",
                "torrent_filename": "bad.torrent",
                "url": "BT 檔案：bad.torrent",
                "speed_bytes_per_sec": 0,
            },
        )
        conn.commit()
    finally:
        conn.close()

    status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
    body = status.get_json()

    assert status.status_code == 200
    assert body["task"]["id"] == task_id
    assert body["task"]["status"] == "failed"
    assert body["task"]["source_type"] == "torrent_file"
    assert "tracker" in body["task"]["msg"]

    listed = client.get("/api/cloud-drive/remote-download/tasks").get_json()["tasks"]
    assert any(item["id"] == task_id and item["status"] == "failed" for item in listed)

    actor_box["actor"] = _actor(2, "bob")
    denied = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
    assert denied.status_code == 403

    denied_remove = client.delete(f"/api/cloud-drive/remote-download/tasks/{task_id}")
    assert denied_remove.status_code == 403
    actor_box["actor"] = _actor(1, "alice")
    removed = client.delete(f"/api/cloud-drive/remote-download/tasks/{task_id}")
    assert removed.status_code == 200
    assert removed.get_json()["removed"] is True
    listed_after_remove = client.get("/api/cloud-drive/remote-download/tasks").get_json()["tasks"]
    assert all(item["id"] != task_id for item in listed_after_remove)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job_row = conn.execute(
            "SELECT * FROM job_center_jobs WHERE source_module='cloud_drive_remote_download' AND source_ref=?",
            (f"remote_download:{task_id}",),
        ).fetchone()
    finally:
        conn.close()
    assert job_row is None


def test_remote_download_persisted_running_task_can_be_cancel_requested_without_memory(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()
    task_id = "persisted-running-task"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        create_job(
            conn,
            owner_user_id=1,
            created_by_user_id=1,
            job_type="cloud_drive.remote_download.bt.magnet",
            title="BT 下載：magnet",
            description="遠端 direct link / BT 下載、掃描與保存",
            source_module="cloud_drive_remote_download",
            source_ref=f"remote_download:{task_id}",
            status="running",
            progress_percent=0,
            stage="downloading",
            stage_detail="下載中",
            cancellable=True,
            metadata={
                "task_id": task_id,
                "source_type": "magnet",
                "filename": "BT/magnet",
                "url": "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "timeout_seconds": 1800,
            },
        )
        conn.commit()
    finally:
        conn.close()

    listed = client.get("/api/cloud-drive/remote-download/tasks").get_json()["tasks"]
    assert any(item["id"] == task_id and item["status"] == "running" for item in listed)

    actor_box["actor"] = _actor(2, "bob")
    denied = client.post(f"/api/cloud-drive/remote-download/tasks/{task_id}/cancel")
    assert denied.status_code == 403

    actor_box["actor"] = _actor(1, "alice")
    cancel = client.post(f"/api/cloud-drive/remote-download/tasks/{task_id}/cancel")
    body = cancel.get_json()
    assert cancel.status_code == 200
    assert body["ok"] is True
    assert body["task"]["id"] == task_id
    assert body["task"]["status"] == "running"
    assert body["task"]["phase"] == "cancel_requested"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            "SELECT status, stage, cancel_requested_at FROM job_center_jobs WHERE source_module='cloud_drive_remote_download' AND source_ref=?",
            (f"remote_download:{task_id}",),
        ).fetchone()
    finally:
        conn.close()
    assert job["status"] == "running"
    assert job["stage"] == "cancel_requested"
    assert job["cancel_requested_at"]

    still_running = client.delete(f"/api/cloud-drive/remote-download/tasks/{task_id}")
    assert still_running.status_code == 409


def test_remote_download_worker_honors_persisted_cancel_request(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    started = threading.Event()

    def fake_download(url, **kwargs):
        cancel_check = kwargs.get("cancel_check")
        assert callable(cancel_check)
        started.set()
        while True:
            cancel_check()
            time.sleep(0.01)

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/cross-worker-cancel.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/cross-worker-cancel.txt",
        },
    )
    assert created.status_code == 202
    task_id = created.get_json()["task"]["id"]
    assert started.wait(timeout=10)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            "SELECT * FROM job_center_jobs WHERE source_module='cloud_drive_remote_download' AND source_ref=?",
            (f"remote_download:{task_id}",),
        ).fetchone()
        assert job is not None
        metadata = json.loads(job["metadata_json"] or "{}")
        metadata["control_action"] = "cancel"
        now = "2026-05-24T00:00:00"
        conn.execute(
            """
            UPDATE job_center_jobs
            SET cancel_requested_at=?, stage='cancel_requested', stage_detail=?, metadata_json=?, updated_at=?
            WHERE job_uuid=?
            """,
            (
                now,
                "已要求取消下載任務，正在停止目前 worker",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                now,
                job["job_uuid"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    final = {}
    for _ in range(300):
        final = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}").get_json()["task"]
        if final["status"] == "cancelled":
            break
        time.sleep(0.03)
    assert final["status"] == "cancelled"
    assert final["file"] is None
    assert final["storage_file"] is None

    conn = sqlite3.connect(db_path)
    try:
        saved = conn.execute("SELECT COUNT(*) FROM uploaded_files WHERE original_filename_plain_for_public LIKE '%cross-worker-cancel%'").fetchone()[0]
        assert saved == 0
    finally:
        conn.close()


def test_remote_download_task_can_pause_and_resume(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = tmp_path / "resume-after-pause.txt"
    source.write_text("resumed content", encoding="utf-8")
    started = threading.Event()
    calls = {"count": 0}

    class FakeDownloaded:
        path = str(source)
        filename = "resume-after-pause.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    def fake_download(url, **kwargs):
        calls["count"] += 1
        cancel_check = kwargs.get("cancel_check")
        assert callable(cancel_check)
        progress = kwargs.get("progress_callback")
        if progress:
            progress({"phase": "downloading", "filename": "resume-after-pause.txt", "loaded_bytes": 3, "total_bytes": 15})
        if calls["count"] == 1:
            started.set()
            while True:
                cancel_check()
                time.sleep(0.01)
        return FakeDownloaded()

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/resume-after-pause.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/resume-after-pause.txt",
        },
    )
    assert created.status_code == 202
    task_id = created.get_json()["task"]["id"]
    assert started.wait(timeout=10)

    paused_request = client.post(f"/api/cloud-drive/remote-download/tasks/{task_id}/pause")
    assert paused_request.status_code == 200

    paused = {}
    for _ in range(200):
        paused = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}").get_json()["task"]
        if paused["status"] == "paused":
            break
        time.sleep(0.03)
    assert paused["status"] == "paused"
    assert "暫停" in paused["msg"]

    resumed = client.post(f"/api/cloud-drive/remote-download/tasks/{task_id}/resume")
    assert resumed.status_code == 200
    assert resumed.get_json()["task"]["status"] == "queued"

    final = {}
    for _ in range(300):
        final = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}").get_json()["task"]
        if final["status"] == "completed":
            break
        time.sleep(0.03)
    assert final["status"] == "completed"
    assert calls["count"] >= 2
    assert final["storage_file"]["virtual_path"] == "/Downloads/resume-after-pause.txt"


def test_remote_download_task_can_be_cancelled_without_saving_file(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    started = threading.Event()

    def fake_download(url, **kwargs):
        cancel_check = kwargs.get("cancel_check")
        assert callable(cancel_check)
        started.set()
        while True:
            cancel_check()
            time.sleep(0.01)

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/cancel-me.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/cancel-me.txt",
        },
    )
    assert created.status_code == 202
    task_id = created.get_json()["task"]["id"]
    assert started.wait(timeout=10)

    cancelled_request = client.post(f"/api/cloud-drive/remote-download/tasks/{task_id}/cancel")
    assert cancelled_request.status_code == 200

    final = {}
    for _ in range(200):
        final = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}").get_json()["task"]
        if final["status"] == "cancelled":
            break
        time.sleep(0.03)
    assert final["status"] == "cancelled"
    assert final["file"] is None
    assert final["storage_file"] is None

    conn = sqlite3.connect(db_path)
    try:
        saved = conn.execute("SELECT COUNT(*) FROM uploaded_files WHERE original_filename_plain_for_public LIKE '%cancel-me%'").fetchone()[0]
        assert saved == 0
    finally:
        conn.close()


def test_remote_download_worker_survives_logout_or_session_change(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = tmp_path / "session-independent.txt"
    source.write_text("background survives session changes", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    class FakeDownloaded:
        path = str(source)
        filename = "session-independent.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    def fake_download(url, **kwargs):
        started.set()
        assert release.wait(timeout=10)
        return FakeDownloaded()

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/session-independent.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/session-independent.txt",
        },
    )
    assert created.status_code == 202
    task_id = created.get_json()["task"]["id"]
    assert started.wait(timeout=10)

    actor_box["actor"] = None
    logged_out_status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
    assert logged_out_status.status_code == 401
    actor_box["actor"] = _actor(2, "bob")
    other_user_status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
    assert other_user_status.status_code == 403

    release.set()
    job = None
    for _ in range(200):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            try:
                job = conn.execute(
                    """
                    SELECT * FROM job_center_jobs
                    WHERE source_module='cloud_drive_remote_download' AND source_ref=? AND status='succeeded'
                    """,
                    (f"remote_download:{task_id}",),
                ).fetchone()
            except sqlite3.OperationalError:
                job = None
        finally:
            conn.close()
        if job:
            break
        time.sleep(0.05)

    assert job is not None
    assert job["owner_user_id"] == 1
    actor_box["actor"] = _actor(1, "alice")
    final_status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
    assert final_status.status_code == 200
    final_body = final_status.get_json()["task"]
    assert final_body["status"] == "completed"
    assert final_body["storage_file"]["virtual_path"] == "/Downloads/session-independent.txt"


def test_remote_download_tasks_can_run_concurrently_per_user(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(
        db_path,
        storage_root,
        actor_box,
        settings={
            "storage_trash_retention_days": 30,
            "remote_download_max_concurrent_global": 2,
            "remote_download_max_concurrent_per_user": 2,
        },
    ).test_client()

    first_source = tmp_path / "first.txt"
    second_source = tmp_path / "second.txt"
    first_source.write_text("first content", encoding="utf-8")
    second_source.write_text("second content", encoding="utf-8")
    release_first = threading.Event()
    first_started = threading.Event()

    class FakeDownloaded:
        def __init__(self, path, filename):
            self.path = str(path)
            self.filename = filename
            self.mimetype = "text/plain"
            self.cleanup_dir = None

    def fake_download(url, **kwargs):
        if url.endswith("/first.txt"):
            first_started.set()
            assert release_first.wait(timeout=30)
            return FakeDownloaded(first_source, "first.txt")
        return FakeDownloaded(second_source, "second.txt")

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    first = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/first.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/first.txt",
        },
    )
    assert first.status_code == 202
    first_id = first.get_json()["task"]["id"]
    assert first_started.wait(timeout=10)

    second = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/second.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/second.txt",
        },
    )
    assert second.status_code == 202
    second_id = second.get_json()["task"]["id"]

    final_second = {}
    for _ in range(600):
        final_second = client.get(f"/api/cloud-drive/remote-download/tasks/{second_id}").get_json()["task"]
        if final_second["status"] == "completed":
            break
        time.sleep(0.05)
    assert final_second["status"] == "completed"

    release_first.set()
    final_first = {}
    for _ in range(600):
        final_first = client.get(f"/api/cloud-drive/remote-download/tasks/{first_id}").get_json()["task"]
        if final_first["status"] == "completed":
            break
        time.sleep(0.05)

    assert final_first["status"] == "completed"
    assert final_first["storage_file"]["virtual_path"] == "/Downloads/first.txt"
    assert final_second["storage_file"]["virtual_path"] == "/Downloads/second.txt"


def test_remote_download_dev_env_override_beats_persisted_limits(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    monkeypatch.setenv("HACKME_REMOTE_DOWNLOAD_LIMITS_PREFER_ENV", "1")
    monkeypatch.setenv("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL", "2")
    monkeypatch.setenv("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER", "2")
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(
        db_path,
        storage_root,
        actor_box,
        settings={
            "storage_trash_retention_days": 30,
            "remote_download_max_concurrent_global": 1,
            "remote_download_max_concurrent_per_user": 1,
        },
    ).test_client()

    release_first = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()

    class FakeDownloaded:
        def __init__(self, filename):
            path = tmp_path / filename
            path.write_text(filename, encoding="utf-8")
            self.path = str(path)
            self.filename = filename
            self.mimetype = "text/plain"
            self.cleanup_dir = None

    def fake_download(url, **kwargs):
        filename = url.rsplit("/", 1)[-1]
        if filename == "first.txt":
            first_started.set()
            assert release_first.wait(timeout=30)
        if filename == "second.txt":
            second_started.set()
        return FakeDownloaded(filename)

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    first = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/first.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/first.txt",
        },
    )
    second = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/second.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/second.txt",
        },
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first_started.wait(timeout=10)
    assert second_started.wait(timeout=10)

    release_first.set()
    task_ids = [first.get_json()["task"]["id"], second.get_json()["task"]["id"]]
    for _ in range(600):
        states = [client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}").get_json()["task"]["status"] for task_id in task_ids]
        if states == ["completed", "completed"]:
            break
        time.sleep(0.05)
    assert states == ["completed", "completed"]


def test_remote_download_third_task_waits_for_per_user_worker_limit(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    monkeypatch.setenv("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL", "2")
    monkeypatch.setenv("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER", "2")
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    release = threading.Event()
    started = []

    class FakeDownloaded:
        def __init__(self, filename):
            path = tmp_path / filename
            path.write_text(filename, encoding="utf-8")
            self.path = str(path)
            self.filename = filename
            self.mimetype = "text/plain"
            self.cleanup_dir = None

    def fake_download(url, **kwargs):
        filename = url.rsplit("/", 1)[-1]
        started.append(filename)
        if filename in {"first.txt", "second.txt"}:
            assert release.wait(timeout=10)
        return FakeDownloaded(filename)

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    ids = []
    for name in ("first.txt", "second.txt", "third.txt"):
        res = client.post(
            "/api/cloud-drive/remote-download/tasks",
            json={
                "url": f"https://93.184.216.34/{name}",
                "privacy_mode": "standard_plain",
                "virtual_path": f"/Downloads/{name}",
            },
        )
        assert res.status_code == 202
        ids.append(res.get_json()["task"]["id"])

    for _ in range(40):
        if {"first.txt", "second.txt"} <= set(started):
            break
        time.sleep(0.03)
    assert {"first.txt", "second.txt"} <= set(started)
    third_task = client.get(f"/api/cloud-drive/remote-download/tasks/{ids[2]}").get_json()["task"]
    assert third_task["status"] == "queued"
    assert "worker" in third_task["msg"]

    release.set()
    for _ in range(600):
        states = [client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}").get_json()["task"]["status"] for task_id in ids]
        if states == ["completed", "completed", "completed"]:
            break
        time.sleep(0.05)
    assert states == ["completed", "completed", "completed"]


def test_remote_download_task_without_virtual_path_is_visible_in_storage(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = tmp_path / "bt-result.txt"
    source.write_text("bt result content", encoding="utf-8")

    class FakeDownloaded:
        path = str(source)
        filename = "bt-result.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    monkeypatch.setattr("routes.files.download_torrent_url_with_aria2", lambda *args, **kwargs: FakeDownloaded())
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/sample.torrent",
            "download_mode": "bt",
            "privacy_mode": "standard_plain",
        },
    )
    assert created.status_code == 202
    task_id = created.get_json()["task"]["id"]

    body = {}
    for _ in range(600):
        status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
        assert status.status_code == 200
        body = status.get_json()["task"]
        if body["status"] == "completed":
            break
        time.sleep(0.05)

    assert body["status"] == "completed"
    assert body["storage_file"]["virtual_path"] == "/Downloads/bt-result.txt"
    listed = client.get("/api/storage/files")
    assert listed.status_code == 200
    paths = [item["virtual_path"] for item in listed.get_json()["files"]]
    assert "/Downloads/bt-result.txt" in paths


def test_remote_download_task_direct_mode_keeps_torrent_file(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = tmp_path / "sample.torrent"
    source.write_bytes(b"d8:announce0:e")
    captured = {}

    class FakeDownloaded:
        path = str(source)
        filename = "sample.torrent"
        mimetype = "application/x-bittorrent"
        cleanup_dir = None

    def fake_download(url, **kwargs):
        captured["url"] = url
        captured["treat_torrent_as_bt"] = kwargs.get("treat_torrent_as_bt")
        return FakeDownloaded()

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/sample.torrent",
            "download_mode": "direct",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/sample.torrent",
        },
    )
    assert created.status_code == 202
    task = created.get_json()["task"]
    assert task["source_type"] == "direct"
    task_id = task["id"]

    body = {}
    for _ in range(600):
        status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
        assert status.status_code == 200
        body = status.get_json()["task"]
        if body["status"] == "completed":
            break
        time.sleep(0.05)

    assert captured["url"] == "https://93.184.216.34/sample.torrent"
    assert captured["treat_torrent_as_bt"] is False
    assert body["status"] == "completed"
    assert body["file"]["filename"] == "sample.torrent"


def test_remote_download_task_bt_mode_torrent_url_downloads_payload(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = tmp_path / "bt-payload.txt"
    source.write_text("bt payload", encoding="utf-8")
    captured = {}

    class FakeDownloaded:
        path = str(source)
        filename = "bt-payload.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    def fake_download(url, **kwargs):
        captured["url"] = url
        progress = kwargs.get("progress_callback")
        assert progress
        progress({"phase": "downloading", "filename": "sample.torrent", "loaded_bytes": 1, "total_bytes": None})
        return FakeDownloaded()

    monkeypatch.setattr("routes.files.download_torrent_url_with_aria2", fake_download)
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/sample.torrent",
            "download_mode": "bt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/bt-payload.txt",
        },
    )
    assert created.status_code == 202
    task = created.get_json()["task"]
    assert task["source_type"] == "torrent_url"
    task_id = task["id"]

    body = {}
    for _ in range(600):
        status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
        assert status.status_code == 200
        body = status.get_json()["task"]
        if body["status"] == "completed":
            break
        time.sleep(0.05)

    assert captured["url"] == "https://93.184.216.34/sample.torrent"
    assert body["status"] == "completed"
    assert body["file"]["filename"] == "bt-payload.txt"


def test_remote_download_task_list_restores_refresh_state(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = tmp_path / "refresh-task.txt"
    source.write_text("refresh task content", encoding="utf-8")

    class FakeDownloaded:
        path = str(source)
        filename = "refresh-task.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    def fake_download(url, **kwargs):
        progress = kwargs.get("progress_callback")
        assert progress
        progress({"phase": "downloading", "filename": "refresh-task.txt", "loaded_bytes": 3, "total_bytes": 30})
        time.sleep(0.05)
        progress({"phase": "downloaded", "filename": "refresh-task.txt", "loaded_bytes": 30, "total_bytes": 30})
        return FakeDownloaded()

    monkeypatch.setattr("routes.files.download_remote_url", fake_download)
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/refresh-task.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/refresh-task.txt",
        },
    )
    assert created.status_code == 202
    task_id = created.get_json()["task"]["id"]

    listed = client.get("/api/cloud-drive/remote-download/tasks")
    assert listed.status_code == 200
    tasks = listed.get_json()["tasks"]
    assert any(task["id"] == task_id for task in tasks)

    # Wait for background thread to finish so user slot is released before next test.
    for _ in range(50):
        status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
        if status.get_json()["task"]["status"] != "running":
            break
        time.sleep(0.02)


def test_remote_download_stale_running_task_does_not_block_new_task(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    with files_routes._REMOTE_DOWNLOAD_TASKS_LOCK:
        files_routes._REMOTE_DOWNLOAD_TASKS.clear()
        files_routes._REMOTE_DOWNLOAD_ACTIVE_USERS.clear()
        files_routes._REMOTE_DOWNLOAD_TASKS["stale-task"] = {
            "id": "stale-task",
            "kind": "remote_download",
            "source_type": "url",
            "status": "running",
            "phase": "downloading",
            "filename": "",
            "url": "https://93.184.216.34/stale.txt",
            "owner_user_id": 1,
            "actor": dict(actor_box["actor"]),
            "privacy_mode": "standard_plain",
            "virtual_path": "",
            "timeout_seconds": 1,
            "loaded_bytes": 0,
            "total_bytes": None,
            "progress_percent": 0,
            "msg": "舊任務",
            "error": "",
            "file": None,
            "storage_file": None,
            "created_at": "2000-01-01T00:00:00",
            "updated_at": "2000-01-01T00:00:00",
        }
        files_routes._REMOTE_DOWNLOAD_ACTIVE_USERS.add(1)

    source = tmp_path / "fresh-task.txt"
    source.write_text("fresh task content", encoding="utf-8")

    class FakeDownloaded:
        path = str(source)
        filename = "fresh-task.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    monkeypatch.setattr("routes.files.download_remote_url", lambda url, **kwargs: FakeDownloaded())
    created = client.post(
        "/api/cloud-drive/remote-download/tasks",
        json={
            "url": "https://93.184.216.34/fresh-task.txt",
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/fresh-task.txt",
        },
    )
    assert created.status_code == 202
    fresh_task_id = created.get_json()["task"]["id"]

    listed = client.get("/api/cloud-drive/remote-download/tasks")
    assert listed.status_code == 200
    stale = next(task for task in listed.get_json()["tasks"] if task["id"] == "stale-task")
    assert stale["status"] == "failed"
    assert "逾時" in stale["msg"]

    removed = client.delete("/api/cloud-drive/remote-download/tasks/stale-task")
    assert removed.status_code == 200
    assert removed.get_json()["removed"] is True
    listed_after_remove = client.get("/api/cloud-drive/remote-download/tasks")
    assert all(task["id"] != "stale-task" for task in listed_after_remove.get_json()["tasks"])

    # Wait for the fresh task's background thread to finish so it releases
    # the user slot in _REMOTE_DOWNLOAD_ACTIVE_USERS before the next test runs.
    for _ in range(50):
        status = client.get(f"/api/cloud-drive/remote-download/tasks/{fresh_task_id}")
        if status.get_json()["task"]["status"] != "running":
            break
        time.sleep(0.02)


def test_remote_download_tasks_are_owner_scoped_for_manager(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(4, "admin", "manager")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    with files_routes._REMOTE_DOWNLOAD_TASKS_LOCK:
        files_routes._REMOTE_DOWNLOAD_TASKS["alice-task"] = {
            "id": "alice-task",
            "kind": "remote_download",
            "source_type": "url",
            "status": "finished",
            "phase": "done",
            "filename": "alice.txt",
            "url": "https://93.184.216.34/alice.txt",
            "owner_user_id": 1,
            "actor": _actor(1, "alice"),
            "privacy_mode": "standard_plain",
            "virtual_path": "",
            "timeout_seconds": 1,
            "loaded_bytes": 5,
            "total_bytes": 5,
            "progress_percent": 100,
            "msg": "完成",
            "error": "",
            "file": None,
            "storage_file": None,
            "created_at": "2026-05-14T01:00:00",
            "updated_at": "2026-05-14T01:00:01",
        }

    listed = client.get("/api/cloud-drive/remote-download/tasks")
    assert listed.status_code == 200
    assert listed.get_json()["tasks"] == []

    status = client.get("/api/cloud-drive/remote-download/tasks/alice-task")
    assert status.status_code == 403

    removed = client.delete("/api/cloud-drive/remote-download/tasks/alice-task")
    assert removed.status_code == 403


def test_remote_download_task_accepts_uploaded_torrent_file(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = tmp_path / "torrent-result.txt"
    source.write_text("torrent task content", encoding="utf-8")
    captured = {}

    class FakeDownloaded:
        path = str(source)
        filename = "torrent-result.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    def fake_download(torrent_path, **kwargs):
        captured["torrent_path"] = torrent_path
        captured["display_name"] = kwargs.get("display_name")
        progress = kwargs.get("progress_callback")
        assert torrent_path.endswith("sample.torrent")
        assert progress
        progress({"phase": "downloading", "filename": "sample.torrent", "loaded_bytes": 0, "total_bytes": None})
        return FakeDownloaded()

    monkeypatch.setattr("routes.files.download_torrent_file_with_aria2", fake_download)
    created = client.post(
        "/api/cloud-drive/remote-download/torrent-tasks",
        data={
            "torrent_file": (io.BytesIO(b"d8:announce0:e"), "sample.torrent"),
            "privacy_mode": "standard_plain",
            "virtual_path": "/Downloads/torrent-result.txt",
        },
    )
    assert created.status_code == 202
    task = created.get_json()["task"]
    assert task["source_type"] == "torrent_file"
    assert task["torrent_filename"] == "sample.torrent"
    task_id = task["id"]

    body = {}
    for _ in range(600):
        status = client.get(f"/api/cloud-drive/remote-download/tasks/{task_id}")
        assert status.status_code == 200
        body = status.get_json()["task"]
        if body["status"] == "completed":
            break
        time.sleep(0.05)

    assert captured["display_name"] == "sample.torrent"
    assert body["status"] == "completed"
    assert body["file"]["filename"] == "torrent-result.txt"
    assert body["storage_file"]["virtual_path"] == "/Downloads/torrent-result.txt"


def test_remote_download_torrent_upload_rejects_non_torrent(tmp_path):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    res = client.post(
        "/api/cloud-drive/remote-download/torrent-tasks",
        data={"torrent_file": (io.BytesIO(b"not torrent"), "note.txt")},
    )

    assert res.status_code == 400
    assert ".torrent" in res.get_json()["msg"]


def test_remote_download_torrent_upload_accepts_and_excludes_private_tracker(tmp_path, monkeypatch):
    db_path = tmp_path / "drive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor(1, "alice")}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    def fake_getaddrinfo(host, port, **kwargs):
        if host == "tracker.example":
            return [(2, 1, 6, "", ("127.0.0.1", int(port or 80)))]
        return [(2, 1, 6, "", ("8.8.8.8", int(port or 80)))]

    tracker = b"http://tracker.example/announce"
    payload = (
        b"d8:announce" + str(len(tracker)).encode("ascii") + b":" + tracker +
        b"4:infod4:name4:test12:piece lengthi16384e6:pieces0:ee"
    )
    source = tmp_path / "torrent-result.txt"
    source.write_text("torrent task content", encoding="utf-8")

    class FakeDownloaded:
        path = str(source)
        filename = "torrent-result.txt"
        mimetype = "text/plain"
        cleanup_dir = None

    monkeypatch.setattr("routes.files.download_torrent_file_with_aria2", lambda *args, **kwargs: FakeDownloaded())
    monkeypatch.setattr("services.storage.remote_downloads.socket.getaddrinfo", fake_getaddrinfo)

    res = client.post(
        "/api/cloud-drive/remote-download/torrent-tasks",
        data={"torrent_file": (io.BytesIO(payload), "bad.torrent")},
    )

    assert res.status_code == 202
    assert res.get_json()["task"]["source_type"] == "torrent_file"
