import gzip
import mimetypes
import tarfile
import zipfile
from pathlib import Path

TEXT_EXTENSIONS = {
    ".css", ".csv", ".htm", ".html", ".ini", ".js", ".json", ".log", ".md",
    ".c", ".cc", ".cpp", ".cs", ".go", ".java", ".jsx", ".php", ".py", ".rs",
    ".sh", ".sql", ".text", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".amr", ".flac", ".m4a", ".mid", ".midi",
    ".mp3", ".oga", ".ogg", ".opus", ".wav", ".weba",
}
VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ogv", ".webm", ".wmv"}
IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
PDF_EXTENSIONS = {".pdf"}
ARCHIVE_EXTENSIONS = {
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".tar.gz", ".tar.bz2",
    ".tbz2", ".tar.xz", ".txz",
}


def _filename(row):
    return row["original_filename_plain_for_public"] or Path(str(row["storage_path"] or "download.bin")).name


def _extension(filename):
    lower = str(filename or "").lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lower.endswith(ext):
            return ext
    return Path(lower).suffix


def _mime(row, filename):
    value = row["mime_type_plain_for_public"] if "mime_type_plain_for_public" in row.keys() else None
    if value:
        return value
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _display_mime(mime, filename):
    if mime and mime != "application/octet-stream":
        return mime
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or mime or "application/octet-stream"


def preview_category(row):
    filename = _filename(row)
    ext = _extension(filename)
    mime = _mime(row, filename)
    # SVG is active XML content. Show its source as inert text and never feed
    # an untrusted upload to an inline image/document browsing context.
    if ext == ".svg" or mime.split(";", 1)[0].strip().lower() == "image/svg+xml":
        return "text", "text/plain"
    if mime.startswith("audio/") or ext in AUDIO_EXTENSIONS:
        return "audio", _display_mime(mime, filename)
    if mime.startswith("video/") or ext in VIDEO_EXTENSIONS:
        return "video", _display_mime(mime, filename)
    if mime.startswith("image/") or ext in IMAGE_EXTENSIONS:
        return "image", _display_mime(mime, filename)
    if mime == "application/pdf" or ext in PDF_EXTENSIONS:
        return "pdf", "application/pdf"
    if ext in ARCHIVE_EXTENSIONS:
        return "archive", mime
    if mime.startswith("text/") or ext in TEXT_EXTENSIONS or not ext:
        return "text", mime if mime != "application/octet-stream" else "text/plain"
    return "metadata", mime


def build_preview_metadata(row, path, *, max_text_bytes=65536, max_archive_entries=100):
    filename = _filename(row)
    category, mime = preview_category(row)
    payload = {
        "file_id": row["id"],
        "filename": filename,
        "size_bytes": int(row["size_bytes"] or 0),
        "privacy_mode": row["privacy_mode"],
        "risk_level": row["risk_level"],
        "scan_status": row["scan_status"],
        "category": category,
        "mime_type": mime,
        "render_mode": "metadata",
        "previewable": category in {"audio", "video", "image", "pdf", "text", "archive"},
    }
    if category in {"audio", "video", "image", "pdf"}:
        payload["render_mode"] = "media"
        return payload
    if category == "text":
        payload["render_mode"] = "text"
        payload["truncated"] = int(row["size_bytes"] or 0) > max_text_bytes
        with open(path, "rb") as handle:
            raw = handle.read(max_text_bytes)
        payload["text"] = raw.decode("utf-8", errors="replace")
        return payload
    if category == "archive":
        payload["render_mode"] = "archive"
        payload["entries"] = _archive_entries(path, filename=filename, max_entries=max_archive_entries)
        payload["truncated"] = len(payload["entries"]) >= max_archive_entries
        return payload
    return payload


def _archive_entries(path, *, filename=None, max_entries):
    entries = []
    lower = str(filename or path).lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist()[:max_entries]:
                    entries.append({
                        "name": info.filename,
                        "size": int(info.file_size or 0),
                        "compressed_size": int(info.compress_size or 0),
                        "is_dir": info.is_dir(),
                    })
        elif any(lower.endswith(ext) for ext in (".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            with tarfile.open(path) as archive:
                for member in archive.getmembers()[:max_entries]:
                    entries.append({
                        "name": member.name,
                        "size": int(member.size or 0),
                        "compressed_size": None,
                        "is_dir": member.isdir(),
                    })
        elif lower.endswith(".gz"):
            with gzip.open(path, "rb") as archive:
                sample = archive.read(1)
            inferred_name = Path(str(path)).name.removesuffix(".gz") or Path(str(path)).name
            entries.append({
                "name": inferred_name,
                "size": None,
                "compressed_size": int(Path(path).stat().st_size),
                "is_dir": False,
                "note": "gzip stream preview",
                "readable": bool(sample or Path(path).stat().st_size == 0),
            })
        elif lower.endswith((".rar", ".7z")):
            entries.append({
                "name": Path(str(path)).name,
                "size": int(Path(path).stat().st_size),
                "compressed_size": int(Path(path).stat().st_size),
                "is_dir": False,
                "note": "此壓縮格式需額外工具才可列出內容；目前提供檔案層級預覽。",
            })
    except Exception as exc:
        return [{"name": f"archive_preview_error: {exc}", "size": 0, "compressed_size": None, "is_dir": False}]
    return entries
