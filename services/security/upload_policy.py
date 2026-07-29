import json
import mimetypes
import os
import shutil
from datetime import datetime
from pathlib import Path

from services.security.identity import is_admin_role
from services.storage.global_capacity import resolve_global_capacity_limit
from services.storage.quota_overrides import apply_storage_quota_override, get_storage_quota_override
from services.storage.quota_purchases import purchased_storage_summary

from services.security.upload_schema import (
    ADMIN_DISK_QUOTA_RATIO,
    ADMIN_DISK_WARNING_RATIO,
    ALLOWED_CLAMAV_COMMANDS,
    ALLOWED_SCANNER_BACKENDS,
    ALLOWED_YARA_COMMANDS,
    ARCHIVE_EXTENSIONS,
    CLOUD_DRIVE_POLICY_BOOL_FIELDS,
    CLOUD_DRIVE_POLICY_INT_FIELDS,
    DEFAULT_CLOUD_DRIVE_SECURITY_POLICY,
    DEFAULT_FILE_TYPE_POLICIES,
    EXECUTABLE_EXTENSIONS,
    MACRO_OFFICE_EXTENSIONS,
    MANAGER_CLOUD_DRIVE_QUOTA_BYTES,
    OFFICE_EXTENSIONS,
    RISK_LEVELS,
    SAFE_PUBLIC_MIME_TYPES,
    SCAN_STATUSES,
    SUPERWEAK_CLOUD_DRIVE_QUOTA_BYTES,
    UNSAFE_PUBLIC_MIME_TYPES,
    UPLOAD_PRIVACY_MODES,
    UploadPolicyDecision,
    ensure_upload_security_schema,
)


def normalize_privacy_mode(value):
    mode = str(value or "").strip()
    if mode not in UPLOAD_PRIVACY_MODES:
        ordered_modes = ["standard_plain", "server_encrypted", "e2ee"]
        supported = ", ".join(item for item in ordered_modes if item in UPLOAD_PRIVACY_MODES)
        raise ValueError(f"unsupported upload privacy mode: expected one of {supported}")
    return mode


def canonical_privacy_mode(value):
    try:
        return normalize_privacy_mode(value)
    except Exception:
        return str(value or "")


def is_e2ee_privacy_mode(value):
    return canonical_privacy_mode(value) == "e2ee"


def is_server_encrypted_privacy_mode(value):
    return canonical_privacy_mode(value) == "server_encrypted"


def normalize_scan_status(value):
    status = str(value or "").strip()
    if status not in SCAN_STATUSES:
        raise ValueError("unsupported scan status")
    return status


def safe_public_filename(filename):
    name = os.path.basename(str(filename or "")).strip()
    name = "".join(ch if ch.isalnum() or ch in "._- ()" else "_" for ch in name)
    return name[:160] or "upload.bin"


def safe_public_mime_type(filename=None, declared_mime=None):
    guessed = mimetypes.guess_type(str(filename or ""))[0] or ""
    candidate = str(guessed or declared_mime or "").split(";", 1)[0].strip().lower()
    if not candidate or candidate in UNSAFE_PUBLIC_MIME_TYPES:
        return "application/octet-stream"
    if candidate in SAFE_PUBLIC_MIME_TYPES:
        return candidate
    if candidate.startswith("image/"):
        return "application/octet-stream"
    if candidate.startswith("text/"):
        return "text/plain"
    return "application/octet-stream"


def file_extension(filename):
    return Path(str(filename or "")).suffix.lower()


def policy_category_for_extension(extension):
    ext = str(extension or "").lower()
    if ext in EXECUTABLE_EXTENSIONS:
        return "executable"
    if ext in ARCHIVE_EXTENSIONS:
        return "archive"
    if ext in MACRO_OFFICE_EXTENSIONS:
        return "office_macro"
    if ext in OFFICE_EXTENSIONS:
        return "office"
    return "default"


