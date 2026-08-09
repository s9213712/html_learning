import json
import os
import uuid
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from cryptography.fernet import InvalidToken

from services.storage.paths import resolve_storage_path
from services.storage.storage_albums import ensure_storage_album_schema
from services.users.friends import assert_can_target_user
from services.security.upload_security import (
    create_uploaded_file_record,
    ensure_upload_security_schema,
    get_cloud_drive_security_policy,
    get_user_cloud_drive_usage,
    is_e2ee_privacy_mode,
    is_server_encrypted_privacy_mode,
    safe_public_filename,
    scan_uploaded_file,
    storage_root_can_accept_bytes,
)


CONTEXT_TYPES = {"dm", "group_chat", "chat_message", "forum_thread", "forum_post", "forum_comment", "announcement", "avatar"}
ANNOUNCEMENT_REQUEST_STATUSES = {"pending", "approved", "rejected"}
DEFAULT_SERVER_ENCRYPTED_INLINE_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_SERVER_ENCRYPTED_CHUNK_BYTES = 4 * 1024 * 1024
SERVER_ENCRYPTED_CHUNKED_VERSION = "server-side-chunked-v1"
SERVER_ENCRYPTED_CHUNKED_MAGIC = b"HACKME_SERVER_ENCRYPTED_CHUNKED_V1\n"


# Browser and API reads call this helper frequently. Cache a committed schema
# verification so routine reads do not repeatedly run DDL and the legacy
# avatar-repair UPDATE under mixed load.
_CLOUD_DRIVE_SCHEMA_LOCK = threading.Lock()
_CLOUD_DRIVE_SCHEMA_READY_PATHS = set()


def server_encrypted_inline_max_bytes():
    raw_bytes = os.environ.get("HACKME_SERVER_ENCRYPTED_INLINE_MAX_BYTES")
    raw_mb = os.environ.get("HACKME_SERVER_ENCRYPTED_INLINE_MAX_MB")
    try:
        value = int(raw_bytes) if raw_bytes is not None else int(raw_mb) * 1024 * 1024 if raw_mb is not None else DEFAULT_SERVER_ENCRYPTED_INLINE_MAX_BYTES
    except Exception:
        value = DEFAULT_SERVER_ENCRYPTED_INLINE_MAX_BYTES
    return max(0, int(value))


def server_encrypted_chunk_bytes():
    raw_bytes = os.environ.get("HACKME_SERVER_ENCRYPTED_CHUNK_BYTES")
    raw_mb = os.environ.get("HACKME_SERVER_ENCRYPTED_CHUNK_MB")
    try:
        value = int(raw_bytes) if raw_bytes is not None else int(raw_mb) * 1024 * 1024 if raw_mb is not None else DEFAULT_SERVER_ENCRYPTED_CHUNK_BYTES
    except Exception:
        value = DEFAULT_SERVER_ENCRYPTED_CHUNK_BYTES
    return max(64 * 1024, int(value))


def _server_encrypted_size_error(size_bytes, *, operation="upload"):
    limit = server_encrypted_inline_max_bytes()
    if limit <= 0 or int(size_bytes or 0) <= limit:
        return ""
    limit_mb = max(1, limit // (1024 * 1024))
    if operation == "decrypt":
        return (
            f"此伺服器端加密檔案超過 {limit_mb} MB；為避免主伺服器一次解密大檔造成卡頓，"
            "目前暫停在主程序預覽或下載。請改用 E2EE / HLS 背景處理，或請管理員評估調整上限。"
        )
    return (
        f"此伺服器端加密操作超過 {limit_mb} MB；"
        "請使用 chunked server-side encryption 路徑，避免主伺服器一次載入完整檔案。"
    )


def ensure_cloud_drive_attachment_schema(conn):
    ensure_upload_security_schema(conn)
    db_path = _connection_path(conn)
    if db_path and db_path in _CLOUD_DRIVE_SCHEMA_READY_PATHS:
        return
    with _CLOUD_DRIVE_SCHEMA_LOCK:
        if db_path and db_path in _CLOUD_DRIVE_SCHEMA_READY_PATHS:
            return
        # Do not cache while this caller could still roll back its migration.
        if db_path and not getattr(conn, "in_transaction", False) and _cloud_drive_schema_cache_valid(conn):
            _CLOUD_DRIVE_SCHEMA_READY_PATHS.add(db_path)
            return
        _ensure_cloud_drive_attachment_schema_uncached(conn)


def _connection_path(conn):
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row["file"] if hasattr(row, "keys") else row[2])
    except Exception:
        return ""


def _cloud_drive_schema_cache_valid(conn):
    try:
        names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('cloud_file_refs', 'file_access_grants', 'announcement_attachment_requests')"
            ).fetchall()
        }
        return names == {"cloud_file_refs", "file_access_grants", "announcement_attachment_requests"}
    except Exception:
        return False


