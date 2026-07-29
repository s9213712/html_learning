import hashlib
import secrets
import uuid
from datetime import datetime

ALBUM_SHARE_PASSWORD_ITERATIONS = 200_000
MAX_ALBUM_SHARE_PASSWORD_LENGTH = 256



from services.storage import catalog as _catalog

globals().update({name: value for name, value in _catalog.__dict__.items() if not name.startswith("__")})

def _normalize_album_visibility(value):
    visibility = str(value or "private").strip().lower()
    return visibility if visibility in {"private", "unlisted", "public"} else "private"


def _album_share_url(token):
    return f"/shared/albums/{token}" if token else ""


def _hash_album_share_password(password):
    password = str(password or "")
    if len(password) > MAX_ALBUM_SHARE_PASSWORD_LENGTH:
        raise ValueError("相簿分享密碼太長")
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        ALBUM_SHARE_PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${ALBUM_SHARE_PASSWORD_ITERATIONS}${salt}${digest}"


def _verify_album_share_password(password, stored_hash):
    parts = str(stored_hash or "").split("$", 3)
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
    except Exception:
        return False
    salt = parts[2]
    expected = parts[3]
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return secrets.compare_digest(actual, expected)


def _album_share_password_required(row):
    if not row:
        return False
    keys = row.keys()
    if "password_required" in keys:
        return bool(int(row["password_required"] or 0))
    return bool(row["password_hash"] if "password_hash" in keys else "")


def _album_share_link_payload(row):
    if not row:
        return None
    token = row["token"]
    return {
        "id": row["id"],
        "album_id": row["album_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"] if "expires_at" in row.keys() else "",
        "max_views": int(row["max_views"] or 0) if "max_views" in row.keys() else 0,
        "access_count": int(row["access_count"] or 0),
        "last_accessed_at": row["last_accessed_at"],
        "url": _album_share_url(token),
        "password_required": _album_share_password_required(row),
    }


def _active_album_share_link(conn, album_id, *, ensure_schema=True):
    if ensure_schema:
        ensure_storage_album_schema(conn)
    return conn.execute(
        """
        SELECT * FROM album_share_links
        WHERE album_id=? AND revoked_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (album_id,),
    ).fetchone()


def _apply_album_share_password(conn, link_id, *, password=None, clear_password=False):
    if clear_password:
        conn.execute(
            "UPDATE album_share_links SET password_required=0, password_hash=NULL WHERE id=?",
            (link_id,),
        )
        return None
    password = str(password or "")
    if not password:
        return None
    try:
        password_hash = _hash_album_share_password(password)
    except ValueError as exc:
        return str(exc)
    conn.execute(
        "UPDATE album_share_links SET password_required=1, password_hash=? WHERE id=?",
        (password_hash, link_id),
    )
    return None


def _normalize_album_share_expires_at(value):
    text = str(value or "").strip()
    return text or None


def _normalize_album_share_max_views(value):
    try:
        count = int(value or 0)
    except Exception as exc:
        raise ValueError("最大存取次數格式錯誤") from exc
    if count < 0:
        raise ValueError("最大存取次數不可小於 0")
    return min(count, 1_000_000)


def _apply_album_share_limits(conn, link_id, *, expires_at=None, expires_at_provided=False, max_views=None, max_views_provided=False, reset_access_count=False):
    updates = []
    params = []
    if expires_at_provided:
        updates.append("expires_at=?")
        params.append(_normalize_album_share_expires_at(expires_at))
    if max_views_provided:
        updates.append("max_views=?")
        params.append(_normalize_album_share_max_views(max_views))
    if reset_access_count:
        updates.append("access_count=0")
        updates.append("last_accessed_at=NULL")
    if not updates:
        return None
    params.append(link_id)
    conn.execute(f"UPDATE album_share_links SET {', '.join(updates)} WHERE id=?", tuple(params))
    return None


def ensure_album_share_link(
    conn,
    *,
    actor,
    album_id,
    password=None,
    password_provided=False,
    clear_password=False,
    expires_at=None,
    expires_at_provided=False,
    max_views=None,
    max_views_provided=False,
    reset_access_count=False,
):
    ensure_storage_album_schema(conn)
    album = _album_row(conn, album_id)
    if not album or album["deleted_at"] or int(album["owner_user_id"]) != int(actor["id"]):
        return None, "找不到相簿"
    try:
        initial_max_views = _normalize_album_share_max_views(max_views) if max_views_provided else 0
    except ValueError as exc:
        return None, str(exc)
    existing = _active_album_share_link(conn, album_id)
    if existing:
        if password_provided or clear_password:
            msg = _apply_album_share_password(conn, existing["id"], password=password, clear_password=clear_password)
            if msg:
                return None, msg
        try:
            _apply_album_share_limits(
                conn,
                existing["id"],
                expires_at=expires_at,
                expires_at_provided=expires_at_provided,
                max_views=max_views,
                max_views_provided=max_views_provided,
                reset_access_count=reset_access_count,
            )
        except ValueError as exc:
            return None, str(exc)
        if password_provided or clear_password or expires_at_provided or max_views_provided or reset_access_count:
            existing = conn.execute("SELECT * FROM album_share_links WHERE id=?", (existing["id"],)).fetchone()
        return _album_share_link_payload(existing), None
    now = _now()
    token = secrets.token_urlsafe(32)
    link_id = uuid.uuid4().hex
    password_hash = None
    password_required = 0
    if str(password or ""):
        try:
            password_hash = _hash_album_share_password(password)
            password_required = 1
        except ValueError as exc:
            return None, str(exc)
    conn.execute(
        """
        INSERT INTO album_share_links (
            id, album_id, owner_user_id, token, token_hash, password_required,
            password_hash, expires_at, max_views, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            link_id,
            album_id,
            int(actor["id"]),
            token,
            _hash_share_token(token),
            password_required,
            password_hash,
            _normalize_album_share_expires_at(expires_at) if expires_at_provided else None,
            initial_max_views,
            int(actor["id"]),
            now,
        ),
    )
    return _album_share_link_payload(conn.execute("SELECT * FROM album_share_links WHERE id=?", (link_id,)).fetchone()), None