def get_file_type_policy(conn, category):
    row = conn.execute("SELECT * FROM file_type_policies WHERE category=?", (category,)).fetchone()
    if row:
        return dict(row)
    fallback = DEFAULT_FILE_TYPE_POLICIES["default"]
    now = datetime.now().isoformat()
    return {
        "category": "default",
        "extensions_json": json.dumps(fallback["extensions"], ensure_ascii=False),
        "public_allowed": 1,
        "server_readable_allowed": 1,
        "e2ee_allowed": 1,
        "default_risk_level": fallback["default_risk_level"],
        "allow_public_share": 1,
        "requires_scan": 1,
        "warn_on_download": 0,
        "notes": fallback["notes"],
        "created_at": now,
        "updated_at": now,
    }


def serialize_cloud_drive_security_policy(row):
    if not row:
        row = DEFAULT_CLOUD_DRIVE_SECURITY_POLICY
        return {
            "scope": row["scope"],
            "require_scan_before_download": bool(row["require_scan_before_download"]),
            "block_unclean_downloads": bool(row["block_unclean_downloads"]),
            "warn_high_risk_downloads": bool(row["warn_high_risk_downloads"]),
            "allow_inline_preview_for_high_risk": bool(row["allow_inline_preview_for_high_risk"]),
            "e2ee_server_scan_claim_allowed": bool(row["e2ee_server_scan_claim_allowed"]),
            "revoke_shares_on_suspension": bool(row["revoke_shares_on_suspension"]),
            "scanner_enabled": bool(row["scanner_enabled"]),
            "scanner_backend": row["scanner_backend"],
            "scanner_command": row["scanner_command"],
            "scanner_timeout_seconds": int(row["scanner_timeout_seconds"]),
            "fail_closed_on_scanner_error": bool(row["fail_closed_on_scanner_error"]),
            "quarantine_on_infected": bool(row["quarantine_on_infected"]),
            "validate_magic_mime": bool(row["validate_magic_mime"]),
            "deep_archive_scan_enabled": bool(row["deep_archive_scan_enabled"]),
            "max_archive_depth": int(row["max_archive_depth"]),
            "office_macro_scan_enabled": bool(row["office_macro_scan_enabled"]),
            "image_reencode_enabled": bool(row["image_reencode_enabled"]),
            "image_reencode_max_pixels": int(row["image_reencode_max_pixels"]),
            "yara_enabled": bool(row["yara_enabled"]),
            "yara_command": row["yara_command"],
            "yara_rules_path": row["yara_rules_path"],
            "max_archive_files": int(row["max_archive_files"]),
            "max_archive_uncompressed_bytes": int(row["max_archive_uncompressed_bytes"]),
            "max_daily_downloads": int(row["max_daily_downloads"]),
            "notes": row["notes"],
        }
    data = dict(row)
    for key in (
        "require_scan_before_download",
        "block_unclean_downloads",
        "warn_high_risk_downloads",
        "allow_inline_preview_for_high_risk",
        "e2ee_server_scan_claim_allowed",
        "revoke_shares_on_suspension",
        "scanner_enabled",
        "fail_closed_on_scanner_error",
        "quarantine_on_infected",
        "validate_magic_mime",
        "deep_archive_scan_enabled",
        "office_macro_scan_enabled",
        "image_reencode_enabled",
        "yara_enabled",
    ):
        data[key] = bool(data.get(key))
    for key in ("scanner_timeout_seconds", "max_archive_depth", "image_reencode_max_pixels", "max_archive_files", "max_archive_uncompressed_bytes", "max_daily_downloads"):
        data[key] = int(data.get(key) or 0)
    data["scanner_backend"] = str(data.get("scanner_backend") or "disabled")
    data["scanner_command"] = str(data.get("scanner_command") or "")
    data["yara_command"] = str(data.get("yara_command") or "")
    data["yara_rules_path"] = str(data.get("yara_rules_path") or "")
    return data


