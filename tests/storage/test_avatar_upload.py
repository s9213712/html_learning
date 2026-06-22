import io
import sqlite3

import pytest
from cryptography.fernet import Fernet
from flask import Flask, jsonify

from routes.users import register_user_routes
from services.users.member_levels import ensure_member_level_user_columns
from services.security.upload_security import ensure_upload_security_schema, update_cloud_drive_security_policy
from services.storage.cloud_drive import encrypt_server_encrypted_chunked_stream


def _build_app(db_path, storage_root, actor_box, server_file_fernet=None):
    app = Flask(__name__)
    app.testing = True

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def passthrough(fn):
        return fn

    register_user_routes(app, {
        "ACCOUNT_STATUSES": {"active", "inactive"},
        "MAX_MANAGERS": 3,
        "MAX_EXTRA_SUPER_ADMINS": 1,
        "MEMBER_LEVELS": {"newbie", "normal", "trusted", "vip", "restricted", "suspended"},
        "PASSWORD_HISTORY_LIMIT": 5,
        "ROLE_LABEL": {"user": "一般用戶", "super_admin": "最高管理者"},
        "ROLE_RANK": {"user": 0, "manager": 1, "super_admin": 2},
        "STORAGE_DIR": str(storage_root),
        "add_violation": lambda *args, **kwargs: None,
        "audit": lambda *args, **kwargs: None,
        "check_user_rate_limit": lambda *args, **kwargs: (False, {"limit": 5}),
        "count_role": lambda role: 0,
        "decrypt_field": lambda value: value or "",
        "encrypt_field": lambda value: value or "",
        "ensure_user_official_room_membership": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": get_db,
        "get_member_level_rule": lambda conn, level: {
            "can_upload_attachment": True,
            "attachment_quota_mb": 10,
            "max_attachment_size_mb": 2,
            "upload_rate_limit_per_day": 20,
        },
        "get_system_settings": lambda: {},
        "get_ua": lambda: "test",
        "hash_password": lambda value: "hash",
        "hash_token": lambda value: "hash",
        "is_feature_enabled": lambda key: True,
        "json_resp": lambda payload: jsonify(payload),
        "normalize_text": lambda value: value.strip() if isinstance(value, str) else "",
        "parse_birthdate": lambda value: value,
        "parse_positive_int": lambda value, **kwargs: int(value),
        "revoke_user_sessions": lambda *args, **kwargs: None,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": False,
        "server_file_fernet": server_file_fernet,
        "enforce_password_strength": lambda value, min_score=3: (True, "", {"score": 4}),
        "score_password_strength": lambda value: {"score": 4},
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "user_public_payload": lambda row, include_sensitive=False: dict(row),
        "validate_id_number": lambda value: True,
        "validate_password": lambda value: (True, ""),
        "validate_phone": lambda value: True,
        "verify_password": lambda *args: True,
    })
    return app