def revoke_album_share_links(conn, *, actor, album_id):
    ensure_storage_album_schema(conn)
    album = _album_row(conn, album_id)
    if not album or int(album["owner_user_id"]) != int(actor["id"]):
        return None, "找不到相簿"
    conn.execute(
        "UPDATE album_share_links SET revoked_at=? WHERE album_id=? AND revoked_at IS NULL",
        (_now(), album_id),
    )
    return {"album_id": album_id}, None


def _is_album_media_storage_row(row):
    mime = str((row["mime_type_plain_for_public"] if "mime_type_plain_for_public" in row.keys() else "") or "").lower()
    name = str((row["display_name"] if "display_name" in row.keys() else "") or "").lower()
    if mime.split(";", 1)[0].strip() == "image/svg+xml" or name.endswith(".svg"):
        return False
    if mime.startswith("image/") or mime.startswith("video/"):
        return True
    image_exts = (".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp")
    video_exts = (".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ogv", ".webm", ".wmv")
    return name.endswith(image_exts + video_exts)


def _album_row(conn, album_id):
    return conn.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()


def create_album(conn, *, actor, title, description="", visibility="private", share_password=None):
    ensure_storage_album_schema(conn)
    title = str(title or "").strip()
    if not title:
        return None, "相簿名稱不可為空"
    now = _now()
    album_id = uuid.uuid4().hex
    normalized_visibility = _normalize_album_visibility(visibility)
    conn.execute(
        """
        INSERT INTO albums (
            id, owner_user_id, title, description, visibility, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            album_id,
            int(actor["id"]),
            title[:120],
            str(description or "")[:1000],
            normalized_visibility,
            now,
            now,
        ),
    )
    if normalized_visibility == "unlisted":
        link, msg = ensure_album_share_link(
            conn,
            actor=actor,
            album_id=album_id,
            password=share_password,
            password_provided=share_password is not None,
        )
        if msg:
            return None, msg
    return get_album(conn, actor=actor, album_id=album_id, include_files=True), None


def create_album_from_storage_folder(conn, *, actor, path, title=None, description="", visibility="private"):
    ensure_storage_album_schema(conn)
    owner_user_id = int(actor["id"])
    try:
        folder_path = _folder_prefix(path)
    except ValueError:
        return None, "資料夾路徑不安全或格式錯誤"
    if folder_path == "/":
        return None, "不能直接把根目錄設為相簿"
    rows = conn.execute(
        """
        SELECT sf.id AS storage_file_id, sf.file_id, sf.display_name, sf.virtual_path,
               f.mime_type_plain_for_public, f.original_filename_plain_for_public
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.deleted_at IS NULL AND sf.is_trashed=0
              AND f.deleted_at IS NULL AND sf.virtual_path LIKE ?
        ORDER BY sf.virtual_path ASC
        """,
        (owner_user_id, folder_path.rstrip("/") + "/%"),
    ).fetchall()
    if not rows:
        return None, "資料夾內沒有可加入相簿的圖片或影片"
    invalid = [row for row in rows if not _is_album_media_storage_row(row)]
    if invalid:
        names = "、".join(str(row["display_name"] or row["original_filename_plain_for_public"] or row["virtual_path"]) for row in invalid[:3])
        suffix = " 等" if len(invalid) > 3 else ""
        return None, f"資料夾內含非圖片/影片檔案：{names}{suffix}"
    album, msg = create_album(
        conn,
        actor=actor,
        title=title or _display_name_from_path(folder_path),
        description=description or f"由資料夾 {folder_path} 建立",
        visibility=visibility,
    )
    if msg:
        return None, msg
    album_id = album["id"]
    now = _now()
    for index, row in enumerate(rows, start=1):
        conn.execute(
            """
            INSERT INTO album_files (
                id, album_id, storage_file_id, file_id, sort_order, caption, added_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                album_id,
                row["storage_file_id"],
                row["file_id"],
                index,
                folder_path,
                int(actor["id"]),
                now,
            ),
        )
    conn.execute("UPDATE albums SET cover_file_id=?, updated_at=? WHERE id=?", (rows[0]["file_id"], now, album_id))
    album = get_album(conn, actor=actor, album_id=album_id, include_files=True)
    album["source_folder"] = folder_path
    album["added_count"] = len(rows)
    return album, None