def get_cloud_drive_security_policy(conn, scope="default"):
    ensure_upload_security_schema(conn)
    row = conn.execute(
        "SELECT * FROM cloud_drive_security_policies WHERE scope=?",
        (scope or "default",),
    ).fetchone()
    return serialize_cloud_drive_security_policy(row)


def _allowed_yara_rules_root():
    configured = str(os.environ.get("HACKME_YARA_RULES_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / "security" / "yara_rules").resolve()


def _validate_yara_rules_path(rules_path):
    try:
        allowed_root = _allowed_yara_rules_root()
        resolved = Path(rules_path).expanduser().resolve()
        if os.path.commonpath([str(allowed_root), str(resolved)]) != str(allowed_root):
            return False, f"yara_rules_path 必須位於 {allowed_root}"
    except Exception:
        return False, "yara_rules_path 格式錯誤"
    return True, "ok"


def update_cloud_drive_security_policy(conn, data, scope="default"):
    ensure_upload_security_schema(conn)
    if not isinstance(data, dict):
        return None, "請求內容格式錯誤"
    updates = []
    params = []
    for key in CLOUD_DRIVE_POLICY_BOOL_FIELDS:
        if key in data:
            updates.append(f"{key}=?")
            params.append(1 if bool(data[key]) else 0)
    for key in CLOUD_DRIVE_POLICY_INT_FIELDS:
        if key in data:
            try:
                value = int(data[key])
            except Exception:
                return None, f"{key} 格式錯誤"
            if value < 0:
                return None, f"{key} 不可小於 0"
            if key == "scanner_timeout_seconds" and value < 1:
                return None, "scanner_timeout_seconds 必須至少 1 秒"
            if key == "max_archive_depth" and value > 5:
                return None, "max_archive_depth 不可大於 5"
            if key == "image_reencode_max_pixels" and value > 100_000_000:
                return None, "image_reencode_max_pixels 不可大於 100000000"
            updates.append(f"{key}=?")
            params.append(value)
    if "scanner_backend" in data:
        value = str(data.get("scanner_backend") or "").strip().lower()
        if value not in ALLOWED_SCANNER_BACKENDS:
            return None, "scanner_backend 僅支援 disabled 或 clamav"
        updates.append("scanner_backend=?")
        params.append(value)
    if "scanner_command" in data:
        scanner_command = str(data.get("scanner_command") or "").strip()
        if scanner_command and scanner_command not in ALLOWED_CLAMAV_COMMANDS:
            return None, "scanner_command 僅可為 clamdscan 或 clamscan，不能填路徑或參數"
        updates.append("scanner_command=?")
        params.append(scanner_command)
    if "yara_command" in data:
        yara_command = str(data.get("yara_command") or "").strip()
        if yara_command and yara_command not in ALLOWED_YARA_COMMANDS:
            return None, "yara_command 僅可為 yara，不能填路徑或參數"
        updates.append("yara_command=?")
        params.append(yara_command)
    if "yara_rules_path" in data:
        rules_path = str(data.get("yara_rules_path") or "").strip()
        if rules_path:
            ok, reason = _validate_yara_rules_path(rules_path)
            if not ok:
                return None, reason
        updates.append("yara_rules_path=?")
        params.append(rules_path[:500])
    if "notes" in data:
        updates.append("notes=?")
        params.append(str(data.get("notes") or "")[:1000])
    if not updates:
        return None, "未提供可更新欄位"
    updates.append("updated_at=?")
    params.append(datetime.now().isoformat())
    params.append(scope or "default")
    conn.execute(f"UPDATE cloud_drive_security_policies SET {', '.join(updates)} WHERE scope=?", tuple(params))
    row = conn.execute("SELECT * FROM cloud_drive_security_policies WHERE scope=?", (scope or "default",)).fetchone()
    return serialize_cloud_drive_security_policy(row), None


def _sum_uploaded_file_bytes(conn, owner_user_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS used_bytes, COUNT(*) AS file_count "
        "FROM uploaded_files "
        "WHERE owner_user_id=? AND deleted_at IS NULL "
        "AND COALESCE(system_asset_type, '')<>'avatar'",
        (int(owner_user_id),),
    ).fetchone()
    return int(row["used_bytes"] or 0), int(row["file_count"] or 0)


def _count_grouped(conn, owner_user_id, field):
    if field not in {"privacy_mode", "risk_level", "scan_status"}:
        raise ValueError("unsupported usage grouping")
    rows = conn.execute(
        f"SELECT {field} AS name, COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes "
        "FROM uploaded_files "
        "WHERE owner_user_id=? AND deleted_at IS NULL "
        "AND COALESCE(system_asset_type, '')<>'avatar' GROUP BY "
        f"{field}",
        (int(owner_user_id),),
    ).fetchall()
    return {row["name"]: {"count": int(row["count"] or 0), "bytes": int(row["bytes"] or 0)} for row in rows}


def _media_derivative_usage_excluded_from_quota(conn, owner_user_id):
    try:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(seg.byte_size), 0) AS bytes,
                COUNT(DISTINCT variant.id) AS variant_count,
                COUNT(DISTINCT asset.id) AS asset_count
            FROM media_stream_segments seg
            JOIN media_stream_variants variant ON variant.id = seg.variant_id
            JOIN media_stream_assets asset ON asset.id = variant.asset_id
            JOIN uploaded_files file ON file.id = asset.uploaded_file_id
            WHERE file.owner_user_id=? AND file.deleted_at IS NULL
            """,
            (int(owner_user_id),),
        ).fetchone()
    except Exception:
        return {
            "bytes": 0,
            "variant_count": 0,
            "asset_count": 0,
            "available": False,
        }
    return {
        "bytes": int(row["bytes"] or 0) if row else 0,
        "variant_count": int(row["variant_count"] or 0) if row else 0,
        "asset_count": int(row["asset_count"] or 0) if row else 0,
        "available": True,
    }


def _e2ee_stream_usage_excluded_from_quota(conn, owner_user_id):
    try:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(asset.source_size_bytes), 0) AS bytes,
                COUNT(*) AS asset_count
            FROM media_e2ee_stream_v2_assets asset
            JOIN uploaded_files file ON file.id = asset.uploaded_file_id
            WHERE file.owner_user_id=? AND file.deleted_at IS NULL
            """,
            (int(owner_user_id),),
        ).fetchone()
    except Exception:
        return {
            "bytes": 0,
            "asset_count": 0,
            "available": False,
        }
    return {
        "bytes": int(row["bytes"] or 0) if row else 0,
        "asset_count": int(row["asset_count"] or 0) if row else 0,
        "available": True,
    }


