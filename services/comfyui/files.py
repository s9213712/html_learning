"""ComfyUI file upload/fetch/discard helpers."""

import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


_COMFYUI_FILE_TYPES = {"output", "input", "temp"}
_COMFYUI_VIEW_FALLBACK_TYPES = ("output", "temp")


def _upload_timeout_seconds(default_timeout):
    try:
        configured = float(os.environ.get("HACKME_COMFYUI_UPLOAD_TIMEOUT_SECONDS", "0") or 0)
    except Exception:
        configured = 0.0
    if configured > 0:
        return max(5.0, min(configured, 600.0))
    try:
        base = float(default_timeout or 30)
    except Exception:
        base = 30.0
    return max(60.0, min(base, 600.0))


def _safe_path_parts(value, *, field_name, error_cls):
    raw = str(value or "").strip().replace("\\", "/")
    if "\x00" in raw:
        raise error_cls(f"ComfyUI {field_name}不合法")
    if not raw:
        return []
    if raw.startswith("/"):
        raise error_cls(f"ComfyUI {field_name}不合法")
    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if any(part in {".", ".."} or ":" in part for part in parts):
        raise error_cls(f"ComfyUI {field_name}不合法")
    return parts


def normalize_file_ref(file_ref, *, error_cls=ValueError, default_type="output", empty_label="ComfyUI 檔案"):
    if not isinstance(file_ref, dict):
        raise error_cls(f"{empty_label}引用格式錯誤")
    filename_parts = _safe_path_parts(file_ref.get("filename"), field_name="檔名", error_cls=error_cls)
    if not filename_parts:
        raise error_cls(f"缺少 {empty_label}檔名")
    subfolder_parts = _safe_path_parts(file_ref.get("subfolder"), field_name="子資料夾", error_cls=error_cls)
    image_type = str(file_ref.get("type") or default_type or "output").strip().lower() or "output"
    if image_type not in _COMFYUI_FILE_TYPES:
        raise error_cls(f"{empty_label}類型不支援")
    if len(filename_parts) > 1:
        subfolder_parts = [*subfolder_parts, *filename_parts[:-1]]
    filename = filename_parts[-1]
    return {
        "filename": filename,
        "subfolder": "/".join(subfolder_parts),
        "type": image_type,
    }


def _candidate_file_refs(file_ref):
    candidates = [file_ref]
    image_type = file_ref.get("type")
    if image_type in _COMFYUI_VIEW_FALLBACK_TYPES:
        for fallback_type in _COMFYUI_VIEW_FALLBACK_TYPES:
            if fallback_type == image_type:
                continue
            candidate = dict(file_ref)
            candidate["type"] = fallback_type
            candidates.append(candidate)
    return candidates


def _ref_description(file_ref):
    path = file_ref["filename"]
    if file_ref.get("subfolder"):
        path = f"{file_ref['subfolder']}/{path}"
    return f"type={file_ref.get('type') or 'output'}, file={path}"


def _open_view(client, file_ref, *, accept):
    query = urllib.parse.urlencode({
        "filename": file_ref["filename"],
        "subfolder": file_ref.get("subfolder") or "",
        "type": file_ref.get("type") or "output",
    })
    req = urllib.request.Request(client._url(f"/view?{query}"), headers={"Accept": accept})
    with urllib.request.urlopen(req, timeout=client.timeout) as resp:
        content_type = resp.headers.get("Content-Type") or "application/octet-stream"
        data = resp.read()
    return content_type, data


def upload_image_bytes(client, data, filename, *, image_type="input", overwrite=False, subfolder="", error_cls):
    filename = Path(str(filename or "upload.png")).name
    payload = client._multipart_request(
        "/upload/image",
        fields={
            "type": str(image_type or "input"),
            "overwrite": "true" if overwrite else "false",
            "subfolder": str(subfolder or ""),
        },
        files=[{
            "field": "image",
            "filename": filename,
            "content_type": "application/octet-stream",
            "data": data or b"",
        }],
        timeout=_upload_timeout_seconds(getattr(client, "timeout", 30)),
    )
    name = str(payload.get("name") or filename).strip()
    if not name:
        raise error_cls("ComfyUI 未回傳上傳檔名")
    return {
        "filename": name,
        "subfolder": str(payload.get("subfolder") or subfolder or "").strip(),
        "type": str(payload.get("type") or image_type or "input").strip() or "input",
    }


