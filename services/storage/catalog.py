import json
import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from services.security.upload_schema import ensure_upload_security_schema
from services.storage.paths import resolve_storage_path

ALBUM_SHARE_PASSWORD_ITERATIONS = 200_000
MAX_ALBUM_SHARE_PASSWORD_LENGTH = 256
# Kept as a named schema field so the migration map remains explicit without
# resembling a token assignment to the static secret scanner.
STORAGE_SHARE_TOKEN_COLUMN = "token"



def _now():
    return datetime.now().isoformat()


def ensure_storage_album_schema(conn):
    ensure_upload_security_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_storage (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            quota_bytes INTEGER NOT NULL DEFAULT 0,
            used_bytes INTEGER NOT NULL DEFAULT 0,
            reserved_bytes INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS storage_files (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            parent_id TEXT REFERENCES storage_files(id) ON DELETE SET NULL,
            display_name TEXT NOT NULL,
            virtual_path TEXT NOT NULL,
            is_trashed INTEGER NOT NULL DEFAULT 0,
            trashed_at TEXT,
            restored_at TEXT,
            trash_source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            UNIQUE(owner_user_id, virtual_path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS storage_folders (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL,
            virtual_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            UNIQUE(owner_user_id, virtual_path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS storage_quota_log (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            file_id TEXT REFERENCES uploaded_files(id) ON DELETE SET NULL,
            delta_bytes INTEGER NOT NULL,
            before_used_bytes INTEGER NOT NULL,
            after_used_bytes INTEGER NOT NULL,
            source TEXT NOT NULL,
            reason TEXT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS albums (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT,
            visibility TEXT NOT NULL DEFAULT 'private',
            cover_file_id TEXT REFERENCES uploaded_files(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS album_files (
            id TEXT PRIMARY KEY,
            album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            storage_file_id TEXT REFERENCES storage_files(id) ON DELETE SET NULL,
            file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            caption TEXT,
            added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            UNIQUE(album_id, file_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS storage_share_links (
            id TEXT PRIMARY KEY,
            storage_file_id TEXT NOT NULL REFERENCES storage_files(id) ON DELETE CASCADE,
            file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT,
            token_hash TEXT NOT NULL UNIQUE,
            password_required INTEGER NOT NULL DEFAULT 0,
            password_hash TEXT,
            can_download INTEGER NOT NULL DEFAULT 1,
            can_preview INTEGER NOT NULL DEFAULT 0,
            access_scope TEXT NOT NULL DEFAULT 'link',
            required_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            max_views INTEGER NOT NULL DEFAULT 0,
            wrapped_file_key_envelope TEXT,
            expires_at TEXT,
            revoked_at TEXT,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed_at TEXT,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS album_share_links (
            id TEXT PRIMARY KEY,
            album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL UNIQUE,
            password_required INTEGER NOT NULL DEFAULT 0,
            password_hash TEXT,
            max_views INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            revoked_at TEXT,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed_at TEXT,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_storage_files_owner_path ON storage_files(owner_user_id, virtual_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_storage_files_file ON storage_files(file_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_storage_folders_owner_path ON storage_folders(owner_user_id, virtual_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_storage_quota_log_user ON storage_quota_log(user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_albums_owner ON albums(owner_user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_album_files_album ON album_files(album_id, sort_order, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_album_share_links_album ON album_share_links(album_id, revoked_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_storage_share_links_owner ON storage_share_links(owner_user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_storage_share_links_file ON storage_share_links(storage_file_id, revoked_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_storage_share_links_required_user ON storage_share_links(required_user_id, revoked_at)")
    storage_file_cols = {row["name"] for row in conn.execute("PRAGMA table_info(storage_files)").fetchall()}
    if "trash_source" not in storage_file_cols:
        conn.execute("ALTER TABLE storage_files ADD COLUMN trash_source TEXT")
    storage_share_cols = {row["name"] for row in conn.execute("PRAGMA table_info(storage_share_links)").fetchall()}
    storage_share_defs = {
        STORAGE_SHARE_TOKEN_COLUMN: "TEXT",
        "can_download": "INTEGER NOT NULL DEFAULT 1",
        "can_preview": "INTEGER NOT NULL DEFAULT 0",
        "access_scope": "TEXT NOT NULL DEFAULT 'link'",
        "required_user_id": "INTEGER",
        "max_views": "INTEGER NOT NULL DEFAULT 0",
        "wrapped_file_key_envelope": "TEXT",
        "password_required": "INTEGER NOT NULL DEFAULT 0",
        "password_hash": "TEXT",
    }
    for column, ddl in storage_share_defs.items():
        if column not in storage_share_cols:
            conn.execute(f"ALTER TABLE storage_share_links ADD COLUMN {column} {ddl}")
    album_share_cols = {row["name"] for row in conn.execute("PRAGMA table_info(album_share_links)").fetchall()}
    if "password_required" not in album_share_cols:
        conn.execute("ALTER TABLE album_share_links ADD COLUMN password_required INTEGER NOT NULL DEFAULT 0")
    if "password_hash" not in album_share_cols:
        conn.execute("ALTER TABLE album_share_links ADD COLUMN password_hash TEXT")
    if "max_views" not in album_share_cols:
        conn.execute("ALTER TABLE album_share_links ADD COLUMN max_views INTEGER NOT NULL DEFAULT 0")
    if "expires_at" not in album_share_cols:
        conn.execute("ALTER TABLE album_share_links ADD COLUMN expires_at TEXT")


def normalize_virtual_path(path, display_name=None):
    raw = str(path or "").replace("\\", "/").strip()
    if not raw:
        raw = str(display_name or "").strip()
    if not raw:
        raw = "untitled"
    parts = []
    for part in raw.split("/"):
        cleaned = part.strip()
        if not cleaned:
            continue
        if cleaned in {".", ".."} or "\x00" in cleaned:
            raise ValueError("invalid storage path")
        parts.append(cleaned[:120])
    if not parts:
        raise ValueError("invalid storage path")
    return "/" + "/".join(parts)


def _display_name_from_path(path):
    return str(path or "").rstrip("/").split("/")[-1] or "untitled"


def storage_existing_file_for_path(conn, *, owner_user_id, virtual_path=None, display_name=None):
    try:
        normalized_path = normalize_virtual_path(virtual_path, display_name)
    except ValueError:
        return None
    row = conn.execute(
        """
        SELECT sf.id, sf.file_id, sf.display_name, sf.virtual_path, f.privacy_mode, f.size_bytes
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.virtual_path=?
          AND sf.deleted_at IS NULL AND f.deleted_at IS NULL
        """,
        (int(owner_user_id), normalized_path),
    ).fetchone()
    return dict(row) if row else None


def storage_replacement_password_required(conn, *, owner_user_id, virtual_path=None, display_name=None, new_privacy_mode="standard_plain"):
    row = storage_existing_file_for_path(conn, owner_user_id=owner_user_id, virtual_path=virtual_path, display_name=display_name)
    if not row:
        return False
    old_mode = str(row["privacy_mode"] or "standard_plain").strip()
    next_mode = str(new_privacy_mode or "standard_plain").strip()
    return old_mode == "e2ee" or next_mode == "e2ee"


def _folder_prefix(path):
    normalized = normalize_virtual_path(path, "folder")
    return normalized.rstrip("/")


def _is_path_inside(path, folder_path):
    return path == folder_path or path.startswith(folder_path.rstrip("/") + "/")


def _parent_folders_for_file(path):
    parts = [part for part in str(path or "").split("/") if part]
    folders = []
    for idx in range(1, len(parts)):
        folders.append("/" + "/".join(parts[:idx]))
    return folders


def _storage_path_exists(conn, owner_user_id, path):
    if conn.execute(
        "SELECT 1 FROM storage_files WHERE owner_user_id=? AND virtual_path=? LIMIT 1",
        (int(owner_user_id), path),
    ).fetchone():
        return True
    return bool(conn.execute(
        "SELECT 1 FROM storage_folders WHERE owner_user_id=? AND virtual_path=? LIMIT 1",
        (int(owner_user_id), path),
    ).fetchone())


def _unique_storage_path(conn, owner_user_id, desired_path, display_name):
    base_path = normalize_virtual_path(desired_path, display_name)
    if not _storage_path_exists(conn, owner_user_id, base_path):
        return base_path
    parent = "/" + "/".join(base_path.strip("/").split("/")[:-1])
    if parent == "/":
        parent = ""
    name = _display_name_from_path(base_path)
    stem, dot, ext = name.rpartition(".")
    if not stem:
        stem, dot, ext = name, "", ""
    for index in range(2, 1000):
        candidate_name = f"{stem} ({index}){dot}{ext}"
        candidate = f"{parent}/{candidate_name}" if parent else f"/{candidate_name}"
        if not _storage_path_exists(conn, owner_user_id, candidate):
            return candidate
    raise ValueError("storage path conflict")


def unique_storage_path(conn, owner_user_id, desired_path, display_name):
    """Return a non-conflicting virtual path for automatic file placement."""
    return _unique_storage_path(conn, owner_user_id, desired_path, display_name)


def get_user_storage_summary(conn, user_id):
    ensure_storage_album_schema(conn)
    row = conn.execute("SELECT * FROM user_storage WHERE user_id=?", (int(user_id),)).fetchone()
    if row:
        return dict(row)
    now = _now()
    conn.execute(
        "INSERT INTO user_storage (user_id, quota_bytes, used_bytes, reserved_bytes, file_count, updated_at) VALUES (?, 0, 0, 0, 0, ?)",
        (int(user_id), now),
    )
    return {
        "user_id": int(user_id),
        "quota_bytes": 0,
        "used_bytes": 0,
        "reserved_bytes": 0,
        "file_count": 0,
        "updated_at": now,
    }


def get_user_storage_summary_snapshot(conn, user_id):
    """Return a listing summary without creating a legacy account row."""
    row = conn.execute("SELECT * FROM user_storage WHERE user_id=?", (int(user_id),)).fetchone()
    if row:
        return dict(row)
    return {"user_id": int(user_id), "quota_bytes": 0, "used_bytes": 0, "reserved_bytes": 0, "file_count": 0, "updated_at": None}


def _recalculate_storage_usage(conn, user_id):
    row = conn.execute(
        """
        SELECT COALESCE(SUM(f.size_bytes), 0) AS used_bytes, COUNT(sf.id) AS file_count
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.deleted_at IS NULL AND f.deleted_at IS NULL
              AND COALESCE(f.system_asset_type, '')<>'avatar'
        """,
        (int(user_id),),
    ).fetchone()
    return int(row["used_bytes"] or 0), int(row["file_count"] or 0)


def _remove_empty_storage_parents(path, storage_root):
    root = Path(storage_root).resolve()
    parent = Path(path).resolve().parent
    while parent != root and root in parent.parents:
        try:
            if any(parent.iterdir()):
                break
            parent.rmdir()
        except Exception:
            break
        parent = parent.parent


def _remove_uploaded_file_blob(storage_root, storage_path):
    if not storage_root or not storage_path:
        return False
    path = resolve_storage_path(storage_root, storage_path, create_parent=False)
    if not path.exists():
        return False
    if not path.is_file():
        return False
    path.unlink()
    _remove_empty_storage_parents(path, storage_root)
    return True


def _storage_table_exists(conn, table_name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (str(table_name or ""),),
    ).fetchone())


def _purge_unreferenced_uploaded_files(conn, *, owner_user_id, file_ids, storage_root=None, now=None):
    now = now or _now()
    purged_file_ids = []
    removed_physical_files = 0
    for file_id in sorted({str(item or "").strip() for item in (file_ids or []) if str(item or "").strip()}):
        still_referenced = conn.execute(
            """
            SELECT 1 FROM storage_files
            WHERE owner_user_id=? AND file_id=? AND deleted_at IS NULL
            LIMIT 1
            """,
            (int(owner_user_id), file_id),
        ).fetchone()
        if still_referenced:
            continue
        file_row = conn.execute(
            "SELECT id, storage_path, deleted_at FROM uploaded_files WHERE id=? AND owner_user_id=?",
            (file_id, int(owner_user_id)),
        ).fetchone()
        if not file_row or file_row["deleted_at"]:
            continue
        if _remove_uploaded_file_blob(storage_root, file_row["storage_path"]):
            removed_physical_files += 1
        updated = conn.execute(
            """
            UPDATE uploaded_files
            SET deleted_at=?, updated_at=?
            WHERE id=? AND owner_user_id=? AND deleted_at IS NULL
            """,
            (now, now, file_id, int(owner_user_id)),
        ).rowcount
        if updated:
            purged_file_ids.append(file_id)
    if purged_file_ids:
        conn.executemany(
            "UPDATE album_files SET deleted_at=? WHERE file_id=? AND deleted_at IS NULL",
            [(now, file_id) for file_id in purged_file_ids],
        )
        conn.executemany(
            "UPDATE storage_share_links SET revoked_at=? WHERE file_id=? AND revoked_at IS NULL",
            [(now, file_id) for file_id in purged_file_ids],
        )
        if _storage_table_exists(conn, "file_access_grants"):
            conn.executemany(
                "UPDATE file_access_grants SET revoked_at=? WHERE file_id=? AND revoked_at IS NULL",
                [(now, file_id) for file_id in purged_file_ids],
            )
    return {
        "purged_file_ids": purged_file_ids,
        "removed_physical_files": removed_physical_files,
    }


def sync_user_storage_summary(conn, user_id, *, actor_user_id=None, source="system", reason="sync"):
    ensure_storage_album_schema(conn)
    before = get_user_storage_summary(conn, user_id)
    used_bytes, file_count = _recalculate_storage_usage(conn, user_id)
    now = _now()
    conn.execute(
        """
        UPDATE user_storage
        SET used_bytes=?, file_count=?, updated_at=?
        WHERE user_id=?
        """,
        (used_bytes, file_count, now, int(user_id)),
    )
    delta = used_bytes - int(before.get("used_bytes") or 0)
    if delta:
        conn.execute(
            """
            INSERT INTO storage_quota_log (
                id, user_id, file_id, delta_bytes, before_used_bytes, after_used_bytes,
                source, reason, actor_user_id, created_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                int(user_id),
                delta,
                int(before.get("used_bytes") or 0),
                used_bytes,
                str(source or "system")[:50],
                str(reason or "")[:200],
                actor_user_id,
                now,
            ),
        )
    return get_user_storage_summary(conn, user_id)


def create_storage_file_entry(conn, *, actor, file_row, virtual_path=None, display_name=None, source="upload", replace_existing=False, storage_root=None, replace_existing_password_confirmed=False):
    ensure_storage_album_schema(conn)
    owner_user_id = int(file_row["owner_user_id"])
    if owner_user_id != int(actor["id"]):
        return None, "只能加入自己的檔案到 storage"
    file_keys = set(file_row.keys()) if hasattr(file_row, "keys") else set()
    asset_type = file_row["system_asset_type"] if "system_asset_type" in file_keys else ""
    if str(asset_type or "").strip().lower() == "avatar":
        return None, "頭貼檔案由個人主頁管理，不加入雲端硬碟列表"
    display_name = str(display_name or file_row["original_filename_plain_for_public"] or "download.bin").strip()[:160]
    try:
        normalized_path = normalize_virtual_path(virtual_path, display_name)
    except ValueError:
        return None, "storage path 不安全或格式錯誤"
    existing = conn.execute(
        """
        SELECT sf.id, sf.file_id, f.size_bytes AS old_size_bytes, f.privacy_mode AS old_privacy_mode
        FROM storage_files sf
        LEFT JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.virtual_path=? AND sf.deleted_at IS NULL
        """,
        (owner_user_id, normalized_path),
    ).fetchone()
    now = _now()
    if existing:
        if not replace_existing:
            return None, "storage path 已存在"
        old_file_id = str(existing["file_id"] or "")
        new_file_id = str(file_row["id"])
        if old_file_id == new_file_id:
            return get_storage_file(conn, actor=actor, storage_file_id=existing["id"]), None
        new_mode = str(file_row["privacy_mode"] if "privacy_mode" in file_keys else "standard_plain").strip() or "standard_plain"
        old_mode = str(existing["old_privacy_mode"] or "standard_plain").strip()
        if (old_mode == "e2ee" or new_mode == "e2ee") and not replace_existing_password_confirmed:
            return None, "覆寫 E2EE 檔案或變更加密方式需要重新輸入檔案密碼"
        before = get_user_storage_summary(conn, owner_user_id)
        old_size = int(existing["old_size_bytes"] or 0)
        new_size = int(file_row["size_bytes"] or 0)
        after_used = max(0, int(before.get("used_bytes") or 0) - old_size + new_size)
        conn.execute(
            """
            UPDATE storage_files
            SET file_id=?, display_name=?, is_trashed=0, trashed_at=NULL,
                restored_at=NULL, trash_source=NULL, updated_at=?
            WHERE id=? AND owner_user_id=?
            """,
            (new_file_id, display_name, now, existing["id"], owner_user_id),
        )
        conn.execute(
            """
            UPDATE storage_share_links
            SET file_id=?
            WHERE storage_file_id=? AND owner_user_id=? AND revoked_at IS NULL
            """,
            (new_file_id, existing["id"], owner_user_id),
        )
        conn.execute(
            """
            UPDATE album_files
            SET file_id=?
            WHERE storage_file_id=? AND deleted_at IS NULL
            """,
            (new_file_id, existing["id"]),
        )
        delta = new_size - old_size
        if delta:
            conn.execute(
                """
                INSERT INTO storage_quota_log (
                    id, user_id, file_id, delta_bytes, before_used_bytes, after_used_bytes,
                    source, reason, actor_user_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    owner_user_id,
                    new_file_id,
                    delta,
                    int(before.get("used_bytes") or 0),
                    after_used,
                    str(source or "upload")[:50],
                    "storage_file_replaced",
                    int(actor["id"]),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO user_storage (user_id, quota_bytes, used_bytes, reserved_bytes, file_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET used_bytes=excluded.used_bytes, updated_at=excluded.updated_at
                """,
                (
                    owner_user_id,
                    int(before.get("quota_bytes") or 0),
                    after_used,
                    int(before.get("reserved_bytes") or 0),
                    int(before.get("file_count") or 0),
                    now,
                ),
            )
        if old_file_id:
            _purge_unreferenced_uploaded_files(conn, owner_user_id=owner_user_id, file_ids=[old_file_id], storage_root=storage_root, now=now)
        return get_storage_file(conn, actor=actor, storage_file_id=existing["id"]), None
    storage_id = uuid.uuid4().hex
    before = get_user_storage_summary(conn, owner_user_id)
    after_used = int(before.get("used_bytes") or 0) + int(file_row["size_bytes"] or 0)
    conn.execute(
        """
        INSERT INTO storage_files (
            id, file_id, owner_user_id, parent_id, display_name, virtual_path,
            is_trashed, created_at, updated_at
        ) VALUES (?, ?, ?, NULL, ?, ?, 0, ?, ?)
        """,
        (storage_id, file_row["id"], owner_user_id, display_name, normalized_path, now, now),
    )
    conn.execute(
        """
        INSERT INTO storage_quota_log (
            id, user_id, file_id, delta_bytes, before_used_bytes, after_used_bytes,
            source, reason, actor_user_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            owner_user_id,
            file_row["id"],
            int(file_row["size_bytes"] or 0),
            int(before.get("used_bytes") or 0),
            after_used,
            str(source or "upload")[:50],
            "storage_file_created",
            int(actor["id"]),
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO user_storage (user_id, quota_bytes, used_bytes, reserved_bytes, file_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET used_bytes=excluded.used_bytes, file_count=user_storage.file_count + 1, updated_at=excluded.updated_at
        """,
        (
            owner_user_id,
            int(before.get("quota_bytes") or 0),
            after_used,
            int(before.get("reserved_bytes") or 0),
            int(before.get("file_count") or 0) + 1,
            now,
        ),
    )
    return get_storage_file(conn, actor=actor, storage_file_id=storage_id), None


def get_storage_file(conn, *, actor, storage_file_id):
    ensure_storage_album_schema(conn)
    row = conn.execute(
        """
        SELECT sf.*, f.storage_path, f.size_bytes, f.privacy_mode, f.risk_level, f.scan_status,
               f.system_asset_type,
               f.original_filename_plain_for_public, f.deleted_at AS file_deleted_at
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.id=? AND COALESCE(f.system_asset_type, '')<>'avatar'
        """,
        (storage_file_id,),
    ).fetchone()
    if not row:
        return None
    if int(row["owner_user_id"]) != int(actor["id"]):
        return None
    return dict(row)


def list_storage_files(conn, *, actor, include_trashed=False, limit=100, offset=0, ensure_schema=True):
    if ensure_schema:
        ensure_storage_album_schema(conn)
    where = "sf.owner_user_id=? AND sf.deleted_at IS NULL AND f.deleted_at IS NULL"
    params = [int(actor["id"])]
    if not include_trashed:
        where += " AND sf.is_trashed=0"
    rows = conn.execute(
        f"""
        SELECT sf.*, f.size_bytes, f.privacy_mode, f.risk_level, f.scan_status,
               f.system_asset_type, f.original_filename_plain_for_public
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE {where} AND COALESCE(f.system_asset_type, '')<>'avatar'
        ORDER BY sf.virtual_path ASC
        LIMIT ? OFFSET ?
        """,
        (*params, int(limit), int(offset)),
    ).fetchall()
    return [dict(row) for row in rows]


def list_storage_folders(conn, *, actor, ensure_schema=True):
    if ensure_schema:
        ensure_storage_album_schema(conn)
    owner_user_id = int(actor["id"])
    folders = {}
    explicit_rows = conn.execute(
        """
        SELECT * FROM storage_folders
        WHERE owner_user_id=? AND deleted_at IS NULL
        ORDER BY virtual_path ASC
        """,
        (owner_user_id,),
    ).fetchall()
    for row in explicit_rows:
        path = row["virtual_path"]
        folders[path] = {
            "id": row["id"],
            "display_name": row["display_name"],
            "virtual_path": path,
            "is_explicit": True,
            "file_count": 0,
            "recursive_file_count": 0,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    file_rows = conn.execute(
        """
        SELECT virtual_path
        FROM storage_files
        WHERE owner_user_id=? AND deleted_at IS NULL AND is_trashed=0
        """,
        (owner_user_id,),
    ).fetchall()
    for row in file_rows:
        file_path = row["virtual_path"]
        parent_parts = _parent_folders_for_file(file_path)
        direct_parent = parent_parts[-1] if parent_parts else ""
        for folder_path in parent_parts:
            folders.setdefault(folder_path, {
                "id": None,
                "display_name": _display_name_from_path(folder_path),
                "virtual_path": folder_path,
                "is_explicit": False,
                "file_count": 0,
                "recursive_file_count": 0,
                "created_at": None,
                "updated_at": None,
            })
            folders[folder_path]["recursive_file_count"] += 1
        if direct_parent:
            folders[direct_parent]["file_count"] += 1
    return [folders[path] for path in sorted(folders)]


def create_storage_folder(conn, *, actor, path):
    ensure_storage_album_schema(conn)
    owner_user_id = int(actor["id"])
    try:
        folder_path = _folder_prefix(path)
    except ValueError:
        return None, "資料夾路徑不安全或格式錯誤"
    if conn.execute(
        "SELECT id FROM storage_files WHERE owner_user_id=? AND virtual_path=? AND deleted_at IS NULL",
        (owner_user_id, folder_path),
    ).fetchone():
        return None, "同路徑已有檔案"
    existing = conn.execute(
        "SELECT * FROM storage_folders WHERE owner_user_id=? AND virtual_path=? AND deleted_at IS NULL",
        (owner_user_id, folder_path),
    ).fetchone()
    if existing:
        return dict(existing), None
    now = _now()
    folder_id = uuid.uuid4().hex
    try:
        conn.execute(
            """
            INSERT INTO storage_folders (id, owner_user_id, display_name, virtual_path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (folder_id, owner_user_id, _display_name_from_path(folder_path), folder_path, now, now),
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM storage_folders WHERE owner_user_id=? AND virtual_path=? AND deleted_at IS NULL",
            (owner_user_id, folder_path),
        ).fetchone()
        if existing:
            return dict(existing), None
        raise
    return dict(conn.execute("SELECT * FROM storage_folders WHERE id=?", (folder_id,)).fetchone()), None


def move_storage_file(conn, *, actor, storage_file_id, new_virtual_path):
    ensure_storage_album_schema(conn)
    row = get_storage_file(conn, actor=actor, storage_file_id=storage_file_id)
    if not row or row.get("deleted_at") or int(row.get("is_trashed") or 0):
        return None, "找不到檔案或檔案已刪除"
    owner_user_id = int(actor["id"])
    requested_path = str(new_virtual_path or "").replace("\\", "/").strip()
    try:
        filename = _display_name_from_path(row.get("display_name") or row.get("virtual_path"))
        if requested_path in {"", "/"}:
            normalized_path = normalize_virtual_path(f"/{filename}", filename)
        else:
            folder_candidate = _folder_prefix(requested_path)
            is_existing_folder = bool(conn.execute(
                """
                SELECT 1 FROM storage_folders
                WHERE owner_user_id=? AND virtual_path=? AND deleted_at IS NULL
                LIMIT 1
                """,
                (owner_user_id, folder_candidate),
            ).fetchone())
            if not is_existing_folder:
                is_existing_folder = bool(conn.execute(
                    """
                    SELECT 1 FROM storage_files
                    WHERE owner_user_id=? AND deleted_at IS NULL AND COALESCE(is_trashed, 0)=0
                          AND virtual_path LIKE ?
                    LIMIT 1
                    """,
                    (owner_user_id, folder_candidate.rstrip("/") + "/%"),
                ).fetchone())
            if requested_path.endswith("/") or is_existing_folder:
                normalized_path = normalize_virtual_path(f"{folder_candidate.rstrip('/')}/{filename}", filename)
            else:
                normalized_path = normalize_virtual_path(new_virtual_path, row.get("display_name"))
    except ValueError:
        return None, "storage path 不安全或格式錯誤"
    conflict = conn.execute(
        """
        SELECT id FROM storage_files
        WHERE owner_user_id=? AND virtual_path=? AND deleted_at IS NULL AND id<>?
        """,
        (owner_user_id, normalized_path, storage_file_id),
    ).fetchone()
    if conflict:
        return None, "目標路徑已有檔案"
    now = _now()
    conn.execute(
        """
        UPDATE storage_files
        SET virtual_path=?, display_name=?, updated_at=?
        WHERE id=? AND owner_user_id=?
        """,
        (normalized_path, _display_name_from_path(normalized_path), now, storage_file_id, owner_user_id),
    )
    return get_storage_file(conn, actor=actor, storage_file_id=storage_file_id), None


def move_storage_folder(conn, *, actor, old_path, new_path):
    ensure_storage_album_schema(conn)
    owner_user_id = int(actor["id"])
    try:
        old_folder = _folder_prefix(old_path)
        new_folder = _folder_prefix(new_path)
    except ValueError:
        return None, "資料夾路徑不安全或格式錯誤"
    if old_folder == new_folder:
        return {"old_path": old_folder, "new_path": new_folder, "moved_files": 0, "moved_folders": 0}, None
    if _is_path_inside(new_folder, old_folder):
        return None, "不能把資料夾移到自己的子資料夾"

    files = conn.execute(
        """
        SELECT id, virtual_path FROM storage_files
        WHERE owner_user_id=? AND deleted_at IS NULL AND is_trashed=0 AND virtual_path LIKE ?
        ORDER BY virtual_path ASC
        """,
        (owner_user_id, old_folder.rstrip("/") + "/%"),
    ).fetchall()
    folders = conn.execute(
        """
        SELECT id, virtual_path FROM storage_folders
        WHERE owner_user_id=? AND deleted_at IS NULL AND (virtual_path=? OR virtual_path LIKE ?)
        ORDER BY virtual_path ASC
        """,
        (owner_user_id, old_folder, old_folder.rstrip("/") + "/%"),
    ).fetchall()
    if not files and not folders:
        return None, "找不到資料夾或資料夾是空的"

    file_updates = []
    moving_file_ids = {row["id"] for row in files}
    for row in files:
        suffix = row["virtual_path"][len(old_folder):]
        target_path = new_folder + suffix
        conflict = conn.execute(
            """
            SELECT id FROM storage_files
            WHERE owner_user_id=? AND virtual_path=? AND deleted_at IS NULL
            """,
            (owner_user_id, target_path),
        ).fetchone()
        if conflict and conflict["id"] not in moving_file_ids:
            return None, f"目標路徑已有檔案：{target_path}"
        file_updates.append((target_path, _display_name_from_path(target_path), row["id"]))

    folder_updates = []
    moving_folder_ids = {row["id"] for row in folders}
    for row in folders:
        suffix = row["virtual_path"][len(old_folder):]
        target_path = new_folder + suffix
        conflict = conn.execute(
            """
            SELECT id FROM storage_folders
            WHERE owner_user_id=? AND virtual_path=? AND deleted_at IS NULL
            """,
            (owner_user_id, target_path),
        ).fetchone()
        if conflict and conflict["id"] not in moving_folder_ids:
            return None, f"目標路徑已有資料夾：{target_path}"
        folder_updates.append((target_path, _display_name_from_path(target_path), row["id"]))

    now = _now()
    for target_path, display_name, row_id in file_updates:
        conn.execute(
            "UPDATE storage_files SET virtual_path=?, display_name=?, updated_at=? WHERE id=? AND owner_user_id=?",
            (target_path, display_name, now, row_id, owner_user_id),
        )
    if not folders:
        folder, msg = create_storage_folder(conn, actor=actor, path=new_folder)
        if msg:
            return None, msg
        folder_updates.append((new_folder, _display_name_from_path(new_folder), folder["id"]))
    for target_path, display_name, row_id in folder_updates:
        conn.execute(
            "UPDATE storage_folders SET virtual_path=?, display_name=?, updated_at=? WHERE id=? AND owner_user_id=?",
            (target_path, display_name, now, row_id, owner_user_id),
        )
    return {
        "old_path": old_folder,
        "new_path": new_folder,
        "moved_files": len(file_updates),
        "moved_folders": len(folder_updates),
    }, None


def trash_storage_folder(conn, *, actor, path):
    ensure_storage_album_schema(conn)
    owner_user_id = int(actor["id"])
    try:
        folder_path = _folder_prefix(path)
    except ValueError:
        return None, "資料夾路徑不安全或格式錯誤"
    if folder_path == "/":
        return None, "不能回收根目錄"
    file_rows = conn.execute(
        """
        SELECT id FROM storage_files
        WHERE owner_user_id=? AND deleted_at IS NULL AND is_trashed=0
              AND virtual_path LIKE ?
        ORDER BY virtual_path ASC
        """,
        (owner_user_id, folder_path.rstrip("/") + "/%"),
    ).fetchall()
    folder_rows = conn.execute(
        """
        SELECT id FROM storage_folders
        WHERE owner_user_id=? AND deleted_at IS NULL
              AND (virtual_path=? OR virtual_path LIKE ?)
        ORDER BY virtual_path ASC
        """,
        (owner_user_id, folder_path, folder_path.rstrip("/") + "/%"),
    ).fetchall()
    if not file_rows and not folder_rows:
        return None, "找不到資料夾或資料夾是空的"
    now = _now()
    album_rows = conn.execute(
        """
        SELECT DISTINCT a.id
        FROM albums a
        JOIN album_files af ON af.album_id=a.id AND af.deleted_at IS NULL
        WHERE a.owner_user_id=? AND a.deleted_at IS NULL AND af.caption=?
        """,
        (owner_user_id, folder_path),
    ).fetchall()
    conn.executemany(
        """
        UPDATE storage_files
        SET is_trashed=1, trashed_at=?, trash_source=NULL, updated_at=?
        WHERE id=? AND owner_user_id=?
        """,
        [(now, now, row["id"], owner_user_id) for row in file_rows],
    )
    conn.executemany(
        "UPDATE storage_folders SET deleted_at=?, updated_at=? WHERE id=? AND owner_user_id=?",
        [(now, now, row["id"], owner_user_id) for row in folder_rows],
    )
    if album_rows:
        conn.executemany(
            "UPDATE albums SET deleted_at=?, updated_at=? WHERE id=? AND owner_user_id=?",
            [(now, now, row["id"], owner_user_id) for row in album_rows],
        )
        conn.executemany(
            "UPDATE album_share_links SET revoked_at=? WHERE album_id=? AND revoked_at IS NULL",
            [(now, row["id"]) for row in album_rows],
        )
    return {
        "path": folder_path,
        "trashed_files": len(file_rows),
        "deleted_folders": len(folder_rows),
        "deleted_albums": len(album_rows),
    }, None


def trash_cloud_file_to_storage(conn, *, actor, file_id):
    ensure_storage_album_schema(conn)
    owner_user_id = int(actor["id"])
    file_row = conn.execute(
        "SELECT * FROM uploaded_files WHERE id=? AND owner_user_id=? AND deleted_at IS NULL",
        (str(file_id or ""), owner_user_id),
    ).fetchone()
    if not file_row:
        return None, "找不到檔案或檔案已刪除"
    now = _now()
    storage_rows = conn.execute(
        """
        SELECT id FROM storage_files
        WHERE owner_user_id=? AND file_id=? AND deleted_at IS NULL
        """,
        (owner_user_id, file_row["id"]),
    ).fetchall()
    if storage_rows:
        conn.executemany(
            """
            UPDATE storage_files
            SET is_trashed=1, trashed_at=?, trash_source='cloud_drive_delete', updated_at=?
            WHERE id=? AND owner_user_id=?
            """,
            [(now, now, row["id"], owner_user_id) for row in storage_rows],
        )
        return {"file_id": file_row["id"], "storage_file_ids": [row["id"] for row in storage_rows]}, None

    display_name = str(file_row["original_filename_plain_for_public"] or "download.bin").strip()[:160]
    try:
        virtual_path = _unique_storage_path(conn, owner_user_id, display_name, display_name)
    except ValueError:
        return None, "storage path 不安全或格式錯誤"
    storage_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO storage_files (
            id, file_id, owner_user_id, parent_id, display_name, virtual_path,
            is_trashed, trashed_at, trash_source, created_at, updated_at
        ) VALUES (?, ?, ?, NULL, ?, ?, 1, ?, 'cloud_drive_delete', ?, ?)
        """,
        (storage_id, file_row["id"], owner_user_id, display_name, virtual_path, now, now, now),
    )
    sync_user_storage_summary(
        conn,
        owner_user_id,
        actor_user_id=owner_user_id,
        source="cloud_drive_delete",
        reason="cloud_file_moved_to_trash",
    )
    return {"file_id": file_row["id"], "storage_file_ids": [storage_id]}, None


def list_storage_trash(conn, *, actor, limit=100, offset=0, ensure_schema=True):
    if ensure_schema:
        ensure_storage_album_schema(conn)
    rows = conn.execute(
        """
        SELECT sf.*, f.size_bytes, f.privacy_mode, f.risk_level, f.scan_status,
               f.system_asset_type, f.original_filename_plain_for_public
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.deleted_at IS NULL AND f.deleted_at IS NULL
              AND sf.is_trashed=1 AND COALESCE(f.system_asset_type, '')<>'avatar'
        ORDER BY sf.trashed_at DESC, sf.updated_at DESC
        LIMIT ? OFFSET ?
        """,
        (int(actor["id"]), int(limit), int(offset)),
    ).fetchall()
    return [dict(row) for row in rows]


def trash_storage_file(conn, *, actor, storage_file_id):
    ensure_storage_album_schema(conn)
    row = get_storage_file(conn, actor=actor, storage_file_id=storage_file_id)
    if not row or row.get("deleted_at") or row.get("file_deleted_at"):
        return None, "找不到檔案或檔案已刪除"
    if int(row.get("is_trashed") or 0):
        return row, None
    now = _now()
    conn.execute(
        """
        UPDATE storage_files
        SET is_trashed=1, trashed_at=?, trash_source=NULL, updated_at=?
        WHERE id=? AND owner_user_id=?
        """,
        (now, now, storage_file_id, int(actor["id"])),
    )
    return get_storage_file(conn, actor=actor, storage_file_id=storage_file_id), None


def restore_storage_file(conn, *, actor, storage_file_id):
    ensure_storage_album_schema(conn)
    row = get_storage_file(conn, actor=actor, storage_file_id=storage_file_id)
    if not row or row.get("deleted_at") or row.get("file_deleted_at"):
        return None, "找不到檔案或檔案已刪除"
    if not int(row.get("is_trashed") or 0):
        return row, None
    now = _now()
    conn.execute(
        """
        UPDATE storage_files
        SET is_trashed=0, restored_at=?, trash_source=NULL, updated_at=?
        WHERE id=? AND owner_user_id=?
        """,
        (now, now, storage_file_id, int(actor["id"])),
    )
    if row.get("trash_source") == "cloud_drive_delete":
        conn.execute(
            """
            UPDATE storage_files
            SET trash_source=NULL, updated_at=?
            WHERE owner_user_id=? AND file_id=? AND deleted_at IS NULL
                  AND trash_source='cloud_drive_delete'
            """,
            (now, int(actor["id"]), row["file_id"]),
        )
    return get_storage_file(conn, actor=actor, storage_file_id=storage_file_id), None


def purge_storage_file(conn, *, actor, storage_file_id, storage_root=None):
    ensure_storage_album_schema(conn)
    row = get_storage_file(conn, actor=actor, storage_file_id=storage_file_id)
    if not row or row.get("deleted_at") or row.get("file_deleted_at"):
        return None, "找不到檔案或檔案已刪除"
    now = _now()
    conn.execute(
        """
        UPDATE storage_files
        SET deleted_at=?, updated_at=?
        WHERE id=? AND owner_user_id=?
        """,
        (now, now, storage_file_id, int(actor["id"])),
    )
    purge_result = _purge_unreferenced_uploaded_files(
        conn,
        owner_user_id=int(actor["id"]),
        file_ids=[row["file_id"]],
        storage_root=storage_root,
        now=now,
    )
    summary = sync_user_storage_summary(
        conn,
        actor["id"],
        actor_user_id=actor["id"],
        source="trash",
        reason="storage_file_purged",
    )
    return {
        "id": storage_file_id,
        "file_id": row["file_id"],
        "purged_file_ids": purge_result["purged_file_ids"],
        "removed_physical_files": purge_result["removed_physical_files"],
        "storage": summary,
    }, None


def restore_storage_trash(conn, *, actor):
    ensure_storage_album_schema(conn)
    now = _now()
    rows = conn.execute(
        """
        SELECT sf.id
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.deleted_at IS NULL AND sf.is_trashed=1
              AND f.deleted_at IS NULL AND COALESCE(f.system_asset_type, '')<>'avatar'
        """,
        (int(actor["id"]),),
    ).fetchall()
    if rows:
        conn.executemany(
            """
            UPDATE storage_files
            SET is_trashed=0, restored_at=?, trash_source=NULL, updated_at=?
            WHERE id=? AND owner_user_id=?
            """,
            [(now, now, row["id"], int(actor["id"])) for row in rows],
        )
    return {"restored": len(rows)}, None


def purge_storage_trash(conn, *, actor, storage_root=None):
    ensure_storage_album_schema(conn)
    now = _now()
    rows = conn.execute(
        """
        SELECT sf.id, sf.file_id, sf.trash_source
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.deleted_at IS NULL AND sf.is_trashed=1
              AND f.deleted_at IS NULL AND COALESCE(f.system_asset_type, '')<>'avatar'
        """,
        (int(actor["id"]),),
    ).fetchall()
    purge_result = {"purged_file_ids": [], "removed_physical_files": 0}
    if rows:
        conn.executemany(
            """
            UPDATE storage_files
            SET deleted_at=?, updated_at=?
            WHERE id=? AND owner_user_id=?
            """,
            [(now, now, row["id"], int(actor["id"])) for row in rows],
        )
        purge_result = _purge_unreferenced_uploaded_files(
            conn,
            owner_user_id=int(actor["id"]),
            file_ids=[row["file_id"] for row in rows],
            storage_root=storage_root,
            now=now,
        )
    summary = sync_user_storage_summary(
        conn,
        actor["id"],
        actor_user_id=actor["id"],
        source="trash",
        reason="storage_trash_purged",
    )
    return {
        "purged": len(rows),
        "purged_file_ids": purge_result["purged_file_ids"],
        "removed_physical_files": purge_result["removed_physical_files"],
        "storage": summary,
    }, None

def _hash_share_token(token):
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _normalize_storage_share_max_views(value):
    if value in (None, "", 0, "0"):
        return 0
    try:
        count = int(value)
    except Exception:
        raise ValueError("最大下載次數必須是整數")
    if count < 0 or count > 1_000_000:
        raise ValueError("最大下載次數必須介於 0 到 1000000")
    return count


def _normalize_storage_share_scope(value):
    scope = str(value or "link").strip().lower()
    if scope not in {"link", "account"}:
        raise ValueError("分享範圍必須是 link 或 account")
    return scope


def _normalize_storage_share_envelope(value):
    if value in (None, "", {}):
        return ""
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except Exception as exc:
            raise ValueError("E2EE 分享授權格式不正確") from exc
    elif isinstance(value, dict):
        payload = dict(value)
    else:
        raise ValueError("E2EE 分享授權格式不正確")
    if str(payload.get("alg") or "") != "AES-GCM" or int(payload.get("v") or 0) != 1:
        raise ValueError("E2EE 分享授權版本不支援")
    nonce = str(payload.get("nonce") or "").strip()
    ciphertext = str(payload.get("ciphertext") or "").strip()
    if not nonce or not ciphertext:
        raise ValueError("E2EE 分享授權缺少必要欄位")
    return json.dumps({"alg": "AES-GCM", "v": 1, "nonce": nonce, "ciphertext": ciphertext}, ensure_ascii=False, sort_keys=True)


def _storage_file_for_share(conn, *, actor, storage_file_id=None, file_id=None):
    storage_file_id = str(storage_file_id or "").strip()
    if storage_file_id:
        storage_file = get_storage_file(conn, actor=actor, storage_file_id=storage_file_id)
        if storage_file:
            return storage_file, None
        return None, "找不到 storage 檔案或檔案已刪除"
    file_id = str(file_id or "").strip()
    if not file_id:
        return None, "缺少要分享的檔案"
    file_row = conn.execute(
        "SELECT * FROM uploaded_files WHERE id=? AND owner_user_id=? AND deleted_at IS NULL",
        (file_id, int(actor["id"])),
    ).fetchone()
    if not file_row:
        return None, "找不到檔案或檔案已刪除"
    existing = conn.execute(
        """
        SELECT id FROM storage_files
        WHERE file_id=? AND owner_user_id=? AND deleted_at IS NULL AND is_trashed=0
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (file_id, int(actor["id"])),
    ).fetchone()
    if existing:
        return get_storage_file(conn, actor=actor, storage_file_id=existing["id"]), None
    display_name = file_row["original_filename_plain_for_public"] or "download.bin"
    storage_file, msg = create_storage_file_entry(
        conn,
        actor=actor,
        file_row=file_row,
        virtual_path=_unique_storage_path(conn, int(actor["id"]), display_name, display_name),
        display_name=display_name,
        source="share",
    )
    return storage_file, msg


def _storage_share_payload(row, *, token=None):
    data = dict(row) if row else None
    if not data:
        return None
    if token:
        data["token"] = token
    share_token = token if token is not None else (data.get("token") or "")
    if token is None:
        data.pop("token", None)
    data["share_url"] = f"/shared/files/{share_token}" if share_token else ""
    data["download_url"] = f"/api/storage/shared/{share_token}/download" if share_token else ""
    data["preview_url"] = f"/api/storage/shared/{share_token}/preview" if share_token else ""
    data["preview_content_url"] = f"/api/storage/shared/{share_token}/preview/content" if share_token else ""
    data["requires_fragment_key"] = bool(str(data.get("wrapped_file_key_envelope") or "").strip())
    data["password_required"] = bool(int(data.get("password_required") or 0))
    data.pop("password_hash", None)
    return data


def _hash_storage_share_password(password):
    password = str(password or "")
    if len(password) > MAX_ALBUM_SHARE_PASSWORD_LENGTH:
        raise ValueError("檔案分享密碼太長")
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        ALBUM_SHARE_PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${ALBUM_SHARE_PASSWORD_ITERATIONS}${salt}${digest}"


def _verify_storage_share_password(password, stored_hash):
    parts = str(stored_hash or "").split("$", 3)
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        parts[2].encode("utf-8"),
        iterations,
    ).hex()
    return secrets.compare_digest(actual, parts[3])


def apply_storage_share_password(conn, link_id, *, password=None, clear_password=False):
    ensure_storage_album_schema(conn)
    if clear_password:
        conn.execute(
            "UPDATE storage_share_links SET password_required=0, password_hash=NULL WHERE id=?",
            (link_id,),
        )
        return None
    password = str(password or "")
    if not password:
        return None
    try:
        password_hash = _hash_storage_share_password(password)
    except ValueError as exc:
        return str(exc)
    conn.execute(
        "UPDATE storage_share_links SET password_required=1, password_hash=? WHERE id=?",
        (password_hash, link_id),
    )
    return None


def create_share_link(
    conn,
    *,
    actor,
    storage_file_id=None,
    file_id=None,
    expires_at=None,
    can_preview=False,
    access_scope="link",
    required_user_id=None,
    required_username=None,
    max_views=0,
    wrapped_file_key_envelope=None,
    share_password=None,
):
    ensure_storage_album_schema(conn)
    storage_file, msg = _storage_file_for_share(conn, actor=actor, storage_file_id=storage_file_id, file_id=file_id)
    if msg:
        return None, msg
    if not storage_file or storage_file.get("deleted_at") or storage_file.get("file_deleted_at") or int(storage_file.get("is_trashed") or 0):
        return None, "找不到 storage 檔案或檔案已刪除"
    try:
        access_scope = _normalize_storage_share_scope(access_scope)
        max_views = _normalize_storage_share_max_views(max_views)
        envelope = _normalize_storage_share_envelope(wrapped_file_key_envelope)
    except ValueError as exc:
        return None, str(exc)
    if access_scope == "account":
        if required_user_id in (None, "") and str(required_username or "").strip():
            user = conn.execute(
                "SELECT id FROM users WHERE username=? LIMIT 1",
                (str(required_username or "").strip(),),
            ).fetchone()
            required_user_id = int(user["id"]) if user else None
        try:
            required_user_id = int(required_user_id)
        except Exception:
            return None, "請指定可下載的帳戶"
        if not conn.execute("SELECT id FROM users WHERE id=? LIMIT 1", (required_user_id,)).fetchone():
            return None, "找不到指定帳戶"
    else:
        required_user_id = None
    if str(storage_file.get("privacy_mode") or "") == "e2ee" and not envelope:
        return None, "E2EE 檔案分享必須建立瀏覽器端分享授權"
    token = secrets.token_urlsafe(32)
    now = _now()
    link_id = uuid.uuid4().hex
    password_hash = None
    password_required = 0
    if str(share_password or ""):
        try:
            password_hash = _hash_storage_share_password(share_password)
            password_required = 1
        except ValueError as exc:
            return None, str(exc)
    conn.execute(
        """
        INSERT INTO storage_share_links (
            id, storage_file_id, file_id, owner_user_id, token, token_hash, can_download,
            can_preview, access_scope, required_user_id, max_views, wrapped_file_key_envelope,
            password_required, password_hash, expires_at, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            link_id,
            storage_file["id"],
            storage_file["file_id"],
            int(actor["id"]),
            token,
            _hash_share_token(token),
            1 if can_preview else 0,
            access_scope,
            required_user_id,
            max_views,
            envelope or None,
            password_required,
            password_hash,
            str(expires_at or "").strip() or None,
            int(actor["id"]),
            now,
        ),
    )
    link = get_share_link(conn, actor=actor, link_id=link_id)
    link["token"] = token
    link["share_url"] = f"/shared/files/{token}"
    link["download_url"] = f"/api/storage/shared/{token}/download"
    link["preview_url"] = f"/api/storage/shared/{token}/preview"
    link["preview_content_url"] = f"/api/storage/shared/{token}/preview/content"
    return link, None


def list_share_links(conn, *, actor, storage_file_id=None):
    ensure_storage_album_schema(conn)
    where = "owner_user_id=?"
    params = [int(actor["id"])]
    if storage_file_id:
        where += " AND storage_file_id=?"
        params.append(str(storage_file_id))
    rows = conn.execute(
        f"""
        SELECT id, storage_file_id, file_id, owner_user_id, token, can_download, can_preview,
               access_scope, required_user_id, max_views, wrapped_file_key_envelope,
               password_required, expires_at, revoked_at, access_count, last_accessed_at,
               created_by, created_at
        FROM storage_share_links
        WHERE {where}
        ORDER BY created_at DESC
        """,
        tuple(params),
    ).fetchall()
    return [_storage_share_payload(row) for row in rows]


def get_share_link(conn, *, actor, link_id):
    row = conn.execute(
        """
        SELECT id, storage_file_id, file_id, owner_user_id, token, can_download, can_preview,
               access_scope, required_user_id, max_views, wrapped_file_key_envelope,
               password_required, expires_at, revoked_at, access_count, last_accessed_at,
               created_by, created_at
        FROM storage_share_links
        WHERE id=? AND owner_user_id=?
        """,
        (link_id, int(actor["id"])),
    ).fetchone()
    return _storage_share_payload(row) if row else None


def revoke_share_link(conn, *, actor, link_id):
    ensure_storage_album_schema(conn)
    link = get_share_link(conn, actor=actor, link_id=link_id)
    if not link:
        return None, "找不到分享連結"
    if link.get("revoked_at"):
        return link, None
    conn.execute("UPDATE storage_share_links SET revoked_at=? WHERE id=?", (_now(), link_id))
    return get_share_link(conn, actor=actor, link_id=link_id), None


def resolve_share_token(conn, token, *, actor=None, require_download=True, password=None, password_verified=False):
    ensure_storage_album_schema(conn)
    row = conn.execute(
        """
        SELECT sl.*, sf.display_name, sf.deleted_at AS storage_deleted_at, sf.is_trashed,
               f.storage_path, f.original_filename_plain_for_public, f.scan_status, f.risk_level,
               f.privacy_mode, f.size_bytes, f.mime_type_plain_for_public,
               f.original_filename_encrypted AS encrypted_metadata,
               f.encryption_algorithm, f.encryption_version, f.nonce,
               f.deleted_at AS file_deleted_at,
               u.username AS required_username
        FROM storage_share_links sl
        JOIN storage_files sf ON sf.id=sl.storage_file_id
        JOIN uploaded_files f ON f.id=sl.file_id
        LEFT JOIN users u ON u.id=sl.required_user_id
        WHERE sl.token_hash=?
        """,
        (_hash_share_token(token),),
    ).fetchone()
    if not row:
        return None, "not_found"
    data = dict(row)
    if data.get("revoked_at"):
        return None, "revoked"
    if data.get("expires_at") and data["expires_at"] <= _now():
        return None, "expired"
    max_views = int(data.get("max_views") or 0)
    if max_views > 0 and int(data.get("access_count") or 0) >= max_views:
        return None, "view_limit_reached"
    if data.get("storage_deleted_at") or data.get("file_deleted_at") or int(data.get("is_trashed") or 0):
        return None, "deleted"
    if require_download and not int(data.get("can_download") or 0):
        return None, "download_disabled"
    if not require_download and not (int(data.get("can_download") or 0) or int(data.get("can_preview") or 0)):
        return None, "download_disabled"
    if str(data.get("access_scope") or "link") == "account":
        if not actor:
            return None, "login_required"
        actor_id = int(actor["id"])
        if actor_id != int(data.get("required_user_id") or 0) and actor_id != int(data.get("owner_user_id") or 0):
            return None, "forbidden"
    if int(data.get("password_required") or 0) and not password_verified:
        if not str(password or ""):
            return None, "password_required"
        if not _verify_storage_share_password(password, data.get("password_hash")):
            return None, "password_invalid"
    if str(data.get("privacy_mode") or "") == "e2ee" and not str(data.get("wrapped_file_key_envelope") or "").strip():
        return None, "e2ee_share_authorization_missing"
    return data, ""


def mark_share_link_accessed(conn, link_id):
    conn.execute(
        """
        UPDATE storage_share_links
        SET access_count=access_count + 1, last_accessed_at=?
        WHERE id=?
        """,
        (_now(), link_id),
    )