def _disk_usage_for_storage_root(storage_root):
    path = Path(storage_root or ".").expanduser()
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(str(probe))
    return {
        "path": str(path),
        "probe_path": str(probe),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
    }


def get_user_cloud_drive_usage(conn, user, member_rule=None, storage_root=None, ensure_schema=True):
    if ensure_schema:
        ensure_upload_security_schema(conn)
    data = dict(user or {})
    user_id = int(data.get("id") or 0)
    capacity_probe_unlimited = str(os.environ.get("HACKME_CAPACITY_PROBE_UNLIMITED") or "").strip().lower() in {"1", "true", "yes", "on"}
    admin_actor = _user_is_admin(data)
    root_actor = _user_is_root(data)
    manager_quota_actor = admin_actor and not root_actor
    effective_level = data.get("effective_level") or data.get("member_level") or "newbie"
    sanction_status = data.get("sanction_status") or "none"
    rule = member_rule or {}
    quota_mb = int(rule.get("attachment_quota_mb") or 0)
    max_file_size_mb = int(rule.get("max_attachment_size_mb") or 0)
    upload_rate_limit_per_day = int(rule.get("upload_rate_limit_per_day") or 0)
    can_upload = admin_actor or (bool(rule.get("can_upload_attachment")) and sanction_status not in {"restricted", "suspended"})
    try:
        mode_row = conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()
        server_mode = str(mode_row["current_mode"] or "test").strip().lower() if mode_row else "test"
    except Exception:
        server_mode = "test"

    used_bytes, file_count = _sum_uploaded_file_bytes(conn, user_id)
    media_derivative_usage = _media_derivative_usage_excluded_from_quota(conn, user_id)
    e2ee_stream_usage = _e2ee_stream_usage_excluded_from_quota(conn, user_id)
    service_cache_bytes_excluded = int(media_derivative_usage.get("bytes") or 0) + int(e2ee_stream_usage.get("bytes") or 0)
    purchased_summary = purchased_storage_summary(conn, user_id, ensure_schema=ensure_schema) if user_id and not root_actor else {
        "purchased_extra_bytes": 0,
        "active_purchase_count": 0,
        "active_purchases": [],
        "latest_expires_at": None,
    }
    purchased_extra_bytes = int(purchased_summary.get("purchased_extra_bytes") or 0)
    disk = _disk_usage_for_storage_root(storage_root) if root_actor and storage_root else None
    if disk:
        capacity_limit = resolve_global_capacity_limit(disk)
        total_bytes = capacity_limit["limit_bytes"]
        base_quota_bytes = total_bytes
        quota_source = (
            "root_global_capacity_limit_mb"
            if capacity_limit.get("limit_source") == "root_setting"
            else "root_disk_total_95_percent"
        )
    elif manager_quota_actor:
        base_quota_bytes = MANAGER_CLOUD_DRIVE_QUOTA_BYTES
        total_bytes = base_quota_bytes + purchased_extra_bytes
        quota_source = "manager_role_fixed_1gb"
    elif root_actor:
        total_bytes = None
        base_quota_bytes = None
        quota_source = "root_role_unlimited_no_storage_root"
    else:
        base_quota_bytes = quota_mb * 1024 * 1024
        total_bytes = base_quota_bytes + purchased_extra_bytes
        quota_source = "member_level_rules.attachment_quota_mb"
    if purchased_extra_bytes and not root_actor:
        quota_source = f"{quota_source}+storage_purchase"
    remaining_bytes = None if total_bytes is None else max(0, total_bytes - used_bytes)
    max_file_size_bytes = remaining_bytes if (disk or manager_quota_actor) else (None if root_actor else max_file_size_mb * 1024 * 1024)
    rate_limit = None if admin_actor else upload_rate_limit_per_day
    percent_used = 0.0
    if total_bytes and total_bytes > 0:
        percent_used = min(100.0, round((used_bytes / total_bytes) * 100, 2))
    elif total_bytes == 0 and used_bytes > 0:
        percent_used = 100.0

    usage = {
        "user_id": user_id,
        "effective_level": effective_level,
        "can_upload": can_upload,
        "quota_source": quota_source,
        "base_quota_bytes": base_quota_bytes,
        "purchased_extra_bytes": purchased_extra_bytes,
        "purchased_storage": purchased_summary,
        "used_bytes": used_bytes,
        "quota_counted_bytes": used_bytes,
        "service_cache_bytes_excluded_from_quota": service_cache_bytes_excluded,
        "service_cache_quota_exempt": True,
        "media_derivative_bytes_excluded_from_quota": int(media_derivative_usage.get("bytes") or 0),
        "media_derivative_variants_excluded_from_quota": int(media_derivative_usage.get("variant_count") or 0),
        "media_derivative_assets_excluded_from_quota": int(media_derivative_usage.get("asset_count") or 0),
        "e2ee_stream_bytes_excluded_from_quota": int(e2ee_stream_usage.get("bytes") or 0),
        "e2ee_stream_assets_excluded_from_quota": int(e2ee_stream_usage.get("asset_count") or 0),
        "quota_exemption_note": "雲端硬碟配額只計算原始上傳檔案；HLS 480/720/1080 串流衍生檔與 E2EE Streaming v2 密文分段屬服務快取，不計入用戶雲端硬碟容量。",
        "total_bytes": total_bytes,
        "remaining_bytes": remaining_bytes,
        "percent_used": percent_used,
        "file_count": file_count,
        "max_file_size_bytes": max_file_size_bytes,
        "upload_rate_limit_per_day": rate_limit,
        "disk": disk,
        "warning_threshold_percent": int(ADMIN_DISK_WARNING_RATIO * 100) if disk else None,
        "warning_threshold_bytes": int(total_bytes * ADMIN_DISK_WARNING_RATIO) if disk and total_bytes is not None else None,
        "warning_active": bool(disk and total_bytes is not None and used_bytes >= int(total_bytes * ADMIN_DISK_WARNING_RATIO)),
        "by_privacy_mode": _count_grouped(conn, user_id, "privacy_mode"),
        "by_risk_level": _count_grouped(conn, user_id, "risk_level"),
        "by_scan_status": _count_grouped(conn, user_id, "scan_status"),
    }
    usage = apply_storage_quota_override(
        usage,
        get_storage_quota_override(conn, user_id, ensure_schema=ensure_schema),
    )
    if capacity_probe_unlimited:
        usage.update({
            "can_upload": True,
            "upload_rate_limit_per_day": None,
        })
    if server_mode == "superweak":
        total_bytes = SUPERWEAK_CLOUD_DRIVE_QUOTA_BYTES
        remaining_bytes = max(0, total_bytes - used_bytes)
        usage.update({
            "quota_source": "server_mode_superweak_forced_10mb",
            "base_quota_bytes": total_bytes,
            "purchased_extra_bytes": 0,
            "total_bytes": total_bytes,
            "remaining_bytes": remaining_bytes,
            "max_file_size_bytes": remaining_bytes,
            "warning_threshold_percent": None,
            "warning_threshold_bytes": None,
            "warning_active": False,
            "percent_used": min(100.0, round((used_bytes / total_bytes) * 100, 2)) if total_bytes else 0.0,
        })
    return usage