def _seed_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            member_level TEXT NOT NULL DEFAULT 'trusted',
            effective_level TEXT NOT NULL DEFAULT 'trusted',
            sanction_status TEXT NOT NULL DEFAULT 'none'
        )
        """
    )
    ensure_member_level_user_columns(conn)
    ensure_upload_security_schema(conn)
    update_cloud_drive_security_policy(conn, {"scanner_enabled": False, "scanner_backend": "disabled"})
    conn.execute("INSERT INTO users (id, username, role, member_level, effective_level) VALUES (1, 'alice', 'user', 'trusted', 'trusted')")
    conn.execute("INSERT INTO users (id, username, role, member_level, effective_level) VALUES (2, 'bob', 'user', 'trusted', 'trusted')")
    conn.commit()
    conn.close()


def test_user_can_upload_avatar_and_crop_metadata(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    db_path = tmp_path / "avatar.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_db(db_path)
    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color=(100, 50, 30)).save(buf, format="JPEG")
    buf.seek(0)

    res = client.post(
        "/api/admin/users/1/avatar",
        data={
            "file": (buf, "avatar.jpg", "image/jpeg"),
            "crop_json": '{"x":1,"y":2,"width":12,"height":10}',
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["avatar_crop"] == {"x": 1, "y": 2, "width": 12, "height": 10}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT avatar_file_id, avatar_crop_json FROM users WHERE id=1").fetchone()
    file_row = conn.execute("SELECT scan_status FROM uploaded_files WHERE id=?", (user["avatar_file_id"],)).fetchone()
    conn.close()
    assert user["avatar_file_id"] == payload["avatar_file_id"]
    assert file_row["scan_status"] in {"clean", "not_required"}

    actor_box["actor"] = {"id": 2, "username": "bob", "role": "user", "member_level": "trusted", "effective_level": "trusted"}
    avatar_res = client.get("/api/admin/users/1/avatar")
    assert avatar_res.status_code == 200
    assert avatar_res.mimetype == "image/jpeg"
    cropped = Image.open(io.BytesIO(avatar_res.data))
    assert cropped.size == (512, 512)


def test_avatar_get_center_crops_static_image_without_crop_metadata(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    db_path = tmp_path / "avatar.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_db(db_path)
    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    buf = io.BytesIO()
    Image.new("RGB", (40, 20), color=(100, 50, 30)).save(buf, format="JPEG")
    buf.seek(0)

    res = client.post(
        "/api/admin/users/1/avatar",
        data={"file": (buf, "avatar.jpg", "image/jpeg")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200

    avatar_res = client.get("/api/admin/users/1/avatar")
    assert avatar_res.status_code == 200
    cropped = Image.open(io.BytesIO(avatar_res.data))
    assert cropped.size == (512, 512)


def test_avatar_crop_near_edge_keeps_requested_box_inside_image(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    db_path = tmp_path / "avatar.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_db(db_path)
    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = Image.new("RGB", (20, 20), color=(10, 80, 220))
    for x in range(20):
        source.putpixel((x, 19), (255, 255, 255))
    buf = io.BytesIO()
    source.save(buf, format="PNG")
    buf.seek(0)

    res = client.post(
        "/api/admin/users/1/avatar",
        data={
            "file": (buf, "avatar.png", "image/png"),
            "crop_json": '{"x":10,"y":19,"width":20,"height":20}',
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 200

    avatar_res = client.get("/api/admin/users/1/avatar")
    assert avatar_res.status_code == 200
    cropped = Image.open(io.BytesIO(avatar_res.data)).convert("RGB")
    assert cropped.size == (512, 512)
    assert cropped.getpixel((256, 256)) == (10, 80, 220)


def test_user_can_select_existing_cloud_image_as_avatar(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    db_path = tmp_path / "avatar.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_db(db_path)
    existing_path = storage_root / "users" / "1" / "cloud-avatar.jpg"
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 24), color=(20, 80, 160)).save(existing_path, format="JPEG")
    size_bytes = existing_path.stat().st_size

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO uploaded_files (
                id, owner_user_id, storage_path, privacy_mode, risk_level, scan_status,
                original_filename_plain_for_public, mime_type_plain_for_public,
                size_bytes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cloud-avatar",
                1,
                "users/1/cloud-avatar.jpg",
                "standard_plain",
                "low",
                "clean",
                "cloud-avatar.jpg",
                "image/jpeg",
                size_bytes,
                "2026-05-18T00:00:00",
                "2026-05-18T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}}
    client = _build_app(db_path, storage_root, actor_box).test_client()
    res = client.post(
        "/api/admin/users/1/avatar",
        data={
            "cloud_file_id": "cloud-avatar",
            "crop_json": '{"x":2,"y":3,"width":18,"height":18}',
        },
        content_type="multipart/form-data",
    )

    assert res.status_code == 200
    payload = res.get_json()
    assert payload["avatar_file_id"] == "cloud-avatar"
    assert payload["avatar_crop"] == {"x": 2, "y": 3, "width": 18, "height": 18}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        user = conn.execute("SELECT avatar_file_id, avatar_crop_json FROM users WHERE id=1").fetchone()
        file_count = conn.execute("SELECT COUNT(*) AS count FROM uploaded_files").fetchone()["count"]
        ref = conn.execute("SELECT context_type FROM cloud_file_refs WHERE file_id='cloud-avatar'").fetchone()
    finally:
        conn.close()
    assert user["avatar_file_id"] == "cloud-avatar"
    assert file_count == 1
    assert ref["context_type"] == "avatar"


def test_avatar_crop_metadata_keeps_rotation_and_get_applies_it(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    db_path = tmp_path / "avatar.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_db(db_path)
    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}}
    client = _build_app(db_path, storage_root, actor_box).test_client()

    source = Image.new("RGB", (30, 10), color=(20, 80, 160))
    buf = io.BytesIO()
    source.save(buf, format="JPEG")
    buf.seek(0)

    res = client.post(
        "/api/admin/users/1/avatar",
        data={
            "file": (buf, "avatar.jpg", "image/jpeg"),
            "crop_json": '{"x":0,"y":0,"width":10,"height":10,"rotation":90}',
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["avatar_crop"] == {"x": 0, "y": 0, "width": 10, "height": 10, "rotation": 90}

    avatar_res = client.get("/api/admin/users/1/avatar")
    assert avatar_res.status_code == 200
    cropped = Image.open(io.BytesIO(avatar_res.data))
    assert cropped.size == (512, 512)


def test_user_can_select_server_encrypted_cloud_image_as_avatar(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    db_path = tmp_path / "avatar.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    plain = io.BytesIO()
    Image.new("RGB", (24, 24), color=(90, 40, 170)).save(plain, format="JPEG")
    payload = plain.getvalue()
    encrypted_path = storage_root / "users" / "1" / "server-avatar.server_encrypt"
    encrypted_path.parent.mkdir(parents=True, exist_ok=True)
    encrypt_server_encrypted_chunked_stream(io.BytesIO(payload), encrypted_path, fernet, size_bytes=len(payload))

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO uploaded_files (
                id, owner_user_id, storage_path, privacy_mode, risk_level, scan_status,
                original_filename_plain_for_public, mime_type_plain_for_public,
                size_bytes, encryption_algorithm, encryption_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "server-avatar",
                1,
                "users/1/server-avatar.server_encrypt",
                "server_encrypted",
                "low",
                "clean",
                "server-avatar.jpg",
                "image/jpeg",
                len(payload),
                "Fernet",
                "server-side-chunked-v1",
                "2026-05-18T00:00:00",
                "2026-05-18T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    actor_box = {"actor": {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}}
    client = _build_app(db_path, storage_root, actor_box, server_file_fernet=fernet).test_client()
    res = client.post(
        "/api/admin/users/1/avatar",
        data={"cloud_file_id": "server-avatar", "crop_json": '{"x":1,"y":1,"width":20,"height":20}'},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200

    avatar_res = client.get("/api/admin/users/1/avatar")
    assert avatar_res.status_code == 200
    cropped = Image.open(io.BytesIO(avatar_res.data))
    assert cropped.size == (512, 512)