def ensure_output_album(conn, *, actor):
    """Keep the ComfyUI /output folder and its backing album in sync."""
    ensure_storage_album_schema(conn)
    owner_user_id = int(actor["id"])
    exact_output_file = conn.execute(
        """
        SELECT sf.id, sf.display_name, sf.virtual_path, f.original_filename_plain_for_public
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.deleted_at IS NULL AND COALESCE(sf.is_trashed, 0)=0
              AND f.deleted_at IS NULL AND sf.virtual_path='/output'
        LIMIT 1
        """,
        (owner_user_id,),
    ).fetchone()
    if exact_output_file:
        filename = _display_name_from_path(
            exact_output_file["original_filename_plain_for_public"]
            or exact_output_file["display_name"]
            or "output-file"
        )
        if filename == "output":
            filename = "output-file"
        repaired_path = _unique_storage_path(conn, owner_user_id, f"/output/{filename}", filename)
        now = _now()
        conn.execute(
            "UPDATE storage_files SET virtual_path=?, display_name=?, updated_at=? WHERE id=?",
            (repaired_path, _display_name_from_path(repaired_path), now, exact_output_file["id"]),
        )
    folder, msg = create_storage_folder(conn, actor=actor, path="/output")
    if msg:
        return None, msg
    row = conn.execute(
        """
        SELECT id FROM albums
        WHERE owner_user_id=? AND deleted_at IS NULL AND title=?
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (owner_user_id, "output"),
    ).fetchone()
    if row:
        album_id = row["id"]
    else:
        album, msg = create_album(
            conn,
            actor=actor,
            title="output",
            description="ComfyUI 預設輸出相簿",
            visibility="private",
        )
        if msg:
            return None, msg
        album_id = album["id"]

    output_rows = conn.execute(
        """
        SELECT sf.id AS storage_file_id, sf.file_id, sf.display_name, sf.virtual_path,
               f.mime_type_plain_for_public, f.original_filename_plain_for_public
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.deleted_at IS NULL AND COALESCE(sf.is_trashed, 0)=0
              AND f.deleted_at IS NULL AND sf.virtual_path LIKE '/output/%'
        ORDER BY sf.virtual_path ASC
        """,
        (owner_user_id,),
    ).fetchall()
    media_rows = [row for row in output_rows if _is_album_media_storage_row(row)]
    media_file_ids = {row["file_id"] for row in media_rows}
    existing_rows = conn.execute(
        """
        SELECT id, file_id FROM album_files
        WHERE album_id=? AND deleted_at IS NULL
        """,
        (album_id,),
    ).fetchall()
    existing_file_ids = {row["file_id"] for row in existing_rows}
    now = _now()
    added_count = 0
    for index, row in enumerate(media_rows, start=1):
        if row["file_id"] in existing_file_ids:
            conn.execute(
                """
                UPDATE album_files
                SET storage_file_id=?, sort_order=?
                WHERE album_id=? AND file_id=? AND deleted_at IS NULL
                """,
                (row["storage_file_id"], index, album_id, row["file_id"]),
            )
            continue
        conn.execute(
            """
            INSERT INTO album_files (
                id, album_id, storage_file_id, file_id, sort_order, caption, added_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                album_id,
                row["storage_file_id"],
                row["file_id"],
                index,
                "ComfyUI output",
                owner_user_id,
                now,
            ),
        )
        added_count += 1

    removed_count = 0
    for row in existing_rows:
        if row["file_id"] in media_file_ids:
            continue
        conn.execute("UPDATE album_files SET deleted_at=? WHERE id=?", (now, row["id"]))
        removed_count += 1

    cover_file_id = media_rows[0]["file_id"] if media_rows else None
    conn.execute(
        "UPDATE albums SET cover_file_id=?, updated_at=? WHERE id=?",
        (cover_file_id, now, album_id),
    )
    album = get_album(conn, actor=actor, album_id=album_id, include_files=True)
    album["folder"] = folder
    album["source_folder"] = "/output"
    album["added_count"] = added_count
    album["removed_count"] = removed_count
    return album, None