def storage_root_can_accept_bytes(storage_root, size_bytes):
    if not storage_root:
        return True, None
    disk = _disk_usage_for_storage_root(storage_root)
    safe_free = int(disk["free_bytes"] * ADMIN_DISK_QUOTA_RATIO)
    if int(size_bytes or 0) > safe_free:
        return False, {
            **disk,
            "safe_free_bytes": safe_free,
            "safety_ratio": ADMIN_DISK_QUOTA_RATIO,
        }
    return True, {
        **disk,
        "safe_free_bytes": safe_free,
        "safety_ratio": ADMIN_DISK_QUOTA_RATIO,
    }


def get_cloud_drive_safety_summary(conn, user, member_rule=None, storage_root=None):
    policy = get_cloud_drive_security_policy(conn)
    usage = get_user_cloud_drive_usage(conn, user, member_rule=member_rule, storage_root=storage_root)
    effective_level = usage["effective_level"]
    restrictions = []
    if not usage["can_upload"]:
        restrictions.append("目前會員等級或處分狀態不可上傳")
    if policy["block_unclean_downloads"]:
        restrictions.append("public/private 檔案需掃描為 clean 才能下載")
    if policy["warn_high_risk_downloads"]:
        restrictions.append("high / unknown_encrypted 下載前必須顯示風險警告")
    if not policy["allow_inline_preview_for_high_risk"]:
        restrictions.append("高風險檔案不提供 inline preview")
    if not policy["e2ee_server_scan_claim_allowed"]:
        restrictions.append("E2EE 檔案不可宣稱已完成伺服器完整掃毒")
    if not _user_is_admin(user) and effective_level in {"restricted", "suspended"}:
        restrictions.append("restricted/suspended 不可新增上傳或分享")
    if usage.get("warning_active"):
        restrictions.append("root 雲端硬碟使用量已超過磁碟安全警示線 80%，請清理檔案或擴充儲存空間")

    modes = {
        "standard_plain": "一般檔案，伺服器可讀明文並掃描，預覽/分享支援最完整",
        "server_encrypted": "伺服器端加密保存，磁碟上是密文；伺服器可暫時解密掃描、預覽與下載",
        "e2ee": "端到端加密，server/root/admin 不可讀；掃毒、預覽與救援受限",
    }
    return {
        "policy": policy,
        "usage": usage,
        "modes": modes,
        "restrictions": restrictions,
    }


