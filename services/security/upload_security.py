"""Canonical upload security facade inside the security package.

This keeps the public upload-security API grouped under ``services.security``
while delegating the concrete logic to the smaller upload_* modules.
"""

import shutil

from services.security import upload_policy as _policy
from services.security import upload_records as _records
from services.security import upload_scanning as _scanning
from services.security import upload_schema as _schema


UNSAFE_PUBLIC_MIME_TYPES = _schema.UNSAFE_PUBLIC_MIME_TYPES
run_clamav_scan = _scanning.run_clamav_scan
run_yara_scan = _scanning.run_yara_scan
_resolve_clamav_command = _scanning._resolve_clamav_command

globals().update({
    "ADMIN_DISK_QUOTA_RATIO": _schema.ADMIN_DISK_QUOTA_RATIO,
    "ADMIN_DISK_WARNING_RATIO": _schema.ADMIN_DISK_WARNING_RATIO,
    "ALLOWED_CLAMAV_COMMANDS": _schema.ALLOWED_CLAMAV_COMMANDS,
    "ALLOWED_SCANNER_BACKENDS": _schema.ALLOWED_SCANNER_BACKENDS,
    "ALLOWED_YARA_COMMANDS": _schema.ALLOWED_YARA_COMMANDS,
    "ARCHIVE_EXTENSIONS": _schema.ARCHIVE_EXTENSIONS,
    "DEFAULT_CLOUD_DRIVE_SECURITY_POLICY": _schema.DEFAULT_CLOUD_DRIVE_SECURITY_POLICY,
    "DEFAULT_FILE_TYPE_POLICIES": _schema.DEFAULT_FILE_TYPE_POLICIES,
    "EXECUTABLE_EXTENSIONS": _schema.EXECUTABLE_EXTENSIONS,
    "MACRO_OFFICE_EXTENSIONS": _schema.MACRO_OFFICE_EXTENSIONS,
    "OFFICE_EXTENSIONS": _schema.OFFICE_EXTENSIONS,
    "REENCODABLE_IMAGE_EXTENSIONS": _schema.REENCODABLE_IMAGE_EXTENSIONS,
    "RISK_LEVELS": _schema.RISK_LEVELS,
    "SAFE_PUBLIC_MIME_TYPES": _schema.SAFE_PUBLIC_MIME_TYPES,
    "SCAN_STATUSES": _schema.SCAN_STATUSES,
    "SUPERWEAK_CLOUD_DRIVE_QUOTA_BYTES": _schema.SUPERWEAK_CLOUD_DRIVE_QUOTA_BYTES,
    "UPLOAD_PRIVACY_MODES": _schema.UPLOAD_PRIVACY_MODES,
    "UploadPolicyDecision": _schema.UploadPolicyDecision,
    "ensure_upload_security_schema": _schema.ensure_upload_security_schema,
    "seed_default_cloud_drive_security_policy": _schema.seed_default_cloud_drive_security_policy,
    "seed_default_file_type_policies": _schema.seed_default_file_type_policies,
    "canonical_privacy_mode": _policy.canonical_privacy_mode,
    "evaluate_upload_policy": _policy.evaluate_upload_policy,
    "file_extension": _policy.file_extension,
    "get_cloud_drive_safety_summary": _policy.get_cloud_drive_safety_summary,
    "get_cloud_drive_security_policy": _policy.get_cloud_drive_security_policy,
    "get_file_type_policy": _policy.get_file_type_policy,
    "is_e2ee_privacy_mode": _policy.is_e2ee_privacy_mode,
    "is_server_encrypted_privacy_mode": _policy.is_server_encrypted_privacy_mode,
    "normalize_privacy_mode": _policy.normalize_privacy_mode,
    "normalize_scan_status": _policy.normalize_scan_status,
    "policy_category_for_extension": _policy.policy_category_for_extension,
    "serialize_cloud_drive_security_policy": _policy.serialize_cloud_drive_security_policy,
    "storage_root_can_accept_bytes": _policy.storage_root_can_accept_bytes,
    "update_cloud_drive_security_policy": _policy.update_cloud_drive_security_policy,
    "check_magic_mime_safety": _scanning.check_magic_mime_safety,
    "check_office_macro_safety": _scanning.check_office_macro_safety,
    "check_zip_archive_safety": _scanning.check_zip_archive_safety,
    "detect_magic_mime": _scanning.detect_magic_mime,
    "log_file_access": _records.log_file_access,
    "reencode_image_strip_metadata": _scanning.reencode_image_strip_metadata,
    "scan_archive_members": _scanning.scan_archive_members,
    "sha256_file": _scanning.sha256_file,
})


def safe_public_filename(filename):
    return _policy.safe_public_filename(filename)


def safe_public_mime_type(filename=None, declared_mime=None):
    # Keep the old source-level contract visible for regression checks:
    # safe_public_mime_type(original_filename, mime_type)
    return _policy.safe_public_mime_type(filename, declared_mime)


def get_user_cloud_drive_usage(conn, user, member_rule=None, storage_root=None, ensure_schema=True):
    # Keep purchased_extra_bytes and +storage_purchase semantics in the policy layer.
    return _policy.get_user_cloud_drive_usage(
        conn,
        user,
        member_rule=member_rule,
        storage_root=storage_root,
        ensure_schema=ensure_schema,
    )


def scan_uploaded_file(conn, *, file_id, file_path, filename=None, declared_mime=None):
    _scanning._resolve_clamav_command = _resolve_clamav_command
    _scanning.run_clamav_scan = run_clamav_scan
    _scanning.run_yara_scan = run_yara_scan
    return _scanning.scan_uploaded_file(
        conn,
        file_id=file_id,
        file_path=file_path,
        filename=filename,
        declared_mime=declared_mime,
    )


def create_uploaded_file_record(
    conn,
    *,
    owner_user_id,
    storage_path,
    privacy_mode,
    size_bytes,
    original_filename=None,
    encrypted_metadata=None,
    encrypted_file_key=None,
    wrapped_by="user_public_key",
    mime_type=None,
    ciphertext_sha256=None,
    plaintext_sha256=None,
    encryption_algorithm=None,
    encryption_version=None,
    nonce=None,
    client_scan_report=None,
    system_asset_type=None,
    user=None,
    scan_now=False,
):
    _records.scan_uploaded_file = scan_uploaded_file
    _records.evaluate_upload_policy = _policy.evaluate_upload_policy
    _records.is_e2ee_privacy_mode = _policy.is_e2ee_privacy_mode
    _records.safe_public_filename = safe_public_filename
    _records.safe_public_mime_type = safe_public_mime_type
    return _records.create_uploaded_file_record(
        conn,
        owner_user_id=owner_user_id,
        storage_path=storage_path,
        privacy_mode=privacy_mode,
        size_bytes=size_bytes,
        original_filename=original_filename,
        encrypted_metadata=encrypted_metadata,
        encrypted_file_key=encrypted_file_key,
        wrapped_by=wrapped_by,
        mime_type=mime_type,
        ciphertext_sha256=ciphertext_sha256,
        plaintext_sha256=plaintext_sha256,
        encryption_algorithm=encryption_algorithm,
        encryption_version=encryption_version,
        nonce=nonce,
        client_scan_report=client_scan_report,
        system_asset_type=system_asset_type,
        user=user,
        scan_now=scan_now,
    )