def smart_organize_albums(conn, *, actor, strategy="folder", visibility="private"):
    """Create or update albums from existing media files without moving storage files."""
    ensure_storage_album_schema(conn)
    owner_user_id = int(actor["id"])
    strategy = str(strategy or "folder").strip().lower()
    if strategy not in {"folder", "month", "type", "all"}:
        return None, "智慧整理方式不支援"
    visibility = _normalize_album_visibility(visibility)
    rows = conn.execute(
        """
        SELECT sf.id AS storage_file_id, sf.file_id, sf.display_name, sf.virtual_path,
               sf.created_at AS storage_created_at,
               f.mime_type_plain_for_public, f.original_filename_plain_for_public,
               f.created_at AS file_created_at
        FROM storage_files sf
        JOIN uploaded_files f ON f.id=sf.file_id
        WHERE sf.owner_user_id=? AND sf.deleted_at IS NULL AND COALESCE(sf.is_trashed, 0)=0
              AND f.deleted_at IS NULL
        ORDER BY sf.virtual_path ASC, sf.created_at ASC
        """,
        (owner_user_id,),
    ).fetchall()
    media_rows = [row for row in rows if _is_album_media_storage_row(row)]
    if not media_rows:
        return {
            "strategy": strategy,
            "visibility": visibility,
            "media_count": 0,
            "album_count": 0,
            "created_count": 0,
            "updated_count": 0,
            "added_count": 0,
            "albums": [],
        }, None

    def media_kind(row):
        mime = str(row["mime_type_plain_for_public"] or "").lower()
        name = str(row["display_name"] or row["original_filename_plain_for_public"] or "").lower()
        if mime.startswith("video/") or name.endswith((".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ogv", ".webm", ".wmv")):
            return "影片"
        return "圖片"

    def month_key(row):
        created = str(row["file_created_at"] or row["storage_created_at"] or "")[:7]
        if len(created) == 7 and created[4] == "-":
            return created
        return "未分類日期"

    def folder_key(row):
        path = str(row["virtual_path"] or "")
        parts = [part for part in path.split("/") if part]
        if len(parts) <= 1:
            return "根目錄"
        return "/" + "/".join(parts[:-1])

    def group_for(row):
        if strategy == "all":
            return "全部媒體"
        if strategy == "type":
            return media_kind(row)
        if strategy == "month":
            return month_key(row)
        return folder_key(row)

    groups = {}
    for row in media_rows:
        groups.setdefault(group_for(row), []).append(row)

    now = _now()
    albums = []
    created_count = 0
    updated_count = 0
    added_total = 0
    for group_name in sorted(groups.keys()):
        group_rows = groups[group_name]
        title = f"智慧整理 - {group_name}"[:120]
        description = f"智慧整理自雲端硬碟；規則={strategy}；來源={group_name}"[:1000]
        album_row = conn.execute(
            """
            SELECT * FROM albums
            WHERE owner_user_id=? AND deleted_at IS NULL AND title=?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (owner_user_id, title),
        ).fetchone()
        if album_row:
            album_id = album_row["id"]
            updated_count += 1
            conn.execute(
                "UPDATE albums SET description=?, visibility=?, updated_at=? WHERE id=?",
                (description, visibility, now, album_id),
            )
        else:
            album, msg = create_album(
                conn,
                actor=actor,
                title=title,
                description=description,
                visibility=visibility,
            )
            if msg:
                return None, msg
            album_id = album["id"]
            created_count += 1

        existing_rows = conn.execute(
            "SELECT file_id FROM album_files WHERE album_id=? AND deleted_at IS NULL",
            (album_id,),
        ).fetchall()
        existing_file_ids = {row["file_id"] for row in existing_rows}
        added_count = 0
        sort_offset = len(existing_file_ids)
        for index, row in enumerate(group_rows, start=1):
            if row["file_id"] in existing_file_ids:
                conn.execute(
                    """
                    UPDATE album_files
                    SET storage_file_id=?, sort_order=?
                    WHERE album_id=? AND file_id=? AND deleted_at IS NULL
                    """,
                    (row["storage_file_id"], index, album_id, row["file_id"]),
                )
                continue
            conn.execute(
                """
                INSERT INTO album_files (
                    id, album_id, storage_file_id, file_id, sort_order, caption, added_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    album_id,
                    row["storage_file_id"],
                    row["file_id"],
                    sort_offset + index,
                    f"smart-organize:{strategy}:{group_name}"[:500],
                    owner_user_id,
                    now,
                ),
            )
            added_count += 1
            existing_file_ids.add(row["file_id"])
        added_total += added_count
        conn.execute(
            "UPDATE albums SET cover_file_id=?, updated_at=? WHERE id=?",
            (group_rows[0]["file_id"], now, album_id),
        )
        album = get_album(conn, actor=actor, album_id=album_id, include_files=False)
        album["group"] = group_name
        album["matched_count"] = len(group_rows)
        album["added_count"] = added_count
        albums.append(album)

    return {
        "strategy": strategy,
        "visibility": visibility,
        "media_count": len(media_rows),
        "album_count": len(albums),
        "created_count": created_count,
        "updated_count": updated_count,
        "added_count": added_total,
        "albums": albums,
    }, None


