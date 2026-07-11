import os
from pathlib import Path
from urllib.parse import quote

from flask import Response

from services.core.http_headers import build_content_disposition


_TRUTHY = {"1", "true", "yes", "on"}


def _first_env(*names):
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def x_accel_enabled():
    raw = _first_env("HACKME_CLOUD_DRIVE_X_ACCEL_ENABLED", "HACKME_X_ACCEL_ENABLED")
    if str(raw or "").strip().lower() in _TRUTHY:
        return True
    return bool(_first_env("HACKME_CLOUD_DRIVE_X_ACCEL_PREFIX", "HACKME_X_ACCEL_STORAGE_PREFIX"))


def x_accel_internal_uri(path, *, storage_root):
    """Return an Nginx X-Accel internal URI for a storage-local file."""
    if not x_accel_enabled():
        return ""
    prefix = _first_env("HACKME_CLOUD_DRIVE_X_ACCEL_PREFIX", "HACKME_X_ACCEL_STORAGE_PREFIX")
    if not prefix:
        return ""
    root_raw = _first_env("HACKME_CLOUD_DRIVE_X_ACCEL_STORAGE_ROOT", "HACKME_X_ACCEL_STORAGE_ROOT")
    try:
        root = Path(root_raw or storage_root).resolve()
        target = Path(path).resolve()
        rel = target.relative_to(root)
    except Exception:
        return ""
    clean_prefix = "/" + prefix.strip("/")
    return f"{clean_prefix}/{quote(rel.as_posix(), safe='/')}"


def x_accel_response(path, *, storage_root, as_attachment, download_name, mimetype=None):
    internal_uri = x_accel_internal_uri(path, storage_root=storage_root)
    if not internal_uri:
        return None
    response = Response(status=200, mimetype=mimetype or "application/octet-stream")
    response.headers["X-Accel-Redirect"] = internal_uri
    response.headers["X-Accel-Buffering"] = "yes"
    response.headers["X-Hackme-Transfer-Mode"] = "x_accel"
    response.headers["X-Hackme-Transfer-Offload"] = "x_accel"
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Disposition"] = build_content_disposition(
        "attachment" if as_attachment else "inline",
        download_name or Path(path).name or "download.bin",
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers.pop("Content-Length", None)
    return response