def _ensure_cloud_drive_attachment_schema_uncached(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cloud_file_refs (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            context_type TEXT NOT NULL,
            context_id TEXT NOT NULL,
            attached_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            permission_snapshot_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_access_grants (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            granted_to_user_id INTEGER,
            granted_to_role TEXT,
            granted_to_group_id TEXT,
            context_type TEXT NOT NULL,
            context_id TEXT NOT NULL,
            can_download INTEGER NOT NULL DEFAULT 1,
            can_preview INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS announcement_attachment_requests (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            requested_by INTEGER NOT NULL REFERENCES users(id),
            announcement_id INTEGER REFERENCES announcements(id),
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_by INTEGER REFERENCES users(id),
            reviewed_at TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_file_refs_file ON cloud_file_refs(file_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_file_refs_context ON cloud_file_refs(context_type, context_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_file_refs_owner ON cloud_file_refs(owner_user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_access_grants_file ON file_access_grants(file_id, revoked_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_access_grants_user_context ON file_access_grants(granted_to_user_id, context_type, context_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_announcement_attachment_requests_status ON announcement_attachment_requests(status, created_at)")
    try:
        conn.execute(
            """
            UPDATE uploaded_files
            SET system_asset_type='avatar'
            WHERE COALESCE(system_asset_type, '')=''
              AND id IN (
                  SELECT file_id
                  FROM cloud_file_refs
                  WHERE context_type='avatar'
              )
            """
        )
    except Exception:
        pass


def _now():
    return datetime.now().isoformat()


def is_e2ee_file(row_or_mode):
    mode = row_or_mode["privacy_mode"] if hasattr(row_or_mode, "keys") and "privacy_mode" in row_or_mode.keys() else row_or_mode
    return is_e2ee_privacy_mode(mode)


def is_server_encrypted_file(row_or_mode):
    mode = row_or_mode["privacy_mode"] if hasattr(row_or_mode, "keys") and "privacy_mode" in row_or_mode.keys() else row_or_mode
    return is_server_encrypted_privacy_mode(mode)


def is_chunked_server_encrypted_file(row):
    try:
        return str(row["encryption_version"] or "") == SERVER_ENCRYPTED_CHUNKED_VERSION
    except Exception:
        return False


def _is_chunked_server_encrypted_path(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(len(SERVER_ENCRYPTED_CHUNKED_MAGIC)) == SERVER_ENCRYPTED_CHUNKED_MAGIC
    except Exception:
        return False


def _read_chunked_manifest(handle):
    header = handle.read(len(SERVER_ENCRYPTED_CHUNKED_MAGIC))
    if header != SERVER_ENCRYPTED_CHUNKED_MAGIC:
        raise ValueError("不是 chunked server-side encrypted 檔案")
    try:
        return json.loads(handle.readline().decode("utf-8"))
    except Exception as exc:
        raise ValueError("伺服器端加密 chunk manifest 已損壞") from exc


def encrypt_server_encrypted_chunked_stream(source, target, server_file_fernet, *, size_bytes=None, chunk_size=None):
    if not server_file_fernet:
        raise ValueError("server-side file encryption key is unavailable")
    chunk_size = int(chunk_size or server_encrypted_chunk_bytes())
    target = Path(target)
    tmp_path = target.with_name(f".{target.name}.chunked-encrypt-{uuid.uuid4().hex}.tmp")
    ciphertext_sha = hashlib.sha256()
    plaintext_sha = hashlib.sha256()
    chunk_count = 0
    plaintext_size = 0
    manifest = {
        "version": 1,
        "chunk_size": chunk_size,
        "plaintext_size": int(size_bytes or 0),
    }
    try:
        with open(tmp_path, "wb") as out:
            out.write(SERVER_ENCRYPTED_CHUNKED_MAGIC)
            out.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                plaintext_size += len(chunk)
                plaintext_sha.update(chunk)
                token = server_file_fernet.encrypt(chunk)
                ciphertext_sha.update(token)
                out.write(str(len(token)).encode("ascii") + b"\n")
                out.write(token + b"\n")
                chunk_count += 1
        os.replace(tmp_path, target)
        return {
            "ciphertext_sha256": ciphertext_sha.hexdigest(),
            "plaintext_sha256": plaintext_sha.hexdigest(),
            "chunk_count": chunk_count,
            "plaintext_size": plaintext_size,
            "chunk_size": chunk_size,
        }
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def iter_decrypted_server_encrypted_chunks(path, server_file_fernet, *, start=None, end=None):
    if not server_file_fernet:
        raise ValueError("server-side file encryption key is unavailable")
    start = 0 if start is None else max(0, int(start))
    end = None if end is None else int(end)
    with open(path, "rb") as handle:
        _read_chunked_manifest(handle)
        plaintext_offset = 0
        while True:
            length_line = handle.readline()
            if not length_line:
                break
            try:
                token_len = int(length_line.strip())
            except Exception as exc:
                raise ValueError("伺服器端加密 chunk 長度已損壞") from exc
            token = handle.read(token_len)
            handle.readline()
            try:
                plain = server_file_fernet.decrypt(token)
            except InvalidToken as exc:
                raise ValueError("此檔案無法以目前伺服器金鑰解密，可能是重設或換鑰後留下的舊檔案，請重新上傳") from exc
            chunk_start = plaintext_offset
            chunk_end = plaintext_offset + len(plain) - 1
            plaintext_offset += len(plain)
            if end is not None and chunk_start > end:
                break
            if chunk_end < start:
                continue
            slice_start = max(0, start - chunk_start)
            slice_end = len(plain) if end is None else min(len(plain), end - chunk_start + 1)
            if slice_start < slice_end:
                yield plain[slice_start:slice_end]


def _chunked_server_encrypted_plaintext_size(path):
    with open(path, "rb") as handle:
        manifest = _read_chunked_manifest(handle)
    try:
        return max(0, int((manifest or {}).get("plaintext_size") or 0))
    except Exception:
        return 0


def decrypt_server_encrypted_bytes(path, server_file_fernet):
    if not server_file_fernet:
        raise ValueError("server-side file encryption key is unavailable")
    if _is_chunked_server_encrypted_path(path):
        return b"".join(iter_decrypted_server_encrypted_chunks(path, server_file_fernet))
    size_error = _server_encrypted_size_error(Path(path).stat().st_size, operation="decrypt")
    if size_error:
        raise ValueError(size_error)
    try:
        return server_file_fernet.decrypt(Path(path).read_bytes())
    except InvalidToken as exc:
        raise ValueError("此檔案無法以目前伺服器金鑰解密，可能是重設或換鑰後留下的舊檔案，請重新上傳") from exc


def _emit_decrypt_progress(progress_callback, bytes_written, total_bytes):
    if not progress_callback:
        return
    try:
        progress_callback(int(bytes_written or 0), int(total_bytes or 0))
    except Exception:
        return


def write_decrypted_server_encrypted_file(path, target, server_file_fernet, *, progress_callback=None):
    target = Path(target)
    if _is_chunked_server_encrypted_path(path):
        bytes_written = 0
        total_bytes = _chunked_server_encrypted_plaintext_size(path)
        with open(target, "wb") as out:
            for chunk in iter_decrypted_server_encrypted_chunks(path, server_file_fernet):
                out.write(chunk)
                bytes_written += len(chunk)
                _emit_decrypt_progress(progress_callback, bytes_written, total_bytes)
        return target
    plaintext = decrypt_server_encrypted_bytes(path, server_file_fernet)
    target.write_bytes(plaintext)
    _emit_decrypt_progress(progress_callback, len(plaintext), len(plaintext))
    return target


def _actor_value(actor, key, default=None):
    if not actor:
        return default
    try:
        return actor[key]
    except Exception:
        return actor.get(key, default) if hasattr(actor, "get") else default


def _actor_role(actor):
    return "super_admin" if actor and _actor_value(actor, "username") == "root" else _actor_value(actor, "role", "user")


def _role_rank_value(role):
    return {"user": 0, "manager": 1, "admin": 1, "super_admin": 2}.get(role or "user", 0)


def is_manager_or_root(actor):
    return _role_rank_value(_actor_role(actor)) >= _role_rank_value("manager")


def _actor_owns(actor, owner_user_id):
    try:
        return int(_actor_value(actor, "id")) == int(owner_user_id)
    except Exception:
        return False


def mark_avatar_file_asset(conn, *, file_id, owner_user_id=None):
    file_id = str(file_id or "").strip()
    if not file_id:
        return False
    ensure_upload_security_schema(conn)
    params = [_now(), file_id]
    owner_clause = ""
    if owner_user_id is not None:
        owner_clause = " AND owner_user_id=?"
        params.append(int(owner_user_id))
    return bool(conn.execute(
        f"""
        UPDATE uploaded_files
        SET system_asset_type='avatar', updated_at=?
        WHERE id=? AND deleted_at IS NULL{owner_clause}
        """,
        tuple(params),
    ).rowcount)


def _file_is_current_avatar(conn, file_id):
    try:
        return bool(conn.execute(
            "SELECT 1 FROM users WHERE avatar_file_id=? LIMIT 1",
            (str(file_id or "").strip(),),
        ).fetchone())
    except Exception:
        return False


def _file_is_avatar_asset(conn, file_id):
    file_id = str(file_id or "").strip()
    if not file_id:
        return False
    try:
        row = conn.execute("SELECT system_asset_type FROM uploaded_files WHERE id=? LIMIT 1", (file_id,)).fetchone()
        if row and str(row["system_asset_type"] or "").strip().lower() == "avatar":
            return True
    except Exception:
        pass
    try:
        return bool(conn.execute(
            "SELECT 1 FROM cloud_file_refs WHERE file_id=? AND context_type='avatar' LIMIT 1",
            (file_id,),
        ).fetchone())
    except Exception:
        return False


def can_remove_context_attachment(actor, ref_row):
    if not actor or not ref_row:
        return False
    try:
        actor_id = int(_actor_value(actor, "id"))
        return actor_id in {int(ref_row["attached_by"]), int(ref_row["owner_user_id"])}
    except Exception:
        return False


def validate_context_type(context_type):
    value = str(context_type or "").strip()
    if value not in CONTEXT_TYPES:
        raise ValueError("unsupported attachment context_type")
    return value


def _context_id(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("context_id is required")
    return text[:80]


def _active_grant_where(actor, action):
    field = "can_preview" if action == "preview" else "can_download"
    role = _actor_role(actor)
    return (
        f"revoked_at IS NULL AND {field}=1 AND "
        "(expires_at IS NULL OR expires_at > ?) AND "
        "((granted_to_user_id IS NOT NULL AND granted_to_user_id=?) OR "
        "(granted_to_role IS NOT NULL AND granted_to_role IN (?, ?)))"
    ), (_now(), int(actor["id"]), role, "user")


def _create_file_access_log(conn, *, file_id, actor_user_id, action, result, reason=None, ip=None, user_agent=None):
    conn.execute(
        """
        INSERT INTO file_access_logs (
            id, file_id, actor_user_id, action, ip, user_agent, result, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            file_id,
            actor_user_id,
            action,
            ip,
            (user_agent or "")[:200] if user_agent else None,
            result,
            reason,
            _now(),
        ),
    )


def list_cloud_files(conn, actor, *, limit=50, offset=0):
    ensure_cloud_drive_attachment_schema(conn)
    ensure_storage_album_schema(conn)
    rows = conn.execute(
        """
        SELECT f.*, COUNT(r.id) AS ref_count,
               (
                   SELECT sf.display_name
                   FROM storage_files sf
                   WHERE sf.file_id=f.id
                         AND sf.owner_user_id=f.owner_user_id
                         AND sf.deleted_at IS NULL
                         AND COALESCE(sf.is_trashed, 0)=0
                   ORDER BY sf.updated_at DESC, sf.created_at DESC
                   LIMIT 1
               ) AS storage_display_name,
               (
                   SELECT sf.virtual_path
                   FROM storage_files sf
                   WHERE sf.file_id=f.id
                         AND sf.owner_user_id=f.owner_user_id
                         AND sf.deleted_at IS NULL
                         AND COALESCE(sf.is_trashed, 0)=0
                   ORDER BY sf.updated_at DESC, sf.created_at DESC
                   LIMIT 1
               ) AS storage_virtual_path
        FROM uploaded_files f
        LEFT JOIN cloud_file_refs r ON r.file_id=f.id
        WHERE f.owner_user_id=? AND f.deleted_at IS NULL
              AND COALESCE(f.system_asset_type, '')<>'avatar'
              AND NOT EXISTS (
                  SELECT 1
                  FROM cloud_file_refs avatar_refs
                  WHERE avatar_refs.file_id=f.id
                        AND avatar_refs.context_type='avatar'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM storage_files sf
                  WHERE sf.file_id=f.id
                        AND sf.owner_user_id=f.owner_user_id
                        AND sf.deleted_at IS NULL
                        AND sf.is_trashed=1
                        AND sf.trash_source='cloud_drive_delete'
              )
        GROUP BY f.id
        ORDER BY f.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (int(actor["id"]), int(limit), int(offset)),
    ).fetchall()
    return [serialize_file_row(row) for row in rows]


def serialize_file_row(row):
    data = dict(row)
    storage_display_name = str(data.pop("storage_display_name", "") or "").strip()
    storage_virtual_path = str(data.pop("storage_virtual_path", "") or "").strip()
    if storage_virtual_path:
        data["virtual_path"] = storage_virtual_path
    if storage_display_name:
        data["display_name"] = storage_display_name
    elif storage_virtual_path:
        virtual_name = storage_virtual_path.rstrip("/").rsplit("/", 1)[-1].strip()
        if virtual_name:
            data["display_name"] = virtual_name
    for key in ("size_bytes", "owner_user_id"):
        if key in data and data[key] is not None:
            data[key] = int(data[key])
    data["ref_count"] = int(data.get("ref_count") or 0)
    return data


def _check_quota(conn, actor, member_rule, size_bytes, storage_root=None):
    usage = get_user_cloud_drive_usage(conn, actor, member_rule=member_rule, storage_root=storage_root)
    if not usage["can_upload"]:
        return False, "目前會員等級或處分狀態不可上傳"
    remaining = usage.get("remaining_bytes")
    if remaining is not None and int(size_bytes) > int(remaining):
        return False, "超過雲端硬碟容量上限"
    max_file = usage.get("max_file_size_bytes")
    if max_file is not None and int(size_bytes) > int(max_file):
        return False, "檔案超過單檔大小限制"
    disk_ok, _disk = storage_root_can_accept_bytes(storage_root, size_bytes)
    if not disk_ok:
        return False, "Host 磁碟可用空間不足，請先清理檔案或擴充儲存空間"
    daily_limit = usage.get("upload_rate_limit_per_day")
    if daily_limit is not None and int(daily_limit) >= 0:
        today = datetime.now().date().isoformat()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM uploaded_files WHERE owner_user_id=? AND created_at>=? AND deleted_at IS NULL",
            (int(actor["id"]), f"{today}T00:00:00"),
        ).fetchone()
        if int(row["c"] or 0) >= int(daily_limit):
            return False, "已達每日上傳限制"
    return True, ""


def store_cloud_upload(
    conn,
    *,
    actor,
    member_rule,
    storage_root,
    file_storage,
    privacy_mode="standard_plain",
    encrypted_metadata=None,
    encrypted_file_key=None,
    wrapped_by="user_public_key",
    ciphertext_sha256=None,
    encryption_algorithm=None,
    encryption_version=None,
    nonce=None,
    client_scan_report=None,
    scan_now=True,
    system_asset_type=None,
    quota_exempt=False,
    server_file_fernet=None,
):
    ensure_cloud_drive_attachment_schema(conn)
    filename = safe_public_filename(getattr(file_storage, "filename", "") or "upload.bin")
    stream = getattr(file_storage, "stream", file_storage)
    position = stream.tell() if hasattr(stream, "tell") else None
    if hasattr(stream, "seek"):
        stream.seek(0, os.SEEK_END)
        size_bytes = stream.tell()
        stream.seek(0)
    else:
        data = stream.read()
        size_bytes = len(data)
        from io import BytesIO
        stream = BytesIO(data)
    asset_type = str(system_asset_type or "").strip().lower()[:40]
    quota_exempt = bool(quota_exempt or asset_type == "avatar")
    if quota_exempt:
        disk_ok, _disk = storage_root_can_accept_bytes(storage_root, size_bytes)
        if not disk_ok:
            if position is not None and hasattr(stream, "seek"):
                stream.seek(position)
            return None, "Host 磁碟可用空間不足，請先清理檔案或擴充儲存空間"
    else:
        ok, msg = _check_quota(conn, actor, member_rule, size_bytes, storage_root=storage_root)
        if not ok:
            if position is not None and hasattr(stream, "seek"):
                stream.seek(position)
            return None, msg
    file_id_hint = uuid.uuid4().hex
    server_encrypted = is_server_encrypted_privacy_mode(privacy_mode)
    if is_e2ee_privacy_mode(privacy_mode):
        stored_name = f"{uuid.uuid4().hex}.e2ee"
    elif server_encrypted:
        stored_name = f"{uuid.uuid4().hex}.server_encrypt"
    else:
        stored_name = filename
    rel_path = f"users/{int(actor['id'])}/{file_id_hint}/{stored_name}"
    target = resolve_storage_path(storage_root, rel_path, create_parent=True)
    if server_encrypted:
        disk_ok, _disk = storage_root_can_accept_bytes(storage_root, int(size_bytes or 0) * 2)
        if not disk_ok:
            if position is not None and hasattr(stream, "seek"):
                stream.seek(position)
            return None, "Host 磁碟可用空間不足，無法暫存並加密此伺服器端加密檔案"
    plaintext_scan_path = target
    plaintext_temp_path = None
    if server_encrypted and server_file_fernet is None:
        raise ValueError("server_file_encryption_key is required for server_encrypted uploads")
    if server_encrypted:
        plaintext_temp_path = target.with_name(f".{target.name}.server-encrypt-plain-{uuid.uuid4().hex}.tmp")
        with open(plaintext_temp_path, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        plaintext_scan_path = plaintext_temp_path
    else:
        with open(plaintext_scan_path, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    try:
        result = create_uploaded_file_record(
            conn,
            owner_user_id=actor["id"],
            storage_path=rel_path,
            privacy_mode=privacy_mode,
            size_bytes=size_bytes,
            original_filename=filename,
            encrypted_metadata=encrypted_metadata,
            encrypted_file_key=encrypted_file_key,
            wrapped_by=wrapped_by,
            mime_type=getattr(file_storage, "mimetype", None),
            ciphertext_sha256=ciphertext_sha256,
            plaintext_sha256=None,
            encryption_algorithm="Fernet" if server_encrypted else encryption_algorithm,
            encryption_version=SERVER_ENCRYPTED_CHUNKED_VERSION if server_encrypted else encryption_version,
            nonce=nonce,
            client_scan_report=client_scan_report,
            system_asset_type=asset_type or None,
            user=actor,
            scan_now=False,
        )
        if scan_now and result.get("scan_status") == "pending":
            scan_result = scan_uploaded_file(
                conn,
                file_id=result["file_id"],
                file_path=plaintext_scan_path,
                filename=filename,
                declared_mime=getattr(file_storage, "mimetype", None),
            )
            result["scan_status"] = scan_result["scan_status"]
            result["risk_level"] = scan_result["risk_level"]
            result["scan_result"] = scan_result
        if server_encrypted:
            with open(plaintext_temp_path, "rb") as source:
                encrypted = encrypt_server_encrypted_chunked_stream(
                    source,
                    target,
                    server_file_fernet,
                    size_bytes=size_bytes,
                )
            conn.execute(
                """
                UPDATE uploaded_files
                SET ciphertext_sha256=?, encryption_algorithm=?, encryption_version=?, updated_at=?
                WHERE id=?
                """,
                (encrypted["ciphertext_sha256"], "Fernet", SERVER_ENCRYPTED_CHUNKED_VERSION, _now(), result["file_id"]),
            )
            result["ciphertext_sha256"] = encrypted["ciphertext_sha256"]
            result["encryption_algorithm"] = "Fernet"
            result["encryption_version"] = SERVER_ENCRYPTED_CHUNKED_VERSION
            result["server_encrypted_chunked"] = {
                "chunk_count": encrypted["chunk_count"],
                "chunk_size": encrypted["chunk_size"],
            }
        result["size_bytes"] = int(size_bytes or 0)
        result["quota_exempt"] = quota_exempt
        result["system_asset_type"] = asset_type
        return result, None
    finally:
        if plaintext_temp_path is not None:
            try:
                Path(plaintext_temp_path).unlink()
            except Exception:
                pass


def get_file_status(conn, *, actor, file_id):
    ensure_cloud_drive_attachment_schema(conn)
    row = _file_row(conn, file_id)
    if not row or row["deleted_at"]:
        return None, "找不到檔案或檔案已刪除"
    if not _actor_owns(actor, row["owner_user_id"]):
        allowed, _, _ = can_download_file(conn, actor=actor, file_id=file_id)
        if not allowed:
            return None, "沒有檔案權限"
    scans = conn.execute(
        """
        SELECT scanner_name, result, malware_name, scan_completed_at, created_at
        FROM file_scan_results
        WHERE file_id=?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (file_id,),
    ).fetchall()
    grants = conn.execute(
        """
        SELECT id, granted_to_user_id, granted_to_role, granted_to_group_id,
               context_type, context_id, can_download, can_preview, expires_at,
               revoked_at, created_at
        FROM file_access_grants
        WHERE file_id=?
        ORDER BY created_at DESC
        """,
        (file_id,),
    ).fetchall()
    keys = []
    if is_e2ee_file(row) and _actor_owns(actor, row["owner_user_id"]):
        keys = conn.execute(
            """
            SELECT id, recipient_user_id, wrapped_by, key_version, created_at, revoked_at
            FROM encrypted_file_keys
            WHERE file_id=?
            ORDER BY created_at DESC
            """,
            (file_id,),
        ).fetchall()
    data = serialize_file_row(row)
    data["scan_results"] = [dict(scan) for scan in scans]
    data["access_grants"] = [dict(grant) for grant in grants]
    data["encrypted_key_recipients"] = [dict(key) for key in keys]
    return data, None


def share_e2ee_file(
    conn,
    *,
    actor,
    file_id,
    recipient_user_id,
    encrypted_file_key,
    wrapped_by="recipient_public_key",
    context_type="dm",
    context_id=None,
):
    ensure_cloud_drive_attachment_schema(conn)
    row = _file_row(conn, file_id)
    if not row or row["deleted_at"]:
        return None, "找不到檔案或檔案已刪除"
    if int(row["owner_user_id"]) != int(actor["id"]):
        return None, "只能分享自己的 E2EE 檔案"
    if not is_e2ee_file(row):
        return None, "只有 E2EE 檔案需要加密金鑰分享"
    try:
        recipient_user_id = int(recipient_user_id)
    except Exception:
        return None, "recipient_user_id 錯誤"
    if recipient_user_id == int(actor["id"]):
        return None, "不需要分享給自己"
    if not str(encrypted_file_key or "").strip():
        return None, "缺少 encrypted_file_key"
    recipient = conn.execute("SELECT id FROM users WHERE id=?", (recipient_user_id,)).fetchone()
    if not recipient:
        return None, "找不到分享對象"
    allowed, target_msg = assert_can_target_user(
        conn,
        actor,
        recipient_user_id,
        context="cloud_drive_share",
    )
    if not allowed:
        return None, target_msg
    context_type = validate_context_type(context_type)
    context_id = _context_id(context_id or f"file-share:{file_id}")
    now = _now()
    key_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO encrypted_file_keys (
            id, file_id, recipient_user_id, encrypted_file_key, wrapped_by, key_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (key_id, file_id, recipient_user_id, str(encrypted_file_key), str(wrapped_by or "recipient_public_key"), 1, now),
    )
    grant_id = _insert_grant(
        conn,
        file_id=file_id,
        user_id=recipient_user_id,
        role=None,
        group_id=None,
        context_type=context_type,
        context_id=context_id,
        can_preview=True,
    )
    _create_file_access_log(
        conn,
        file_id=file_id,
        actor_user_id=actor["id"],
        action="share",
        result="allowed",
        reason=f"recipient_user_id={recipient_user_id}",
    )
    return {"key_id": key_id, "grant_id": grant_id, "recipient_user_id": recipient_user_id}, None


def revoke_e2ee_file_share(conn, *, actor, file_id, recipient_user_id):
    ensure_cloud_drive_attachment_schema(conn)
    row = _file_row(conn, file_id)
    if not row or row["deleted_at"]:
        return None, "找不到檔案或檔案已刪除"
    if not _actor_owns(actor, row["owner_user_id"]):
        return None, "只能撤銷自己的檔案分享"
    try:
        recipient_user_id = int(recipient_user_id)
    except Exception:
        return None, "recipient_user_id 錯誤"
    now = _now()
    key_cur = conn.execute(
        """
        UPDATE encrypted_file_keys
        SET revoked_at=?
        WHERE file_id=? AND recipient_user_id=? AND revoked_at IS NULL
        """,
        (now, file_id, recipient_user_id),
    )
    grant_cur = conn.execute(
        """
        UPDATE file_access_grants
        SET revoked_at=?
        WHERE file_id=? AND granted_to_user_id=? AND revoked_at IS NULL
        """,
        (now, file_id, recipient_user_id),
    )
    _create_file_access_log(
        conn,
        file_id=file_id,
        actor_user_id=actor["id"],
        action="revoke_share",
        result="allowed",
        reason=f"recipient_user_id={recipient_user_id}",
    )
    return {"revoked_keys": key_cur.rowcount, "revoked_grants": grant_cur.rowcount}, None


def _file_row(conn, file_id):
    return conn.execute("SELECT * FROM uploaded_files WHERE id=?", (file_id,)).fetchone()


def attach_existing_file(conn, *, actor, file_id, context_type, context_id, grant_user_ids=None, grant_role=None, grant_group_id=None, can_preview=False, allow_announcement=False):
    ensure_cloud_drive_attachment_schema(conn)
    context_type = validate_context_type(context_type)
    context_id = _context_id(context_id)
    row = _file_row(conn, file_id)
    if not row or row["deleted_at"]:
        return None, "找不到檔案或檔案已刪除"
    if not _actor_owns(actor, row["owner_user_id"]):
        return None, "只能附加自己的雲端硬碟檔案"
    if context_type == "announcement" and not allow_announcement:
        return None, "公告附件必須走 root 審核流程"
    now = _now()
    ref_id = uuid.uuid4().hex
    snapshot = {
        "scan_status": row["scan_status"],
        "risk_level": row["risk_level"],
        "privacy_mode": row["privacy_mode"],
    }
    conn.execute(
        """
        INSERT INTO cloud_file_refs (
            id, file_id, owner_user_id, context_type, context_id, attached_by,
            created_at, permission_snapshot_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ref_id, file_id, row["owner_user_id"], context_type, context_id, actor["id"], now, json.dumps(snapshot, ensure_ascii=False)),
    )
    created_grants = []
    for uid in sorted({int(x) for x in (grant_user_ids or []) if str(x).strip()}):
        created_grants.append(_insert_grant(conn, file_id=file_id, user_id=uid, role=None, group_id=None, context_type=context_type, context_id=context_id, can_preview=can_preview))
    if grant_role:
        created_grants.append(_insert_grant(conn, file_id=file_id, user_id=None, role=str(grant_role), group_id=None, context_type=context_type, context_id=context_id, can_preview=can_preview))
    if grant_group_id:
        created_grants.append(_insert_grant(conn, file_id=file_id, user_id=None, role=None, group_id=str(grant_group_id), context_type=context_type, context_id=context_id, can_preview=can_preview))
    return {"ref_id": ref_id, "grants": created_grants}, None


def _insert_grant(conn, *, file_id, user_id=None, role=None, group_id=None, context_type, context_id, can_preview=False):
    grant_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO file_access_grants (
            id, file_id, granted_to_user_id, granted_to_role, granted_to_group_id,
            context_type, context_id, can_download, can_preview, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (grant_id, file_id, user_id, role, group_id, context_type, context_id, 1 if can_preview else 0, _now()),
    )
    return grant_id


def can_download_file(conn, *, actor, file_id, action="download"):
    ensure_cloud_drive_attachment_schema(conn)
    ensure_storage_album_schema(conn)
    row = _file_row(conn, file_id)
    if not row:
        return False, "not_found", None
    if row["deleted_at"]:
        return False, "deleted", row
    trashed = conn.execute(
        """
        SELECT 1 FROM storage_files
        WHERE file_id=? AND owner_user_id=? AND deleted_at IS NULL
              AND is_trashed=1 AND trash_source='cloud_drive_delete'
        LIMIT 1
        """,
        (file_id, int(row["owner_user_id"])),
    ).fetchone()
    if trashed:
        return False, "deleted", row
    if _actor_owns(actor, row["owner_user_id"]):
        return _scan_allows_download(conn, row), "owner_or_admin", row
    where, params = _active_grant_where(actor, action)
    grant = conn.execute(
        f"SELECT id FROM file_access_grants WHERE file_id=? AND {where} LIMIT 1",
        (file_id, *params),
    ).fetchone()
    if not grant:
        return False, "no_grant", row
    allowed = _scan_allows_download(conn, row)
    return allowed, "grant" if allowed else "blocked_by_scan_policy", row


def _scan_allows_download(conn, row):
    policy = get_cloud_drive_security_policy(conn)
    if not policy["block_unclean_downloads"]:
        return True
    if is_e2ee_file(row):
        return True
    return row["scan_status"] in {"clean", "not_required"}


def soft_delete_cloud_file(conn, *, actor, file_id):
    row = _file_row(conn, file_id)
    if not row or row["deleted_at"]:
        return False, "找不到檔案或檔案已刪除"
    if not _actor_owns(actor, row["owner_user_id"]):
        return False, "只能刪除自己的檔案"
    conn.execute("UPDATE uploaded_files SET deleted_at=?, updated_at=? WHERE id=?", (_now(), _now(), file_id))
    return True, ""


def delete_cloud_file_permanently(conn, *, actor, file_id, storage_root):
    from services.storage.catalog import sync_user_storage_summary

    ensure_cloud_drive_attachment_schema(conn)
    ensure_storage_album_schema(conn)
    row = _file_row(conn, file_id)
    if not row or row["deleted_at"]:
        return None, "找不到檔案或檔案已刪除"
    if not _actor_owns(actor, row["owner_user_id"]):
        return None, "只能刪除自己的檔案"
    if _file_is_current_avatar(conn, row["id"]):
        return None, "頭貼檔案由個人主頁管理；更換頭貼後系統會自動清除舊檔"
    now = _now()
    owner_user_id = int(row["owner_user_id"])
    storage_path = resolve_file_storage_path(storage_root, row)
    removed_physical = False
    if storage_path.exists() and storage_path.is_file():
        storage_path.unlink()
        removed_physical = True
        try:
            parent = storage_path.parent
            while parent != Path(storage_root).resolve() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except Exception:
            pass
    conn.execute(
        "UPDATE uploaded_files SET deleted_at=?, updated_at=? WHERE id=? AND owner_user_id=?",
        (now, now, row["id"], owner_user_id),
    )
    conn.execute(
        "UPDATE storage_files SET deleted_at=?, updated_at=? WHERE file_id=? AND owner_user_id=? AND deleted_at IS NULL",
        (now, now, row["id"], owner_user_id),
    )
    conn.execute("UPDATE album_files SET deleted_at=? WHERE file_id=? AND deleted_at IS NULL", (now, row["id"]))
    conn.execute("UPDATE storage_share_links SET revoked_at=? WHERE file_id=? AND revoked_at IS NULL", (now, row["id"]))
    conn.execute("UPDATE file_access_grants SET revoked_at=? WHERE file_id=? AND revoked_at IS NULL", (now, row["id"]))
    summary = sync_user_storage_summary(
        conn,
        owner_user_id,
        actor_user_id=actor["id"],
        source="cloud_drive_delete",
        reason="cloud_file_permanently_deleted",
    )
    return {
        "file_id": row["id"],
        "removed_physical_file": removed_physical,
        "storage": summary,
    }, None


def delete_avatar_file_if_unreferenced(conn, *, actor, file_id, storage_root):
    file_id = str(file_id or "").strip()
    if not file_id:
        return {"deleted": False, "reason": "empty_file_id"}
    ensure_cloud_drive_attachment_schema(conn)
    row = _file_row(conn, file_id)
    if not row or row["deleted_at"]:
        return {"deleted": False, "reason": "missing_or_deleted", "file_id": file_id}
    if _file_is_current_avatar(conn, file_id):
        return {"deleted": False, "reason": "still_current_avatar", "file_id": file_id}
    if not _file_is_avatar_asset(conn, file_id):
        return {"deleted": False, "reason": "not_avatar_asset", "file_id": file_id}
    result, msg = delete_cloud_file_permanently(conn, actor=actor, file_id=file_id, storage_root=storage_root)
    if msg:
        return {"deleted": False, "reason": msg, "file_id": file_id}
    return {"deleted": True, "file_id": file_id, "result": result}


def create_announcement_attachment_request(conn, *, actor, file_id, announcement_id=None, reason=""):
    ensure_cloud_drive_attachment_schema(conn)
    if not is_manager_or_root(actor):
        return None, "只有管理員以上可提出公告附件請求"
    row = _file_row(conn, file_id)
    if not row or row["deleted_at"]:
        return None, "找不到檔案或檔案已刪除"
    if not _actor_owns(actor, row["owner_user_id"]):
        return None, "只能提交自己的檔案作為公告附件"
    if is_e2ee_file(row):
        return None, "E2EE 檔案不可作為公告附件"
    req_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO announcement_attachment_requests (
            id, file_id, requested_by, announcement_id, status, reason, created_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (req_id, file_id, actor["id"], announcement_id, str(reason or "")[:500], _now()),
    )
    return {"id": req_id}, None


def review_announcement_attachment_request(conn, *, actor, request_id, action, reason=""):
    ensure_cloud_drive_attachment_schema(conn)
    if _actor_role(actor) != "super_admin":
        return None, "只有 root 可審核公告附件"
    if action not in {"approve", "reject"}:
        return None, "審核動作錯誤"
    req = conn.execute("SELECT * FROM announcement_attachment_requests WHERE id=?", (request_id,)).fetchone()
    if not req:
        return None, "找不到公告附件請求"
    if req["status"] != "pending":
        return None, "此請求已審核"
    status = "approved" if action == "approve" else "rejected"
    now = _now()
    conn.execute(
        "UPDATE announcement_attachment_requests SET status=?, reviewed_by=?, reviewed_at=?, reason=? WHERE id=?",
        (status, actor["id"], now, str(reason or req["reason"] or "")[:500], request_id),
    )
    if status == "approved":
        # Approved announcement attachments become root-owned management files so
        # future quota/accounting follows the announcement ownership rule.
        conn.execute(
            "UPDATE uploaded_files SET owner_user_id=?, updated_at=? WHERE id=?",
            (actor["id"], now, req["file_id"]),
        )
        attach_existing_file(
            conn,
            actor=actor,
            file_id=req["file_id"],
            context_type="announcement",
            context_id=str(req["announcement_id"] or request_id),
            grant_role="user",
            can_preview=True,
            allow_announcement=True,
        )
    return {"id": request_id, "status": status}, None


def resolve_file_storage_path(storage_root, row):
    return resolve_storage_path(storage_root, row["storage_path"], create_parent=False)