def list_albums(conn, *, actor, include_deleted=False, limit=100, offset=0, ensure_schema=True):
    if ensure_schema:
        ensure_storage_album_schema(conn)
    where = "owner_user_id=?"
    params = [int(actor["id"])]
    if not include_deleted:
        where += " AND a.deleted_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT a.*, COUNT(af.id) AS file_count
        FROM albums a
        LEFT JOIN album_files af ON af.album_id=a.id AND af.deleted_at IS NULL
        WHERE {where}
        GROUP BY a.id
        ORDER BY a.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, int(limit), int(offset)),
    ).fetchall()
    albums = []
    for row in rows:
        item = dict(row)
        if item.get("visibility") == "unlisted":
            item["share_link"] = _album_share_link_payload(_active_album_share_link(conn, item["id"], ensure_schema=False))
            if item["share_link"]:
                item["share_url"] = item["share_link"]["url"]
        albums.append(item)
    return albums


def get_album(conn, *, actor, album_id, include_files=False, ensure_schema=True):
    if ensure_schema:
        ensure_storage_album_schema(conn)
    row = _album_row(conn, album_id)
    if not row or row["deleted_at"] or int(row["owner_user_id"]) != int(actor["id"]):
        return None
    data = dict(row)
    if data.get("visibility") == "unlisted":
        data["share_link"] = _album_share_link_payload(_active_album_share_link(conn, data["id"], ensure_schema=False))
        if data["share_link"]:
            data["share_url"] = data["share_link"]["url"]
    if include_files:
        files = conn.execute(
            """
            SELECT af.*,
                   COALESCE(sf.display_name, (
                       SELECT sf2.display_name
                       FROM storage_files sf2
                       WHERE sf2.file_id=af.file_id
                         AND sf2.owner_user_id=?
                         AND sf2.deleted_at IS NULL
                         AND COALESCE(sf2.is_trashed, 0)=0
                       ORDER BY sf2.updated_at DESC, sf2.created_at DESC
                       LIMIT 1
                   )) AS display_name,
                   COALESCE(sf.virtual_path, (
                       SELECT sf2.virtual_path
                       FROM storage_files sf2
                       WHERE sf2.file_id=af.file_id
                         AND sf2.owner_user_id=?
                         AND sf2.deleted_at IS NULL
                         AND COALESCE(sf2.is_trashed, 0)=0
                       ORDER BY sf2.updated_at DESC, sf2.created_at DESC
                       LIMIT 1
                   )) AS virtual_path,
                   f.original_filename_plain_for_public, f.mime_type_plain_for_public, f.storage_path,
                   f.size_bytes, f.scan_status, f.risk_level
            FROM album_files af
            JOIN uploaded_files f ON f.id=af.file_id
            LEFT JOIN storage_files sf ON sf.id=af.storage_file_id
            WHERE af.album_id=? AND af.deleted_at IS NULL AND f.deleted_at IS NULL
            ORDER BY af.sort_order ASC, af.created_at ASC
            """,
            (actor["id"], actor["id"], album_id),
        ).fetchall()
        data["files"] = [dict(file_row) for file_row in files]
    return data