def _mapping_value(mapping, key, default=None):
    if not mapping:
        return default
    try:
        return mapping[key]
    except Exception:
        return mapping.get(key, default) if hasattr(mapping, "get") else default


def _user_role(user):
    username = _mapping_value(user, "username")
    if username == "root":
        return "super_admin"
    return _mapping_value(user, "role", "user") or "user"


def _user_is_root(user):
    return _mapping_value(user, "username") == "root"


def _user_is_admin(user):
    return is_admin_role(_user_role(user))


def evaluate_upload_policy(conn, *, filename, privacy_mode, user=None, size_bytes=0):
    mode = normalize_privacy_mode(privacy_mode)
    ext = file_extension(filename)
    category = policy_category_for_extension(ext)
    policy = get_file_type_policy(conn, category)
    warnings = []
    reason = "allowed"
    risk_level = policy["default_risk_level"] if policy["default_risk_level"] in RISK_LEVELS else "medium"

    if mode == "standard_plain" and not (policy["public_allowed"] and policy["server_readable_allowed"]):
        return UploadPolicyDecision(False, mode, "blocked", "quarantined", category, "file type is blocked for standard plaintext uploads", tuple(warnings))
    if mode == "server_encrypted" and not policy["server_readable_allowed"]:
        return UploadPolicyDecision(False, mode, "blocked", "quarantined", category, "file type is blocked for server-side encrypted uploads", tuple(warnings))
    if mode == "e2ee" and not policy["e2ee_allowed"]:
        return UploadPolicyDecision(False, mode, "blocked", "quarantined", category, "file type is blocked for encrypted vault uploads", tuple(warnings))

    if not _user_is_admin(user):
        effective_level = str(_mapping_value(user, "effective_level") or _mapping_value(user, "member_level") or "newbie")
        if effective_level in {"restricted", "suspended"}:
            return UploadPolicyDecision(False, mode, "blocked", "quarantined", category, f"{effective_level} users cannot upload", tuple(warnings))
        if effective_level == "newbie" and category in {"executable", "archive", "office_macro"}:
            return UploadPolicyDecision(False, mode, "blocked", "quarantined", category, "newbie users cannot upload this high-risk file type", tuple(warnings))

    if mode == "e2ee":
        risk_level = "unknown_encrypted" if category != "executable" else "high"
        scan_status = "unknown_encrypted"
        warnings.append("server cannot fully scan end-to-end encrypted content")
        if category in {"archive", "office_macro", "executable"}:
            warnings.append("download must show high-risk warning")
        return UploadPolicyDecision(True, mode, risk_level, scan_status, category, reason, tuple(warnings))

    scan_status = "pending" if policy["requires_scan"] else "not_required"
    if category in {"archive", "office_macro", "executable"}:
        warnings.append("requires strict scan before download")
    if size_bytes and int(size_bytes) > 50 * 1024 * 1024:
        risk_level = "medium" if risk_level == "low" else risk_level
        warnings.append("large file requires quota and rate-limit checks")
    return UploadPolicyDecision(True, mode, risk_level, scan_status, category, reason, tuple(warnings))