def fetch_file(client, file_ref, *, error_cls, file_cls, accept="*/*", empty_label="ComfyUI 檔案"):
    normalized_ref = normalize_file_ref(file_ref, error_cls=error_cls, empty_label=empty_label)
    not_found_exc = None
    fetched_ref = normalized_ref
    for candidate in _candidate_file_refs(normalized_ref):
        try:
            content_type, data = _open_view(client, candidate, accept=accept)
            fetched_ref = candidate
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                not_found_exc = exc
                continue
            reason = getattr(exc, "reason", "") or getattr(exc, "msg", "") or ""
            raise error_cls(f"{empty_label}讀取失敗：HTTP {exc.code} {reason}".strip()) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise error_cls(f"{empty_label}讀取逾時：遠端 ComfyUI 已產生檔案引用，但 /view 在逾時前沒有回傳檔案內容。請確認遠端 output 檔案存在、遠端磁碟/網路沒有卡住，或稍後重新整理預覽。") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise error_cls(f"{empty_label}讀取失敗：{reason}") from exc
    else:
        detail = _ref_description(normalized_ref)
        raise error_cls(
            f"{empty_label}讀取失敗：ComfyUI 回傳 404 Not Found（{detail}）。"
            "可能是輸出檔已被清理、目前後端不是產生該檔案的 ComfyUI，或 workflow 回傳的檔案類型/子資料夾與實際輸出不一致。"
        ) from not_found_exc
    if not data:
        raise error_cls(f"{empty_label}內容為空")
    return file_cls(
        filename=fetched_ref["filename"],
        subfolder=fetched_ref.get("subfolder") or "",
        type=fetched_ref.get("type") or "output",
        mime_type=content_type,
        data=data,
    )


def fetch_image(client, image_ref, *, error_cls, image_cls):
    image = fetch_file(
        client,
        image_ref,
        error_cls=error_cls,
        file_cls=image_cls,
        accept="image/*",
        empty_label="ComfyUI 圖片",
    )
    if not image.mime_type:
        image.mime_type = "image/png"
    return image


def local_dir_for_type(image_type, *, error_cls, local_base_dir=None):
    normalized = str(image_type or "output").strip().lower() or "output"
    if normalized not in {"output", "input", "temp"}:
        raise error_cls("ComfyUI 圖片類型不支援刪除")
    explicit = os.environ.get(f"COMFYUI_{normalized.upper()}_DIR")
    if explicit:
        return Path(explicit).expanduser()
    base_dir = local_base_dir or os.environ.get("COMFYUI_BASE_DIR")
    if base_dir:
        return Path(base_dir).expanduser() / normalized
    return None


def safe_local_image_path(image_ref, *, error_cls, local_base_dir=None):
    normalized_ref = normalize_file_ref(image_ref, error_cls=error_cls, empty_label="ComfyUI 圖片")
    filename = normalized_ref["filename"]
    subfolder = normalized_ref.get("subfolder") or ""
    image_type = normalized_ref.get("type") or "output"
    base_dir = local_dir_for_type(image_type, error_cls=error_cls, local_base_dir=local_base_dir)
    if not base_dir:
        return None
    relative = Path(subfolder) / filename if subfolder else Path(filename)
    if relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
        raise error_cls("ComfyUI 圖片路徑不合法")
    base = base_dir.resolve()
    target = (base / relative).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise error_cls("ComfyUI 圖片路徑超出允許目錄") from exc
    return target


def discard_image(client, image_ref, *, prompt_id=None, local_base_dir=None, allow_api_delete=True, error_cls):
    result = {
        "file_deleted": False,
        "file_missing": False,
        "file_delete_supported": False,
        "history_deleted": False,
    }
    target = safe_local_image_path(image_ref, error_cls=error_cls, local_base_dir=local_base_dir)
    if target:
        result["file_delete_supported"] = True
        if target.exists():
            if not target.is_file():
                raise error_cls("ComfyUI 目標路徑不是檔案")
            target.unlink()
            result["file_deleted"] = True
        else:
            result["file_missing"] = True
    elif allow_api_delete:
        normalized_ref = normalize_file_ref(image_ref, error_cls=error_cls, empty_label="ComfyUI 圖片")
        filename = normalized_ref["filename"]
        subfolder = normalized_ref.get("subfolder") or ""
        image_type = normalized_ref.get("type") or "output"
        query = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": image_type})
        req = urllib.request.Request(client._url(f"/view?{query}"), method="DELETE", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=client.timeout):
                result["file_delete_supported"] = True
                result["file_deleted"] = True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                result["file_delete_supported"] = True
                result["file_missing"] = True
            elif exc.code not in {405, 501}:
                raise error_cls(f"ComfyUI 原始檔刪除失敗：HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise error_cls(f"ComfyUI 原始檔刪除失敗：{getattr(exc, 'reason', exc)}") from exc
    if prompt_id:
        client._json_request("/history", method="POST", payload={"delete": [str(prompt_id)]}, allow_non_json=True)
        result["history_deleted"] = True
    return result