def update_album(conn, *, actor, album_id, title=None, description=None, visibility=None, share_password=None, share_password_provided=False, clear_share_password=False):
    album = get_album(conn, actor=actor, album_id=album_id)
    if not album:
        return None, "找不到相簿"
    fields = []
    params = []
    if title is not None:
        title = str(title or "").strip()
        if not title:
            return None, "相簿名稱不可為空"
        fields.append("title=?")
        params.append(title[:120])
    if description is not None:
        fields.append("description=?")
        params.append(str(description or "")[:1000])
    if visibility is not None:
        next_visibility = _normalize_album_visibility(visibility)
        fields.append("visibility=?")
        params.append(next_visibility)
    password_update_requested = share_password_provided or clear_share_password
    if not fields and not password_update_requested:
        return get_album(conn, actor=actor, album_id=album_id, include_files=True), None
    now = _now()
    if fields:
        fields.append("updated_at=?")
        params.append(now)
        params.append(album_id)
        conn.execute(f"UPDATE albums SET {', '.join(fields)} WHERE id=?", tuple(params))
    else:
        conn.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, album_id))
    if visibility is not None:
        if next_visibility == "unlisted":
            link, msg = ensure_album_share_link(
                conn,
                actor=actor,
                album_id=album_id,
                password=share_password,
                password_provided=share_password_provided,
                clear_password=clear_share_password,
            )
            if msg:
                return None, msg
        else:
            revoke_album_share_links(conn, actor=actor, album_id=album_id)
    elif album.get("visibility") == "unlisted" and (share_password_provided or clear_share_password):
        link, msg = ensure_album_share_link(
            conn,
            actor=actor,
            album_id=album_id,
            password=share_password,
            password_provided=share_password_provided,
            clear_password=clear_share_password,
        )
        if msg:
            return None, msg
    return get_album(conn, actor=actor, album_id=album_id, include_files=True), None


def delete_album(conn, *, actor, album_id):
    album = get_album(conn, actor=actor, album_id=album_id)
    if not album:
        return None, "找不到相簿"
    now = _now()
    conn.execute("UPDATE albums SET deleted_at=?, updated_at=? WHERE id=?", (now, now, album_id))
    revoke_album_share_links(conn, actor=actor, album_id=album_id)
    return {"id": album_id}, None


def add_album_file(conn, *, actor, album_id, storage_file_id=None, file_id=None, caption="", sort_order=0):
    album = get_album(conn, actor=actor, album_id=album_id)
    if not album:
        return None, "找不到相簿"
    storage_file = None
    if storage_file_id:
        storage_file = get_storage_file(conn, actor=actor, storage_file_id=storage_file_id)
        if not storage_file or storage_file.get("deleted_at") or int(storage_file.get("is_trashed") or 0):
            return None, "找不到 storage 檔案或檔案已刪除"
        file_id = storage_file["file_id"]
    if not file_id:
        return None, "缺少 file_id"
    file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=? AND deleted_at IS NULL", (str(file_id),)).fetchone()
    if not file_row or int(file_row["owner_user_id"]) != int(actor["id"]):
        return None, "只能加入自己的檔案"
    existing = conn.execute(
        "SELECT id FROM album_files WHERE album_id=? AND file_id=? AND deleted_at IS NULL",
        (album_id, file_row["id"]),
    ).fetchone()
    if existing:
        return None, "檔案已在相簿內"
    now = _now()
    album_file_id = uuid.uuid4().hex
    try:
        sort_order = int(sort_order)
    except Exception:
        sort_order = 0
    conn.execute(
        """
        INSERT INTO album_files (
            id, album_id, storage_file_id, file_id, sort_order, caption, added_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            album_file_id,
            album_id,
            storage_file_id,
            file_row["id"],
            sort_order,
            str(caption or "")[:500],
            int(actor["id"]),
            now,
        ),
    )
    conn.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, album_id))
    return get_album(conn, actor=actor, album_id=album_id, include_files=True), None


def create_album_with_files(conn, *, actor, title, storage_file_ids, description=""):
    """Create an unlisted album and all memberships as one database transaction."""
    ensure_storage_album_schema(conn)
    if not isinstance(storage_file_ids, (list, tuple)):
        return None, "storage_file_ids 必須是陣列"
    if not 1 <= len(storage_file_ids) <= 100:
        return None, "批次分享一次只能包含 1 到 100 個檔案"
    normalized_ids = []
    seen = set()
    for raw_id in storage_file_ids:
        storage_file_id = str(raw_id or "").strip()
        if not storage_file_id:
            return None, "storage_file_ids 包含空白檔案 ID"
        if storage_file_id in seen:
            return None, "storage_file_ids 不可包含重複檔案"
        seen.add(storage_file_id)
        normalized_ids.append(storage_file_id)

    savepoint = f"album_batch_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        album, msg = create_album(
            conn,
            actor=actor,
            title=title,
            description=description,
            visibility="unlisted",
        )
        if msg:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            return None, msg
        for sort_order, storage_file_id in enumerate(normalized_ids, start=1):
            _, msg = add_album_file(
                conn,
                actor=actor,
                album_id=album["id"],
                storage_file_id=storage_file_id,
                sort_order=sort_order,
            )
            if msg:
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
                return None, msg
        result = get_album(conn, actor=actor, album_id=album["id"], include_files=True)
        conn.execute(f"RELEASE {savepoint}")
        return result, None
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise


def remove_album_file(conn, *, actor, album_id, album_file_id):
    album = get_album(conn, actor=actor, album_id=album_id)
    if not album:
        return None, "找不到相簿"
    row = conn.execute(
        "SELECT id FROM album_files WHERE id=? AND album_id=? AND deleted_at IS NULL",
        (album_file_id, album_id),
    ).fetchone()
    if not row:
        return None, "找不到相簿檔案"
    now = _now()
    conn.execute("UPDATE album_files SET deleted_at=? WHERE id=?", (now, album_file_id))
    conn.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, album_id))
    return get_album(conn, actor=actor, album_id=album_id, include_files=True), None

def resolve_album_share_token(conn, token, password=None):
    ensure_storage_album_schema(conn)
    token = str(token or "").strip()
    if not token:
        return None, "missing_token"
    row = conn.execute(
        """
        SELECT asl.*, a.title, a.description, a.visibility, a.cover_file_id,
               a.created_at AS album_created_at, a.updated_at AS album_updated_at
        FROM album_share_links asl
        JOIN albums a ON a.id=asl.album_id
        WHERE asl.token_hash=? AND a.deleted_at IS NULL
        """,
        (_hash_share_token(token),),
    ).fetchone()
    if not row:
        return None, "not_found"
    if row["revoked_at"]:
        return None, "revoked"
    if row["visibility"] != "unlisted":
        return None, "not_unlisted"
    if row["expires_at"] and row["expires_at"] <= _now():
        return None, "expired"
    max_views = int(row["max_views"] or 0)
    if max_views > 0 and int(row["access_count"] or 0) >= max_views:
        return None, "view_limit_reached"
    if _album_share_password_required(row):
        if not str(password or ""):
            return None, "password_required"
        if not _verify_album_share_password(password, row["password_hash"]):
            return None, "password_invalid"
    return row, None


def mark_album_share_link_accessed(conn, link_id):
    conn.execute(
        """
        UPDATE album_share_links
        SET access_count=access_count + 1, last_accessed_at=?
        WHERE id=?
        """,
        (_now(), link_id),
    )


def public_album_payload(conn, share_row):
    token = share_row["token"]
    files = conn.execute(
        """
        SELECT af.id AS album_file_id, af.file_id, af.caption, af.sort_order,
               COALESCE(sf.display_name, f.original_filename_plain_for_public, af.file_id) AS display_name,
               f.mime_type_plain_for_public, f.size_bytes, f.scan_status, f.risk_level
        FROM album_files af
        JOIN uploaded_files f ON f.id=af.file_id
        LEFT JOIN storage_files sf ON sf.id=af.storage_file_id
        WHERE af.album_id=? AND af.deleted_at IS NULL AND f.deleted_at IS NULL
        ORDER BY af.sort_order ASC, af.created_at ASC
        """,
        (share_row["album_id"],),
    ).fetchall()
    return {
        "id": share_row["album_id"],
        "title": share_row["title"],
        "description": share_row["description"] or "",
        "visibility": share_row["visibility"],
        "created_at": share_row["album_created_at"],
        "updated_at": share_row["album_updated_at"],
        "files": [
            {
                "album_file_id": row["album_file_id"],
                "file_id": row["file_id"],
                "display_name": row["display_name"],
                "mime_type": row["mime_type_plain_for_public"],
                "size_bytes": int(row["size_bytes"] or 0),
                "scan_status": row["scan_status"],
                "risk_level": row["risk_level"],
                "download_url": f"/api/storage/shared/albums/{token}/files/{row['file_id']}/download",
            }
            for row in files
        ],
    }


def resolve_album_share_file(conn, token, file_id, password=None):
    share_row, reason = resolve_album_share_token(conn, token, password=password)
    if not share_row:
        return None, reason
    file_row = conn.execute(
        """
        SELECT f.*, COALESCE(sf.display_name, f.original_filename_plain_for_public, f.id) AS display_name
        FROM album_files af
        JOIN uploaded_files f ON f.id=af.file_id
        LEFT JOIN storage_files sf ON sf.id=af.storage_file_id
        WHERE af.album_id=? AND af.file_id=? AND af.deleted_at IS NULL AND f.deleted_at IS NULL
        LIMIT 1
        """,
        (share_row["album_id"], str(file_id or "")),
    ).fetchone()
    if not file_row:
        return None, "file_not_found"
    return {"share": share_row, "file": file_row}, None
