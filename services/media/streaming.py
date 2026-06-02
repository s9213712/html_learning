import json
import mimetypes
import os
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

try:
    import fcntl
except Exception:  # pragma: no cover - non-Unix fallback
    fcntl = None

from services.storage.cloud_drive import (
    is_e2ee_file,
    is_server_encrypted_file,
    resolve_file_storage_path,
    write_decrypted_server_encrypted_file,
)
from services.storage.paths import resolve_storage_path


MEDIA_STREAM_ASSET_STATUSES = {"pending", "processing", "ready", "failed", "unavailable"}
MEDIA_STREAM_STORAGE_MODE = "acl_protected_plain"
HLS_JS_URL = "/js/hls.light.min.js?v=20260505-hlsjs"
DEFAULT_HLS_SEGMENT_SECONDS = 4
DEFAULT_STREAM_AUTO_PREPARE_AUDIO_MIN_BYTES = 25 * 1024 * 1024
DEFAULT_FFMPEG_THREADS = 1
DEFAULT_FFMPEG_TIMEOUT_SECONDS = 60 * 60
DEFAULT_SUBTITLE_EXTRACT_TIMEOUT_SECONDS = 90
DEFAULT_FFMPEG_PRESET = "ultrafast"
DEFAULT_FFMPEG_MAX_VIDEO_HEIGHT = 0
DEFAULT_HLS_QUALITY_HEIGHTS = "480,720"
DEFAULT_HLS_AUDIO_BITRATE = "160k"
DEFAULT_STREAM_AUDIO_TRACK_LIMIT = 8
DEFAULT_STREAM_SUBTITLE_TRACK_LIMIT = 32
DEFAULT_REALTIME_PROXY_MAX_CONCURRENT = 2
DEFAULT_REALTIME_PROXY_TIMEOUT_SECONDS = 4 * 60 * 60
DEFAULT_REALTIME_PROXY_AUDIO_BITRATE = "160k"
HLS_DERIVATIVES_QUOTA_EXEMPT = True
STRICT_E2EE_SERVER_TRANSCODE_DISABLED_REASON = (
    "strict E2EE files cannot generate server-side HLS or server-side transcode derivatives"
)

_REALTIME_PROXY_LOCK = threading.Lock()
_REALTIME_PROXY_ACTIVE = 0
_REALTIME_PROXY_HELD_SLOTS = set()


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


def _table_columns(conn, table):
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _ensure_columns(conn, table, definitions):
    columns = _table_columns(conn, table)
    for column, ddl in definitions.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_media_stream_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_stream_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uploaded_file_id TEXT NOT NULL UNIQUE REFERENCES uploaded_files(id) ON DELETE CASCADE,
            source_mode TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'video',
            status TEXT NOT NULL DEFAULT 'pending',
            storage_mode TEXT NOT NULL DEFAULT 'acl_protected_plain',
            master_manifest_path TEXT,
            duration_seconds REAL NOT NULL DEFAULT 0,
            source_mime_type TEXT,
            source_size_bytes INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (status IN ('pending', 'processing', 'ready', 'failed', 'unavailable'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_stream_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES media_stream_assets(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            media_kind TEXT NOT NULL DEFAULT 'variant',
            label TEXT,
            language TEXT,
            stream_index INTEGER NOT NULL DEFAULT -1,
            is_default INTEGER NOT NULL DEFAULT 0,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            bitrate INTEGER NOT NULL DEFAULT 0,
            codec TEXT,
            playlist_path TEXT NOT NULL,
            init_segment_path TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_stream_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id INTEGER NOT NULL REFERENCES media_stream_variants(id) ON DELETE CASCADE,
            sequence_number INTEGER NOT NULL DEFAULT 0,
            filename TEXT NOT NULL,
            path TEXT NOT NULL,
            duration_seconds REAL NOT NULL DEFAULT 0,
            byte_size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_stream_subtitles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES media_stream_assets(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            label TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'und',
            codec TEXT,
            path TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            is_forced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_stream_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES media_stream_assets(id) ON DELETE CASCADE,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT,
            finished_at TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    _ensure_columns(conn, "media_stream_assets", {"media_type": "TEXT NOT NULL DEFAULT 'video'"})
    _ensure_columns(conn, "media_stream_variants", {
        "media_kind": "TEXT NOT NULL DEFAULT 'variant'",
        "label": "TEXT",
        "language": "TEXT",
        "stream_index": "INTEGER NOT NULL DEFAULT -1",
        "is_default": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(conn, "media_stream_subtitles", {
        "is_forced": "INTEGER NOT NULL DEFAULT 0",
    })
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_stream_assets_status ON media_stream_assets(status, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_stream_variants_asset ON media_stream_variants(asset_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_stream_variants_asset_kind ON media_stream_variants(asset_id, media_kind, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_stream_segments_variant_seq ON media_stream_segments(variant_id, sequence_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_stream_subtitles_asset ON media_stream_subtitles(asset_id, name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_stream_jobs_asset ON media_stream_jobs(asset_id, created_at)")


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _safe_commit(conn):
    try:
        conn.commit()
    except Exception:
        pass


def _bounded_env_int(name, default, *, min_value, max_value):
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None else int(default)
    except Exception:
        value = int(default)
    return max(int(min_value), min(int(max_value), value))


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _ffmpeg_thread_count():
    return _bounded_env_int("HACKME_MEDIA_FFMPEG_THREADS", DEFAULT_FFMPEG_THREADS, min_value=1, max_value=4)


def _ffmpeg_preset():
    allowed = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}
    value = str(os.environ.get("HACKME_MEDIA_FFMPEG_PRESET") or DEFAULT_FFMPEG_PRESET).strip().lower()
    return value if value in allowed else DEFAULT_FFMPEG_PRESET


def _ffmpeg_copy_first_enabled():
    raw = os.environ.get("HACKME_MEDIA_HLS_COPY_FIRST", "1")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _ffmpeg_copy_fallback_transcode_enabled():
    raw = os.environ.get("HACKME_MEDIA_HLS_COPY_FALLBACK_TRANSCODE", "1")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _ffmpeg_force_stream_copy_enabled():
    raw = os.environ.get("HACKME_MEDIA_HLS_FORCE_COPY", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _ffmpeg_max_video_height():
    return _bounded_env_int(
        "HACKME_MEDIA_FFMPEG_MAX_VIDEO_HEIGHT",
        DEFAULT_FFMPEG_MAX_VIDEO_HEIGHT,
        min_value=0,
        max_value=2160,
    )


def _hls_profile():
    raw = str(os.environ.get("HACKME_MEDIA_HLS_PROFILE") or "").strip().lower().replace("-", "_")
    if raw in {"storage", "storage_saver", "storage_saving", "saver"}:
        return "storage_saver"
    if raw in {"mobile", "mobile_saver", "mobile_storage_saver", "low_cost", "lowcost"}:
        return "mobile_saver"
    return "full"


def _hls_default_quality_heights():
    if _hls_profile() == "mobile_saver":
        return "480"
    return DEFAULT_HLS_QUALITY_HEIGHTS


def _hls_quality_heights():
    raw = str(os.environ.get("HACKME_MEDIA_HLS_QUALITY_HEIGHTS") or _hls_default_quality_heights())
    values = []
    for part in raw.replace(";", ",").split(","):
        try:
            height = int(str(part or "").strip().lower().replace("p", ""))
        except Exception:
            continue
        if height in {2160, 1440, 1080, 720, 480, 360} and height not in values:
            values.append(height)
    return values


def _hls_original_variant_mode():
    raw = str(os.environ.get("HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE") or "").strip().lower()
    if not raw and not _env_bool("HACKME_MEDIA_HLS_INCLUDE_ORIGINAL", True):
        raw = "never"
    if not raw and _hls_profile() in {"storage_saver", "mobile_saver"}:
        raw = "never"
    normalized = raw.replace("-", "_")
    if normalized in {"never", "skip", "omit", "off", "false", "0", "storage_saver", "storage"}:
        return "never"
    if normalized in {"auto", "adaptive"}:
        return "auto"
    return "always"


def _audio_bitrate_value(raw, default):
    text = str(raw or default).strip().lower()
    if re.fullmatch(r"[1-9][0-9]{1,3}k", text):
        return text
    return str(default)


def _audio_bitrate_to_bits_per_second(value):
    text = _audio_bitrate_value(value, DEFAULT_HLS_AUDIO_BITRATE)
    try:
        return int(text[:-1]) * 1000
    except Exception:
        return 160000


def _hls_audio_bitrate():
    default = "128k" if _hls_profile() == "mobile_saver" else DEFAULT_HLS_AUDIO_BITRATE
    return _audio_bitrate_value(os.environ.get("HACKME_MEDIA_HLS_AUDIO_BITRATE"), default)


def _hls_effective_profile():
    raw_profile = str(os.environ.get("HACKME_MEDIA_HLS_PROFILE") or "").strip()
    if raw_profile:
        return _hls_profile()
    default_heights = [
        int(part)
        for part in DEFAULT_HLS_QUALITY_HEIGHTS.split(",")
        if str(part or "").strip().isdigit()
    ]
    if (
        _hls_original_variant_mode() != "always"
        or _hls_quality_heights() != default_heights
        or _hls_audio_bitrate() != DEFAULT_HLS_AUDIO_BITRATE
    ):
        return "custom"
    return "full"


def _ffmpeg_maxrate_multiplier():
    raw = os.environ.get("HACKME_MEDIA_HLS_MAXRATE_MULTIPLIER")
    try:
        value = float(raw) if raw is not None else 1.15
    except Exception:
        value = 1.15
    return max(1.0, min(2.0, value))


def _ffmpeg_bufsize_multiplier():
    raw = os.environ.get("HACKME_MEDIA_HLS_BUFSIZE_MULTIPLIER")
    try:
        value = float(raw) if raw is not None else 2.0
    except Exception:
        value = 2.0
    return max(1.0, min(4.0, value))


def _ffmpeg_timeout_seconds():
    return _bounded_env_int(
        "HACKME_MEDIA_FFMPEG_TIMEOUT_SECONDS",
        DEFAULT_FFMPEG_TIMEOUT_SECONDS,
        min_value=60,
        max_value=24 * 60 * 60,
    )


def _subtitle_extract_timeout_seconds():
    return _bounded_env_int(
        "HACKME_MEDIA_SUBTITLE_EXTRACT_TIMEOUT_SECONDS",
        DEFAULT_SUBTITLE_EXTRACT_TIMEOUT_SECONDS,
        min_value=10,
        max_value=_ffmpeg_timeout_seconds(),
    )


def realtime_proxy_enabled():
    return _env_bool("HACKME_MEDIA_REALTIME_PROXY_ENABLED", True)


def realtime_proxy_max_concurrent():
    return _bounded_env_int(
        "HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT",
        DEFAULT_REALTIME_PROXY_MAX_CONCURRENT,
        min_value=1,
        max_value=16,
    )


def realtime_proxy_timeout_seconds():
    return _bounded_env_int(
        "HACKME_MEDIA_REALTIME_PROXY_TIMEOUT_SECONDS",
        DEFAULT_REALTIME_PROXY_TIMEOUT_SECONDS,
        min_value=30,
        max_value=24 * 60 * 60,
    )


def realtime_proxy_audio_bitrate():
    return _audio_bitrate_value(os.environ.get("HACKME_MEDIA_REALTIME_PROXY_AUDIO_BITRATE"), DEFAULT_REALTIME_PROXY_AUDIO_BITRATE)


def realtime_proxy_lock_dir():
    raw = str(os.environ.get("HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR") or "").strip()
    if raw:
        return Path(raw)
    runtime_root = str(os.environ.get("HACKME_RUNTIME_DIR") or "").strip()
    if runtime_root:
        return Path(runtime_root) / "locks" / "realtime_proxy"
    return None


def realtime_proxy_slot_scope():
    raw = str(
        os.environ.get("HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE")
        or os.environ.get("HACKME_MEDIA_REALTIME_PROXY_SLOT_SCOPE")
        or "auto"
    ).strip().lower()
    if raw in {"process", "local", "per_process", "per-process"}:
        return "process"
    if raw in {"global", "host", "host_global", "host-global"}:
        return "global" if fcntl is not None and realtime_proxy_lock_dir() is not None else "process"
    return "global" if fcntl is not None and realtime_proxy_lock_dir() is not None else "process"


def _row_dict(row):
    return dict(row) if row is not None else None


def _row_value(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _file_media_type(file_row):
    mime = str(file_row["mime_type_plain_for_public"] or "").lower()
    filename = str(file_row["original_filename_plain_for_public"] or "").lower()
    if mime.startswith("audio/") or any(filename.endswith(ext) for ext in (".mp3", ".m4a", ".aac", ".flac", ".wav", ".weba", ".opus", ".oga", ".ogg")):
        return "audio"
    return "video"


def _source_filename_ext(file_row):
    filename = str(_row_value(file_row, "original_filename_plain_for_public", "") or "").strip().lower()
    return Path(filename).suffix.lower()


def _declared_or_guessed_mime(file_row):
    filename = str(_row_value(file_row, "original_filename_plain_for_public", "") or "")
    stored = str(_row_value(file_row, "mime_type_plain_for_public", "") or "").strip().lower()
    guessed = str(mimetypes.guess_type(filename)[0] or "").strip().lower()
    generic = {"", "application/octet-stream", "binary/octet-stream", "application/binary"}
    if stored in generic:
        return guessed or stored or "application/octet-stream"
    if guessed.startswith(("video/", "audio/")) and not stored.startswith(("video/", "audio/")):
        return guessed
    return stored or guessed or "application/octet-stream"


def _audio_stream_browser_direct_friendly(stream):
    codec = str((stream or {}).get("codec") or "").lower()
    codec_tag = str((stream or {}).get("codec_tag") or "").lower()
    channels = _safe_int((stream or {}).get("channels"), 0)
    if channels > 2:
        return False
    return codec in {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le", "pcm_s24le"} or codec_tag in {"mp4a", "mp3"}


def _metadata_browser_direct_friendly(metadata, *, ext="", mime=""):
    media_type = str((metadata or {}).get("media_type") or "")
    ext = str(ext or "").lower()
    mime = str(mime or "").lower()
    codec = str((metadata or {}).get("codec") or "").lower()
    codec_tag = str((metadata or {}).get("codec_tag") or "").lower()
    audio_streams = list((metadata or {}).get("audio_streams") or [])
    if media_type == "audio":
        if ext in {".mp3", ".wav", ".weba", ".opus", ".oga", ".ogg", ".m4a", ".aac", ".flac"}:
            return _audio_stream_browser_direct_friendly({
                "codec": codec,
                "codec_tag": codec_tag,
                "channels": _safe_int((audio_streams[0] if audio_streams else {}).get("channels"), 0),
            })
        return mime.startswith("audio/") and _audio_stream_browser_direct_friendly({
            "codec": codec,
            "codec_tag": codec_tag,
            "channels": _safe_int((audio_streams[0] if audio_streams else {}).get("channels"), 0),
        })
    if ext not in {".mp4", ".m4v", ".webm"}:
        return False
    if ext in {".mp4", ".m4v"}:
        video_ok = codec in {"h264", "avc1"} or codec_tag == "avc1"
        audio_ok = not audio_streams or (len(audio_streams) == 1 and _audio_stream_browser_direct_friendly(audio_streams[0]))
        return bool(video_ok and audio_ok)
    if ext == ".webm":
        video_ok = codec in {"vp8", "vp9", "av1", "av01"} or codec_tag == "av01"
        audio_ok = not audio_streams or (len(audio_streams) == 1 and _audio_stream_browser_direct_friendly(audio_streams[0]))
        return bool(video_ok and audio_ok)
    return False


def direct_browser_playback_status(file_row, *, storage_root=None, ffprobe_bin="ffprobe"):
    ext = _source_filename_ext(file_row)
    mime = _declared_or_guessed_mime(file_row)
    media_type = _file_media_type(file_row)
    incompatible_exts = {".mkv", ".avi", ".ts", ".m2ts", ".mts", ".flv", ".wmv", ".ogv"}
    if ext in incompatible_exts:
        return {
            "available": False,
            "reason": f"container_{ext[1:] or 'unknown'}_not_browser_native",
            "detail": "原始容器不是瀏覽器穩定支援的直接播放格式，請使用 HLS 或即時轉封裝。",
            "mime_type": mime,
            "extension": ext,
        }
    direct_exts = {".mp4", ".m4v", ".webm", ".mp3", ".wav", ".weba", ".opus", ".oga", ".ogg", ".m4a", ".aac", ".flac"}
    if ext not in direct_exts and not mime.startswith(("video/mp4", "video/webm", "audio/")):
        return {
            "available": False,
            "reason": "unknown_direct_browser_support",
            "detail": "原始格式無法判定為瀏覽器原生可播放，請使用 HLS 或即時轉封裝。",
            "mime_type": mime,
            "extension": ext,
        }
    metadata = None
    if storage_root and not is_server_encrypted_file(file_row):
        try:
            source_path = resolve_file_storage_path(storage_root, file_row)
            if source_path.exists():
                metadata = _parse_probe_metadata(_run_probe(source_path, ffprobe_bin=ffprobe_bin))
        except Exception:
            metadata = None
    if metadata is not None:
        ok = _metadata_browser_direct_friendly(metadata, ext=ext, mime=mime)
        if not ok:
            return {
                "available": False,
                "reason": "codec_or_track_not_browser_native",
                "detail": "原始 codec、音軌數或聲道配置不適合直接交給瀏覽器播放。",
                "mime_type": mime,
                "extension": ext,
                "codec": str(metadata.get("codec") or ""),
                "audio_codec": str(metadata.get("audio_codec") or ""),
                "audio_tracks": len(metadata.get("audio_streams") or []),
            }
    return {
        "available": True,
        "reason": "browser_native_likely",
        "detail": "原始格式屬於瀏覽器原生播放的常見組合。",
        "mime_type": mime,
        "extension": ext,
        "media_type": media_type,
    }


def should_auto_prepare_stream(file_row, *, visibility="public"):
    if not file_row or _row_value(file_row, "deleted_at") or is_e2ee_file(file_row):
        return {"enabled": False, "reason": "unavailable"}
    media_type = _file_media_type(file_row)
    privacy_mode = str(_row_value(file_row, "privacy_mode", "standard_plain") or "standard_plain")
    visibility = str(visibility or "public").strip().lower() or "public"
    size_bytes = _safe_int(_row_value(file_row, "size_bytes"), 0)
    if privacy_mode == "server_encrypted":
        return {
            "enabled": True,
            "reason": "server_encrypted_media",
            "media_type": media_type,
        }
    if media_type == "video" and visibility in {"public", "unlisted"}:
        return {
            "enabled": True,
            "reason": "published_video",
            "media_type": media_type,
        }
    if media_type == "audio" and visibility in {"public", "unlisted"} and size_bytes >= DEFAULT_STREAM_AUTO_PREPARE_AUDIO_MIN_BYTES:
        return {
            "enabled": True,
            "reason": "large_published_audio",
            "media_type": media_type,
        }
    return {
        "enabled": False,
        "reason": "manual_prepare_only",
        "media_type": media_type,
    }


def _derivative_root_relpath(uploaded_file_id):
    return f"media_derivatives/{uploaded_file_id}"


def _asset_row(conn, uploaded_file_id):
    return conn.execute("SELECT * FROM media_stream_assets WHERE uploaded_file_id=?", (str(uploaded_file_id),)).fetchone()


def _variant_rows(conn, asset_id):
    return conn.execute(
        "SELECT * FROM media_stream_variants WHERE asset_id=? ORDER BY id ASC",
        (int(asset_id),),
    ).fetchall()


def _segment_rows(conn, variant_id):
    return conn.execute(
        "SELECT * FROM media_stream_segments WHERE variant_id=? ORDER BY sequence_number ASC, id ASC",
        (int(variant_id),),
    ).fetchall()


def _subtitle_rows(conn, asset_id):
    return conn.execute(
        "SELECT * FROM media_stream_subtitles WHERE asset_id=? ORDER BY is_default DESC, id ASC",
        (int(asset_id),),
    ).fetchall()


def _segment_summary(conn, variant_id):
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS segment_count,
            COALESCE(SUM(byte_size), 0) AS segments_total_bytes,
            COALESCE(SUM(duration_seconds), 0) AS segments_total_duration_seconds
        FROM media_stream_segments
        WHERE variant_id=?
        """,
        (int(variant_id),),
    ).fetchone()
    return {
        "segment_count": _safe_int(row["segment_count"] if row else 0, 0),
        "segments_total_bytes": _safe_int(row["segments_total_bytes"] if row else 0, 0),
        "segments_total_duration_seconds": _safe_float(row["segments_total_duration_seconds"] if row else 0.0, 0.0),
    }


def serialize_stream_asset(conn, uploaded_file_id, *, include_segments=True):
    ensure_media_stream_schema(conn)
    asset = _asset_row(conn, uploaded_file_id)
    if not asset:
        return None
    data = _row_dict(asset)
    data["source_size_bytes"] = _safe_int(data.get("source_size_bytes"), 0)
    data["duration_seconds"] = _safe_float(data.get("duration_seconds"), 0.0)
    data["variants"] = []
    data["audio_tracks"] = []
    data["subtitles"] = []
    for variant in _variant_rows(conn, asset["id"]):
        item = _row_dict(variant)
        item["media_kind"] = str(item.get("media_kind") or "variant")
        item["label"] = str(item.get("label") or "")
        item["language"] = str(item.get("language") or "")
        item["stream_index"] = _safe_int(item.get("stream_index"), -1)
        item["is_default"] = bool(_safe_int(item.get("is_default"), 0))
        item["width"] = _safe_int(item.get("width"), 0)
        item["height"] = _safe_int(item.get("height"), 0)
        item["bitrate"] = _safe_int(item.get("bitrate"), 0)
        item["segment_count"] = 0
        item["segments_total_bytes"] = 0
        item["segments_total_duration_seconds"] = 0.0
        item["segments"] = []
        if include_segments:
            segment_rows = _segment_rows(conn, variant["id"])
            item["segment_count"] = len(segment_rows)
            for segment in segment_rows:
                byte_size = _safe_int(segment["byte_size"], 0)
                duration_seconds = _safe_float(segment["duration_seconds"], 0.0)
                item["segments_total_bytes"] += byte_size
                item["segments_total_duration_seconds"] += duration_seconds
                seg = _row_dict(segment)
                seg["sequence_number"] = _safe_int(seg.get("sequence_number"), 0)
                seg["byte_size"] = byte_size
                seg["duration_seconds"] = duration_seconds
                item["segments"].append(seg)
        else:
            item.update(_segment_summary(conn, variant["id"]))
        if item["media_kind"] == "audio":
            if not item["label"]:
                item["label"] = item["language"] or item["name"]
            data["audio_tracks"].append(item)
        else:
            data["variants"].append(item)
    for subtitle in _subtitle_rows(conn, asset["id"]):
        item = _row_dict(subtitle)
        item["is_default"] = bool(_safe_int(item.get("is_default"), 0))
        item["is_forced"] = bool(_safe_int(item.get("is_forced"), 0))
        data["subtitles"].append(item)
    data["premium_hls_profile_policy"] = _premium_hls_profile_policy(data)
    return data


def _upsert_asset_row(conn, *, file_row, status, error_message=None):
    now = _now()
    media_type = _file_media_type(file_row)
    existing = _asset_row(conn, file_row["id"])
    if existing:
        conn.execute(
            """
            UPDATE media_stream_assets
            SET source_mode=?, media_type=?, status=?, storage_mode=?, source_mime_type=?, source_size_bytes=?,
                error_message=?, updated_at=?
            WHERE uploaded_file_id=?
            """,
            (
                str(file_row["privacy_mode"] or "standard_plain"),
                media_type,
                status,
                MEDIA_STREAM_STORAGE_MODE,
                str(file_row["mime_type_plain_for_public"] or ""),
                _safe_int(file_row["size_bytes"], 0),
                str(error_message or ""),
                now,
                file_row["id"],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO media_stream_assets (
                uploaded_file_id, source_mode, media_type, status, storage_mode, source_mime_type,
                source_size_bytes, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_row["id"],
                str(file_row["privacy_mode"] or "standard_plain"),
                media_type,
                status,
                MEDIA_STREAM_STORAGE_MODE,
                str(file_row["mime_type_plain_for_public"] or ""),
                _safe_int(file_row["size_bytes"], 0),
                str(error_message or ""),
                now,
                now,
            ),
        )
    return _asset_row(conn, file_row["id"])


def _mark_asset_unavailable(conn, *, file_row, reason):
    asset = _upsert_asset_row(conn, file_row=file_row, status="unavailable", error_message=reason)
    return serialize_stream_asset(conn, asset["uploaded_file_id"])


def mark_stream_asset_processing(conn, *, file_row, error_message=""):
    ensure_media_stream_schema(conn)
    if not file_row or _row_value(file_row, "deleted_at"):
        raise ValueError("file not found")
    if is_e2ee_file(file_row):
        return _mark_asset_unavailable(conn, file_row=file_row, reason=STRICT_E2EE_SERVER_TRANSCODE_DISABLED_REASON)
    asset = _upsert_asset_row(conn, file_row=file_row, status="processing", error_message=error_message)
    return serialize_stream_asset(conn, asset["uploaded_file_id"])


def _set_asset_ready(conn, *, file_row, master_manifest_path, duration_seconds, warning_message=""):
    now = _now()
    conn.execute(
        """
        UPDATE media_stream_assets
        SET status='ready', master_manifest_path=?, duration_seconds=?, error_message=?, updated_at=?
        WHERE uploaded_file_id=?
        """,
        (master_manifest_path, float(duration_seconds or 0.0), str(warning_message or ""), now, file_row["id"]),
    )
    return _asset_row(conn, file_row["id"])


def _set_asset_failed(conn, *, file_row, reason):
    now = _now()
    conn.execute(
        """
        UPDATE media_stream_assets
        SET status='failed', error_message=?, updated_at=?
        WHERE uploaded_file_id=?
        """,
        (str(reason or "stream build failed"), now, file_row["id"]),
    )
    return _asset_row(conn, file_row["id"])


def _record_job(conn, *, asset_id, status, error_message=None, started_at=None, job_type="prepare_hls"):
    now = _now()
    conn.execute(
        """
        INSERT INTO media_stream_jobs (
            asset_id, job_type, status, started_at, finished_at, error_message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(asset_id),
            str(job_type or "prepare_hls"),
            str(status),
            str(started_at or now),
            now if status in {"ready", "failed", "unavailable"} else None,
            str(error_message or ""),
            now,
        ),
    )


def _run_probe(source_path, *, ffprobe_bin="ffprobe"):
    cmd = [
        str(ffprobe_bin),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout or "{}")


def _track_language(value):
    text = str(value or "und").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "", text)[:16]
    return text or "und"


def _track_title(tags):
    if not isinstance(tags, dict):
        return ""
    return str(tags.get("title") or "").strip()[:80]


def _audio_label(stream, index):
    title = str((stream or {}).get("title") or "").strip()
    language = _track_language((stream or {}).get("language"))
    codec = str((stream or {}).get("codec") or "").strip().upper()
    if title:
        return title[:80]
    if language != "und":
        return f"音軌 {index} ({language})"
    if codec:
        return f"音軌 {index} ({codec})"
    return f"音軌 {index}"


def _stream_track_limit(name, default):
    return _bounded_env_int(name, default, min_value=1, max_value=64)


def _parse_probe_metadata(payload):
    streams = payload.get("streams") if isinstance(payload, dict) else []
    fmt = payload.get("format") if isinstance(payload, dict) else {}
    video_stream = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio_stream = next((row for row in streams if row.get("codec_type") == "audio"), None)
    duration_seconds = _safe_float((fmt or {}).get("duration") or (video_stream or {}).get("duration") or (audio_stream or {}).get("duration"), 0.0)
    bitrate = _safe_int((fmt or {}).get("bit_rate") or (video_stream or {}).get("bit_rate") or (audio_stream or {}).get("bit_rate"), 0)
    width = _safe_int((video_stream or {}).get("width"), 0)
    height = _safe_int((video_stream or {}).get("height"), 0)
    profile = (video_stream or audio_stream or {}).get("profile") or ""
    level = _safe_int((video_stream or {}).get("level"), 0)
    codec = (video_stream or audio_stream or {}).get("codec_name") or ""
    codec_tag = (video_stream or audio_stream or {}).get("codec_tag_string") or ""
    audio_codec = (audio_stream or {}).get("codec_name") or ""
    audio_codec_tag = (audio_stream or {}).get("codec_tag_string") or ""
    media_type = "audio" if video_stream is None and audio_stream is not None else "video"
    audio_streams = []
    audio_ordinal = 0
    for stream in streams:
        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        audio_ordinal += 1
        audio_streams.append({
            "index": _safe_int(stream.get("index"), -1),
            "ordinal": audio_ordinal,
            "codec": str(stream.get("codec_name") or ""),
            "codec_tag": str(stream.get("codec_tag_string") or ""),
            "language": _track_language(tags.get("language") or "und"),
            "title": _track_title(tags),
            "is_default": bool(_safe_int(disposition.get("default"), 0)) or audio_ordinal == 1,
            "channels": _safe_int(stream.get("channels"), 0),
            "channel_layout": str(stream.get("channel_layout") or ""),
            "bitrate": _safe_int(stream.get("bit_rate"), 0),
        })
    subtitle_streams = []
    for stream in streams:
        if stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        subtitle_streams.append({
            "index": _safe_int(stream.get("index"), -1),
            "codec": str(stream.get("codec_name") or ""),
            "language": _track_language(tags.get("language") or "und"),
            "title": _track_title(tags),
            "is_default": bool(_safe_int(disposition.get("default"), 0)),
            "is_forced": bool(_safe_int(disposition.get("forced"), 0)),
        })
    return {
        "duration_seconds": duration_seconds,
        "bitrate": bitrate,
        "width": width,
        "height": height,
        "profile": str(profile),
        "level": level,
        "codec": str(codec),
        "codec_tag": str(codec_tag),
        "audio_codec": str(audio_codec),
        "audio_codec_tag": str(audio_codec_tag),
        "media_type": media_type,
        "audio_streams": audio_streams,
        "subtitle_streams": subtitle_streams,
    }


def _subtitle_language(value):
    return _track_language(value)


def _subtitle_label(stream, index):
    title = str((stream or {}).get("title") or "").strip()
    language = _subtitle_language((stream or {}).get("language"))
    if title:
        return title[:80]
    if language != "und":
        return f"字幕 {index} ({language})"
    return f"字幕 {index}"


def _subtitle_codec_supported(codec):
    return str(codec or "").strip().lower() in {
        "ass",
        "ssa",
        "subrip",
        "srt",
        "mov_text",
        "webvtt",
        "text",
    }


def _extract_subtitles_to_webvtt(source_path, *, derivative_dir, derivative_root_rel, metadata, ffmpeg_bin="ffmpeg"):
    subtitle_streams = list((metadata or {}).get("subtitle_streams") or [])
    subtitle_dir = Path(derivative_dir) / "subtitles"
    rows = []
    errors = []
    supported_index = 0
    for stream in subtitle_streams[:_stream_track_limit("HACKME_MEDIA_SUBTITLE_TRACK_LIMIT", DEFAULT_STREAM_SUBTITLE_TRACK_LIMIT)]:
        codec = str(stream.get("codec") or "").strip().lower()
        stream_index = _safe_int(stream.get("index"), -1)
        if stream_index < 0:
            continue
        if not _subtitle_codec_supported(codec):
            errors.append(f"subtitle stream {stream_index}: unsupported codec {codec or 'unknown'}")
            continue
        supported_index += 1
        language = _subtitle_language(stream.get("language"))
        name = f"sub{supported_index:02d}_{language}"
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        output_path = subtitle_dir / f"{name}.vtt"
        cmd = [
            str(ffmpeg_bin),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-map",
            f"0:{stream_index}",
            "-vn",
            "-an",
            "-c:s",
            "webvtt",
            str(output_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=_subtitle_extract_timeout_seconds())
        except subprocess.TimeoutExpired:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            errors.append(f"subtitle stream {stream_index}: extraction timed out after {_subtitle_extract_timeout_seconds()}s")
            continue
        except Exception as exc:
            errors.append(f"subtitle stream {stream_index}: {str(exc)[:180]}")
            continue
        if not output_path.exists() or output_path.stat().st_size <= 0:
            errors.append(f"subtitle stream {stream_index}: empty WebVTT output")
            continue
        rows.append({
            "name": name,
            "label": _subtitle_label(stream, supported_index),
            "language": language,
            "codec": codec,
            "path": f"{derivative_root_rel}/subtitles/{name}.vtt",
            "is_default": bool(stream.get("is_default")) or supported_index == 1,
            "is_forced": bool(stream.get("is_forced")),
            "absolute_path": output_path,
        })
    return rows, errors


def _srt_text_to_webvtt(text):
    lines = ["WEBVTT", ""]
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw
        if "-->" in line:
            line = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", line)
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def parse_subtitle_shift_ms(value, *, min_ms=-60 * 60 * 1000, max_ms=60 * 60 * 1000):
    try:
        if value is None or value == "":
            return 0
        parsed = int(round(float(value)))
    except Exception:
        return 0
    return max(int(min_ms), min(int(max_ms), parsed))


def _format_webvtt_timestamp(total_ms):
    total = max(0, int(round(total_ms or 0)))
    hours, remainder = divmod(total, 60 * 60 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _webvtt_timestamp_to_ms(match):
    hours = _safe_int(match.group("hours"), 0)
    minutes = _safe_int(match.group("minutes"), 0)
    seconds = _safe_int(match.group("seconds"), 0)
    millis = _safe_int(match.group("millis"), 0)
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + millis


WEBVTT_TIMESTAMP_RE = re.compile(
    r"(?:(?P<hours>\d{2,}):)?(?P<minutes>\d{2}):(?P<seconds>\d{2})\.(?P<millis>\d{3})"
)


def shift_webvtt_text(text, shift_ms):
    offset = parse_subtitle_shift_ms(shift_ms)
    if not offset:
        return str(text or "")
    shifted = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if "-->" not in raw_line:
            shifted.append(raw_line)
            continue
        shifted.append(WEBVTT_TIMESTAMP_RE.sub(lambda match: _format_webvtt_timestamp(_webvtt_timestamp_to_ms(match) + offset), raw_line))
    return "\n".join(shifted)


def _normalize_uploaded_subtitle_to_webvtt(source_path, output_path, *, original_filename="", ffmpeg_bin="ffmpeg"):
    suffix = Path(original_filename or source_path).suffix.lower()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".vtt":
        text = Path(source_path).read_text(encoding="utf-8", errors="replace")
        if not text.lstrip().upper().startswith("WEBVTT"):
            text = "WEBVTT\n\n" + text
        output_path.write_text(text, encoding="utf-8")
        return
    if suffix == ".srt":
        text = Path(source_path).read_text(encoding="utf-8", errors="replace")
        output_path.write_text(_srt_text_to_webvtt(text), encoding="utf-8")
        return
    if suffix in {".ass", ".ssa"}:
        cmd = [
            str(ffmpeg_bin),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-c:s",
            "webvtt",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=_ffmpeg_timeout_seconds())
        return
    raise ValueError("unsupported subtitle format; use .srt, .vtt, .ass, or .ssa")


def add_stream_subtitle(
    conn,
    *,
    file_row,
    storage_root,
    subtitle_file_path,
    original_filename="",
    label="",
    language="und",
    ffmpeg_bin="ffmpeg",
):
    ensure_media_stream_schema(conn)
    if not file_row or _row_value(file_row, "deleted_at"):
        raise ValueError("file not found")
    if is_e2ee_file(file_row):
        raise ValueError("strict E2EE media subtitles must be prepared by the browser-side E2EE package")
    existing = _asset_row(conn, file_row["id"])
    asset = _upsert_asset_row(
        conn,
        file_row=file_row,
        status=str(existing["status"] if existing else "pending"),
        error_message=(existing["error_message"] if existing else None),
    )
    derivative_root_rel = _derivative_root_relpath(file_row["id"])
    derivative_root = resolve_storage_path(storage_root, derivative_root_rel, create_parent=True)
    subtitle_dir = derivative_root / "subtitles"
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    language = _subtitle_language(language)
    name = f"user_{uuid.uuid4().hex[:12]}_{language}"
    output_path = subtitle_dir / f"{name}.vtt"
    _normalize_uploaded_subtitle_to_webvtt(
        subtitle_file_path,
        output_path,
        original_filename=original_filename,
        ffmpeg_bin=ffmpeg_bin,
    )
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise ValueError("subtitle conversion produced an empty file")
    now = _now()
    has_existing = conn.execute(
        "SELECT 1 FROM media_stream_subtitles WHERE asset_id=? LIMIT 1",
        (int(asset["id"]),),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO media_stream_subtitles (
            asset_id, name, label, language, codec, path, is_default, is_forced, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(asset["id"]),
            name,
            str(label or Path(original_filename or "").stem or language or "字幕")[:80],
            language,
            Path(original_filename or "").suffix.lower().lstrip(".") or "webvtt",
            f"{derivative_root_rel}/subtitles/{name}.vtt",
            0 if has_existing else 1,
            0,
            now,
        ),
    )
    return serialize_stream_asset(conn, file_row["id"], include_segments=False)


def refresh_stream_subtitles(
    conn,
    *,
    file_row,
    storage_root,
    server_file_fernet=None,
    source_path_override=None,
    force=False,
    ffprobe_bin="ffprobe",
    ffmpeg_bin="ffmpeg",
    progress_callback=None,
):
    ensure_media_stream_schema(conn)
    if not file_row or _row_value(file_row, "deleted_at"):
        raise ValueError("file not found")
    if is_e2ee_file(file_row):
        raise ValueError("strict E2EE media subtitles must be prepared by the browser-side E2EE package")
    asset = _asset_row(conn, file_row["id"])
    if not asset or str(asset["status"] or "") != "ready":
        raise ValueError("HLS asset must be ready before refreshing subtitles")
    existing = _subtitle_rows(conn, asset["id"])
    if existing and not force:
        return serialize_stream_asset(conn, file_row["id"], include_segments=False)

    derivative_root_rel = _derivative_root_relpath(file_row["id"])
    derivative_root = resolve_storage_path(storage_root, derivative_root_rel, create_parent=True)
    derivative_root.mkdir(parents=True, exist_ok=True)
    started_at = _now()
    try:
        with tempfile.TemporaryDirectory(prefix=f"hackme_stream_subtitles_{file_row['id']}_") as temp_dir:
            prepared_source = Path(source_path_override) if source_path_override else resolve_file_storage_path(storage_root, file_row)
            if source_path_override is None and is_server_encrypted_file(file_row):
                if progress_callback:
                    progress_callback(10, "decrypting", "正在解密伺服器端加密影音以抽取字幕。")
                source_path = resolve_file_storage_path(storage_root, file_row)
                temp_source = Path(temp_dir) / (str(file_row["original_filename_plain_for_public"] or file_row["id"]) or "media.bin")
                write_decrypted_server_encrypted_file(
                    source_path,
                    temp_source,
                    server_file_fernet,
                    progress_callback=(
                        (lambda written, total: progress_callback(
                            10 + int((max(0, min(int(written or 0), int(total or 0))) / max(1, int(total or 0))) * 45),
                            "decrypting",
                            f"正在解密伺服器端加密影音以抽取字幕：{_format_bytes_short(written)} / {_format_bytes_short(total)}。",
                        ))
                        if progress_callback else None
                    ),
                )
                prepared_source = temp_source
            if progress_callback:
                progress_callback(60, "probing", "正在讀取字幕軌資訊。")
            probe_payload = _run_probe(prepared_source, ffprobe_bin=ffprobe_bin)
            metadata = _parse_probe_metadata(probe_payload)
            if progress_callback:
                progress_callback(75, "extracting", "正在轉換字幕為 WebVTT。")
            subtitle_rows, subtitle_errors = _extract_subtitles_to_webvtt(
                prepared_source,
                derivative_dir=derivative_root,
                derivative_root_rel=derivative_root_rel,
                metadata=metadata,
                ffmpeg_bin=ffmpeg_bin,
            )
        now = _now()
        conn.execute(
            "DELETE FROM media_stream_subtitles WHERE asset_id=?",
            (int(asset["id"]),),
        )
        for subtitle in subtitle_rows:
            conn.execute(
                """
                INSERT INTO media_stream_subtitles (
                    asset_id, name, label, language, codec, path, is_default, is_forced, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(asset["id"]),
                    str(subtitle.get("name") or ""),
                    str(subtitle.get("label") or ""),
                    str(subtitle.get("language") or "und"),
                    str(subtitle.get("codec") or ""),
                    str(subtitle.get("path") or ""),
                    1 if subtitle.get("is_default") else 0,
                    1 if subtitle.get("is_forced") else 0,
                    now,
                ),
            )
        warning = "; ".join(subtitle_errors or [])[:800]
        conn.execute(
            """
            UPDATE media_stream_assets
            SET error_message=?, updated_at=?
            WHERE id=?
            """,
            (warning, now, int(asset["id"])),
        )
        _record_job(
            conn,
            asset_id=asset["id"],
            status="ready" if subtitle_rows else "failed",
            error_message=warning,
            started_at=started_at,
            job_type="refresh_subtitles",
        )
        _safe_commit(conn)
        if progress_callback:
            progress_callback(100, "ready" if subtitle_rows else "failed", "字幕抽取完成。" if subtitle_rows else "沒有可用的文字字幕軌。")
        return serialize_stream_asset(conn, file_row["id"], include_segments=False)
    except Exception as exc:
        _record_job(
            conn,
            asset_id=asset["id"],
            status="failed",
            error_message=str(exc),
            started_at=started_at,
            job_type="refresh_subtitles",
        )
        _safe_commit(conn)
        raise


def hls_codec_string(*, width=0, height=0, codec=""):
    raw = str(codec or "").strip().lower()
    if raw.startswith(("avc1.", "avc3.", "mp4a.")):
        return str(codec).strip()
    if raw in {"mp4a", "aac"}:
        return "mp4a.40.2"
    if raw in {"av1", "av01"} or "av01" in raw:
        return "av01.0.12M.08,mp4a.40.2" if width or height else "av01.0.12M.08"
    if not width and not height:
        return "mp4a.40.2"
    return "avc1.64001f,mp4a.40.2"


def _h264_avc1_codec_string(metadata):
    profile = str((metadata or {}).get("profile") or "").strip().lower()
    level = _safe_int((metadata or {}).get("level"), 31)
    if "high" in profile:
        profile_hex = "64"
    elif "main" in profile:
        profile_hex = "4d"
    else:
        profile_hex = "42"
    if level <= 0:
        level = 31
    return f"avc1.{profile_hex}00{max(1, min(level, 255)):02x}"


def realtime_proxy_mse_content_type(metadata):
    # Realtime proxy deliberately transcodes to a mobile-friendly fMP4 stream.
    # Do not advertise the source codec here: the source may be HEVC, AV1, Opus,
    # or a MOV-specific tag that mobile MSE cannot decode reliably.
    return 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"'


def repair_hls_master_manifest_text(text):
    repaired = []
    for raw_line in str(text or "").splitlines():
        line = raw_line
        if line.startswith("#EXT-X-STREAM-INF:"):
            if 'CODECS="' in line:
                start = line.find('CODECS="') + len('CODECS="')
                end = line.find('"', start)
                codec_value = line[start:end] if end > start else ""
                normalized = hls_codec_string(width=1, height=1, codec=codec_value)
                if normalized != codec_value and end > start:
                    line = line[:start] + normalized + line[end:]
            elif "RESOLUTION=" in line:
                line = f'{line},CODECS="{hls_codec_string(width=1, height=1)}"'
            else:
                line = f'{line},CODECS="{hls_codec_string(width=0, height=0)}"'
        repaired.append(line)
    return "\n".join(repaired) + ("\n" if repaired else "")


def _write_master_manifest(target, *, variant_name, playlist_name="playlist.m3u8", bitrate=0, width=0, height=0, codec=""):
    _write_master_manifest_variants(
        target,
        [{
            "name": variant_name,
            "playlist_name": playlist_name,
            "bitrate": bitrate,
            "width": width,
            "height": height,
            "codec": codec,
        }],
    )


def _hls_attr_quote(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _write_master_manifest_variants(target, variants, *, audio_tracks=None):
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
    ]
    tracks = list(audio_tracks or [])
    if tracks:
        has_default = any(bool(track.get("is_default")) for track in tracks)
        for index, track in enumerate(tracks):
            name = str(track.get("name") or "").strip()
            playlist_name = str(track.get("playlist_name") or "playlist.m3u8")
            if not name:
                continue
            label = str(track.get("label") or track.get("language") or name)
            language = _track_language(track.get("language") or "und")
            is_default = bool(track.get("is_default")) or (not has_default and index == 0)
            attrs = [
                "TYPE=AUDIO",
                'GROUP-ID="audio"',
                f'NAME="{_hls_attr_quote(label)}"',
                f'LANGUAGE="{_hls_attr_quote(language)}"',
                f"DEFAULT={'YES' if is_default else 'NO'}",
                "AUTOSELECT=YES",
                f'URI="{_hls_attr_quote(f"{name}/{playlist_name}")}"',
            ]
            lines.append("#EXT-X-MEDIA:" + ",".join(attrs))
    for variant in variants or []:
        name = str(variant.get("name") or "source").strip() or "source"
        playlist_name = str(variant.get("playlist_name") or "playlist.m3u8")
        width = _safe_int(variant.get("width"), 0)
        height = _safe_int(variant.get("height"), 0)
        codecs = hls_codec_string(width=width, height=height, codec=variant.get("codec") or "")
        bandwidth = max(int(variant.get("bitrate") or 0), 128000)
        attrs = [f"BANDWIDTH={bandwidth}"]
        if width and height:
            attrs.append(f"RESOLUTION={int(width)}x{int(height)}")
        if codecs:
            attrs.append(f'CODECS="{codecs}"')
        if tracks:
            attrs.append('AUDIO="audio"')
        lines.append("#EXT-X-STREAM-INF:" + ",".join(attrs))
        lines.append(f"{name}/{playlist_name}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _metadata_supports_stream_copy(metadata):
    if not _ffmpeg_copy_first_enabled():
        return False
    media_type = str((metadata or {}).get("media_type") or "")
    if _ffmpeg_force_stream_copy_enabled() and media_type in {"audio", "video"}:
        return True
    codec = str((metadata or {}).get("codec") or "").lower()
    codec_tag = str((metadata or {}).get("codec_tag") or "").lower()
    audio_codec = str((metadata or {}).get("audio_codec") or "").lower()
    audio_codec_tag = str((metadata or {}).get("audio_codec_tag") or "").lower()
    if media_type == "audio":
        return codec in {"aac", "mp3", "alac", "flac"} or codec_tag in {"mp4a", "mp3"}
    if codec not in {"h264", "avc1", "av1", "hevc", "h265"} and codec_tag not in {"avc1", "av01", "hvc1", "hev1"}:
        return False
    if audio_codec and audio_codec not in {"aac", "mp3"} and audio_codec_tag not in {"mp4a", "mp3"}:
        return False
    return True


def _metadata_supports_video_stream_copy(metadata):
    if not _ffmpeg_copy_first_enabled():
        return False
    if str((metadata or {}).get("media_type") or "") != "video":
        return False
    if _ffmpeg_force_stream_copy_enabled():
        return True
    codec = str((metadata or {}).get("codec") or "").lower()
    codec_tag = str((metadata or {}).get("codec_tag") or "").lower()
    return codec in {"h264", "avc1", "av1", "hevc", "h265"} or codec_tag in {"avc1", "av01", "hvc1", "hev1"}


def _audio_codec_browser_hls_friendly(stream):
    codec = str((stream or {}).get("codec") or "").lower()
    codec_tag = str((stream or {}).get("codec_tag") or "").lower()
    return codec in {"aac", "mp3"} or codec_tag in {"mp4a", "mp3"}


def _hls_audio_streams(metadata):
    limit = _stream_track_limit("HACKME_MEDIA_AUDIO_TRACK_LIMIT", DEFAULT_STREAM_AUDIO_TRACK_LIMIT)
    rows = []
    seen_default = False
    for stream in list((metadata or {}).get("audio_streams") or [])[:limit]:
        stream_index = _safe_int(stream.get("index"), -1)
        if stream_index < 0:
            continue
        item = dict(stream)
        item["language"] = _track_language(item.get("language") or "und")
        item["is_default"] = bool(item.get("is_default"))
        if item["is_default"]:
            seen_default = True
        rows.append(item)
    if rows and not seen_default:
        rows[0]["is_default"] = True
    return rows


def _hls_embedded_audio_needs_stereo_transcode(metadata):
    if _ffmpeg_force_stream_copy_enabled():
        return False
    if str((metadata or {}).get("media_type") or "") != "video":
        return False
    audio_streams = _hls_audio_streams(metadata)
    if len(audio_streams) != 1:
        return False
    stream = audio_streams[0]
    channels = _safe_int(stream.get("channels"), 0)
    if channels > 2:
        return True
    return not _audio_codec_browser_hls_friendly(stream)


def _realtime_proxy_audio_track_rows(metadata):
    rows = []
    for audio_index, stream in enumerate(_hls_audio_streams(metadata), start=1):
        language = _track_language(stream.get("language") or "und")
        name = f"audio_{audio_index:02d}_{language}"
        rows.append({
            "name": name,
            "label": _audio_label(stream, audio_index),
            "language": language,
            "stream_index": _safe_int(stream.get("index"), -1),
            "ordinal": audio_index,
            "is_default": bool(stream.get("is_default")) or audio_index == 1,
            "codec": str(stream.get("codec") or ""),
            "bitrate": _safe_int(stream.get("bitrate"), 0),
            "channels": _safe_int(stream.get("channels"), 0),
            "channel_layout": str(stream.get("channel_layout") or ""),
        })
    if rows and not any(row["is_default"] for row in rows):
        rows[0]["is_default"] = True
    return rows


def realtime_proxy_audio_tracks_for_source(source_path, *, ffprobe_bin="ffprobe"):
    metadata = _parse_probe_metadata(_run_probe(source_path, ffprobe_bin=ffprobe_bin))
    return _realtime_proxy_audio_track_rows(metadata)


def _select_realtime_proxy_audio_track(metadata, selector=None):
    tracks = _realtime_proxy_audio_track_rows(metadata)
    if not tracks:
        return None
    clean = str(selector or "").strip().lower()
    if not clean:
        return next((track for track in tracks if track.get("is_default")), tracks[0])
    for track in tracks:
        values = {
            str(track.get("name") or "").lower(),
            str(track.get("label") or "").lower(),
            str(track.get("language") or "").lower(),
            str(track.get("stream_index")),
            str(track.get("ordinal")),
            f"audio_{int(track.get('ordinal') or 0):02d}",
        }
        if clean in values:
            return track
    raise ValueError("找不到指定的即時轉封裝音軌")


def realtime_proxy_availability(file_row):
    media_type = _file_media_type(file_row)
    if not realtime_proxy_enabled():
        return {
            "available": False,
            "reason": "realtime_proxy_not_enabled",
            "media_type": media_type,
            "implementation_status": "disabled",
        }
    if is_e2ee_file(file_row):
        return {
            "available": False,
            "reason": "e2ee_server_proxy_not_allowed",
            "media_type": media_type,
            "implementation_status": "ready",
        }
    if media_type != "video":
        return {
            "available": False,
            "reason": "realtime_proxy_video_only",
            "media_type": media_type,
            "implementation_status": "ready",
        }
    return {
        "available": True,
        "reason": "available",
        "media_type": media_type,
        "implementation_status": "ready",
    }


def _parse_realtime_proxy_start_seconds(value):
    try:
        parsed = float(value)
    except Exception:
        parsed = 0.0
    if parsed < 0 or not parsed < 24 * 60 * 60:
        return 0.0
    return round(parsed, 3)


def build_realtime_proxy_command(
    source_path,
    *,
    audio_track=None,
    start_seconds=0,
    ffmpeg_bin="ffmpeg",
    ffprobe_bin="ffprobe",
):
    source_path = Path(source_path)
    metadata = _parse_probe_metadata(_run_probe(source_path, ffprobe_bin=ffprobe_bin))
    if str(metadata.get("media_type") or "") != "video":
        raise ValueError("即時轉封裝目前只支援影片")
    if _safe_int(metadata.get("width"), 0) <= 0 or _safe_int(metadata.get("height"), 0) <= 0:
        raise ValueError("找不到可轉封裝的影片軌")
    selected_audio = _select_realtime_proxy_audio_track(metadata, audio_track)
    start = _parse_realtime_proxy_start_seconds(start_seconds)
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]
    if start > 0:
        cmd.extend(["-ss", f"{start:.3f}".rstrip("0").rstrip(".")])
    cmd.extend(["-i", str(source_path), "-map", "0:v:0?"])
    if selected_audio:
        cmd.extend(["-map", f"0:{int(selected_audio['stream_index'])}"])
    else:
        cmd.append("-an")
    cmd.extend([
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-threads",
        str(_ffmpeg_thread_count()),
        "-preset",
        _ffmpeg_preset(),
        "-profile:v",
        "baseline",
        "-pix_fmt",
        "yuv420p",
        "-tune",
        "zerolatency",
        "-crf",
        "23",
        "-g",
        "48",
        "-keyint_min",
        "48",
        "-sc_threshold",
        "0",
    ])
    if selected_audio:
        cmd.extend(["-c:a", "aac", "-b:a", realtime_proxy_audio_bitrate(), "-ac", "2"])
    cmd.extend([
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-flush_packets",
        "1",
        "-f",
        "mp4",
        "pipe:1",
    ])
    return {
        "command": cmd,
        "metadata": metadata,
        "audio_track": selected_audio,
        "audio_tracks": _realtime_proxy_audio_track_rows(metadata),
        "start_seconds": start,
        "mimetype": "video/mp4",
        "mse_content_type": realtime_proxy_mse_content_type(metadata),
    }


def _realtime_proxy_slot_path(lock_dir, index):
    return Path(lock_dir) / f"realtime_proxy_slot_{int(index):02d}.lock"


def _count_realtime_proxy_global_slots_locked(limit, lock_dir):
    if fcntl is None or lock_dir is None:
        return {"global_active": None, "global_free": None}
    locked = 0
    free = 0
    errors = 0
    for index in range(int(limit)):
        if index in _REALTIME_PROXY_HELD_SLOTS:
            locked += 1
            continue
        handle = None
        try:
            handle = _realtime_proxy_slot_path(lock_dir, index).open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            locked += 1
        except Exception:
            errors += 1
        else:
            free += 1
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
    return {
        "global_active": locked,
        "global_free": free,
        "global_errors": errors,
    }


def _global_realtime_proxy_state_locked(limit, lock_dir):
    counts = _count_realtime_proxy_global_slots_locked(limit, lock_dir)
    active = counts.get("global_active")
    if active is None:
        active = _REALTIME_PROXY_ACTIVE
    return {
        "active": int(active or 0),
        "local_active": int(_REALTIME_PROXY_ACTIVE),
        "limit": int(limit),
        "scope": "global",
        "lock_dir": str(lock_dir),
        **counts,
    }


def _try_acquire_realtime_proxy_slot():
    global _REALTIME_PROXY_ACTIVE
    limit = realtime_proxy_max_concurrent()
    scope = realtime_proxy_slot_scope()
    if scope == "global":
        lock_dir = realtime_proxy_lock_dir()
        if fcntl is not None and lock_dir is not None:
            with _REALTIME_PROXY_LOCK:
                lock_dir.mkdir(parents=True, exist_ok=True)
                for index in range(limit):
                    if index in _REALTIME_PROXY_HELD_SLOTS:
                        continue
                    handle = _realtime_proxy_slot_path(lock_dir, index).open("a+", encoding="utf-8")
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        handle.close()
                        continue
                    except Exception:
                        handle.close()
                        raise
                    _REALTIME_PROXY_HELD_SLOTS.add(index)
                    _REALTIME_PROXY_ACTIVE += 1
                    state = _global_realtime_proxy_state_locked(limit, lock_dir)
                    state["slot_index"] = index
                    return True, state, (handle, index)
                state = _global_realtime_proxy_state_locked(limit, lock_dir)
                state["active"] = max(int(state.get("active") or 0), int(limit))
                return False, state, None
    with _REALTIME_PROXY_LOCK:
        if _REALTIME_PROXY_ACTIVE >= limit:
            return False, {"active": _REALTIME_PROXY_ACTIVE, "local_active": _REALTIME_PROXY_ACTIVE, "limit": limit, "scope": "process"}, None
        _REALTIME_PROXY_ACTIVE += 1
        return True, {"active": _REALTIME_PROXY_ACTIVE, "local_active": _REALTIME_PROXY_ACTIVE, "limit": limit, "scope": "process"}, None


def _release_realtime_proxy_slot(slot_handle=None):
    global _REALTIME_PROXY_ACTIVE
    handle = None
    index = None
    if isinstance(slot_handle, tuple):
        handle, index = slot_handle
    else:
        handle = slot_handle
    with _REALTIME_PROXY_LOCK:
        if handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                handle.close()
            except Exception:
                pass
        if index is not None:
            _REALTIME_PROXY_HELD_SLOTS.discard(index)
        _REALTIME_PROXY_ACTIVE = max(0, _REALTIME_PROXY_ACTIVE - 1)


def realtime_proxy_runtime_status():
    limit = realtime_proxy_max_concurrent()
    scope = realtime_proxy_slot_scope()
    with _REALTIME_PROXY_LOCK:
        if scope == "global":
            lock_dir = realtime_proxy_lock_dir()
            state = _global_realtime_proxy_state_locked(limit, lock_dir) if lock_dir is not None else {
                "active": int(_REALTIME_PROXY_ACTIVE),
                "local_active": int(_REALTIME_PROXY_ACTIVE),
                "limit": int(limit),
                "scope": "process",
            }
        else:
            state = {
                "active": int(_REALTIME_PROXY_ACTIVE),
                "local_active": int(_REALTIME_PROXY_ACTIVE),
                "limit": int(limit),
                "scope": "process",
            }
        state.update({
            "enabled": realtime_proxy_enabled(),
            "timeout_seconds": realtime_proxy_timeout_seconds(),
        })
        return state


def _realtime_proxy_process_sample(pid):
    sample = {"rss_bytes": 0, "cpu_time_seconds": 0.0}
    try:
        pid = int(pid or 0)
    except Exception:
        pid = 0
    if pid <= 0:
        return sample
    status_path = Path(f"/proc/{pid}/status")
    try:
        for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    sample["rss_bytes"] = max(0, int(parts[1]) * 1024)
                break
    except Exception:
        pass
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        stat = stat_path.read_text(encoding="utf-8", errors="ignore")
        tail = stat[stat.rfind(")") + 2 :].split()
        ticks = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK")) or 100
        if len(tail) >= 13:
            sample["cpu_time_seconds"] = round((int(tail[11]) + int(tail[12])) / float(ticks), 6)
    except Exception:
        pass
    return sample


def open_realtime_proxy_stream(
    source_path,
    *,
    audio_track=None,
    start_seconds=0,
    ffmpeg_bin="ffmpeg",
    ffprobe_bin="ffprobe",
    chunk_size=256 * 1024,
):
    acquired, state, slot_handle = _try_acquire_realtime_proxy_slot()
    if not acquired:
        raise RuntimeError(f"realtime_proxy_busy:{state['active']}/{state['limit']}")
    proc = None
    try:
        info = build_realtime_proxy_command(
            source_path,
            audio_track=audio_track,
            start_seconds=start_seconds,
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin,
        )
        proc = subprocess.Popen(
            info["command"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except Exception:
        _release_realtime_proxy_slot(slot_handle)
        raise

    timeout_seconds = realtime_proxy_timeout_seconds()
    started = time.monotonic()
    metrics = {
        "pid": int(getattr(proc, "pid", 0) or 0),
        "runtime_active_at_start": int(state.get("active") or 0),
        "runtime_local_active_at_start": int(state.get("local_active") or 0),
        "runtime_limit": int(state.get("limit") or 0),
        "runtime_scope": str(state.get("scope") or "process"),
        "runtime_slot_index": state.get("slot_index"),
        "chunk_size": int(chunk_size or 256 * 1024),
        "bytes_sent": 0,
        "chunks_sent": 0,
        "first_chunk_latency_ms": None,
        "duration_ms": None,
        "rss_peak_bytes": 0,
        "cpu_time_seconds": 0.0,
        "resource_samples": 0,
        "returncode": None,
        "closed_by_client": False,
        "timed_out": False,
        "terminated": False,
        "killed": False,
        "finished": False,
    }

    def update_resource_sample():
        sample = _realtime_proxy_process_sample(metrics["pid"])
        metrics["resource_samples"] += 1
        metrics["rss_peak_bytes"] = max(int(metrics["rss_peak_bytes"] or 0), int(sample.get("rss_bytes") or 0))
        metrics["cpu_time_seconds"] = max(float(metrics["cpu_time_seconds"] or 0.0), float(sample.get("cpu_time_seconds") or 0.0))

    def generate():
        nonlocal proc
        try:
            stdout = proc.stdout
            while stdout is not None:
                if time.monotonic() - started > timeout_seconds:
                    metrics["timed_out"] = True
                    raise TimeoutError("realtime proxy timeout")
                update_resource_sample()
                ready, _, _ = select.select([stdout], [], [], 1.0)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                chunk = stdout.read(int(chunk_size or 256 * 1024))
                update_resource_sample()
                if not chunk:
                    break
                metrics["chunks_sent"] += 1
                metrics["bytes_sent"] += len(chunk)
                if metrics["first_chunk_latency_ms"] is None:
                    metrics["first_chunk_latency_ms"] = round((time.monotonic() - started) * 1000, 3)
                yield chunk
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        except GeneratorExit:
            metrics["closed_by_client"] = True
            raise
        finally:
            try:
                if proc and proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                if proc and proc.poll() is None:
                    metrics["terminated"] = True
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        metrics["killed"] = True
                        proc.kill()
            finally:
                update_resource_sample()
                metrics["returncode"] = proc.poll() if proc else None
                metrics["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
                metrics["finished"] = True
                _release_realtime_proxy_slot(slot_handle)

    info["chunks"] = generate()
    info["runtime"] = state
    info["metrics"] = metrics
    return info


def _should_use_external_hls_audio(metadata):
    if str((metadata or {}).get("media_type") or "") != "video":
        return False
    if _ffmpeg_force_stream_copy_enabled():
        return False
    audio_streams = _hls_audio_streams(metadata)
    if not audio_streams:
        return False
    if len(audio_streams) > 1:
        return True
    return not _audio_codec_browser_hls_friendly(audio_streams[0])


def _target_bitrate_for_height(height, source_bitrate=0):
    targets = {
        2160: 12_000_000,
        1440: 8_000_000,
        1080: 5_000_000,
        720: 2_800_000,
        480: 1_400_000,
        360: 800_000,
    }
    target = targets.get(int(height or 0), 2_800_000)
    source = int(source_bitrate or 0)
    if source > 0:
        return max(320_000, min(target, source))
    return target


def _ffmpeg_bitrate_args(target_bitrate):
    target = _safe_int(target_bitrate, 0)
    if target <= 0:
        return []
    maxrate = max(target, int(round(target * _ffmpeg_maxrate_multiplier())))
    bufsize = max(target, int(round(target * _ffmpeg_bufsize_multiplier())))
    return [
        "-b:v",
        str(target),
        "-maxrate",
        str(maxrate),
        "-bufsize",
        str(bufsize),
    ]


def _scaled_width(source_width, source_height, target_height):
    try:
        width = int(round(float(source_width or 0) * float(target_height or 0) / float(source_height or 1)))
    except Exception:
        width = 0
    if width <= 0:
        return 0
    return max(2, width - (width % 2))


def _hls_variant_specs(metadata, *, external_audio=False):
    media_type = str((metadata or {}).get("media_type") or "")
    source_width = _safe_int((metadata or {}).get("width"), 0)
    source_height = _safe_int((metadata or {}).get("height"), 0)
    source_bitrate = _safe_int((metadata or {}).get("bitrate"), 0)
    if media_type == "audio":
        return [{
            "name": "audio",
            "label": "原音質",
            "width": 0,
            "height": 0,
            "bitrate": source_bitrate,
            "codec": metadata.get("codec_tag") or metadata.get("codec") or "",
            "copy_codecs": _metadata_supports_stream_copy(metadata),
            "target_height": 0,
        }]
    original_audio_stereo_transcode = bool(
        not external_audio
        and _metadata_supports_video_stream_copy(metadata)
        and _hls_embedded_audio_needs_stereo_transcode(metadata)
    )
    original_spec = {
        "name": "original",
        "label": f"原畫質 {source_height}p" if source_height else "原畫質",
        "width": source_width,
        "height": source_height,
        "bitrate": source_bitrate,
        "codec": metadata.get("codec_tag") or metadata.get("codec") or "",
        "copy_codecs": (
            _metadata_supports_video_stream_copy(metadata)
            if external_audio
            else (_metadata_supports_stream_copy(metadata) and not original_audio_stereo_transcode)
        ),
        "copy_video_transcode_audio": original_audio_stereo_transcode,
        "target_height": 0,
    }
    quality_specs = []
    for height in _hls_quality_heights():
        if source_height and source_height <= height:
            continue
        quality_specs.append({
            "name": f"q{height}",
            "label": f"{height}p",
            "width": _scaled_width(source_width, source_height, height),
            "height": height,
            "bitrate": _target_bitrate_for_height(height, source_bitrate),
            "codec": "h264",
            "copy_codecs": False,
            "target_height": height,
        })
    original_mode = _hls_original_variant_mode()
    include_original = original_mode == "always" or not quality_specs
    if original_mode == "auto":
        include_original = not quality_specs
    specs = []
    if include_original:
        specs.append(original_spec)
    specs.extend(quality_specs)
    return specs


def _run_ffmpeg_hls(
    source_path,
    *,
    derivative_dir,
    media_type,
    variant_name=None,
    ffmpeg_bin="ffmpeg",
    segment_seconds=DEFAULT_HLS_SEGMENT_SECONDS,
    duration_seconds=0,
    source_height=0,
    target_height=0,
    target_bitrate=0,
    copy_codecs=False,
    copy_video_transcode_audio=False,
    video_only=False,
    audio_stream_index=None,
    progress_callback=None,
):
    variant_name = str(variant_name or ("audio" if media_type == "audio" else "source")).strip() or "source"
    variant_dir = Path(derivative_dir) / variant_name
    variant_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = variant_dir / "playlist.m3u8"
    segment_pattern = str(variant_dir / "seg_%05d.m4s")
    cmd = [
        str(ffmpeg_bin),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        "-y",
        "-i",
        str(source_path),
    ]
    if media_type == "audio":
        stream_index = _safe_int(audio_stream_index, -1)
        cmd.extend(["-map", f"0:{stream_index}" if stream_index >= 0 else "0:a:0?", "-vn", "-sn", "-dn"])
    elif video_only:
        cmd.extend(["-map", "0:v:0?", "-an", "-sn", "-dn"])
    else:
        cmd.extend(["-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn"])
    if copy_codecs:
        cmd.extend(["-c", "copy"])
    elif copy_video_transcode_audio and media_type == "video" and not video_only:
        cmd.extend([
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            _hls_audio_bitrate(),
            "-ac",
            "2",
        ])
    elif media_type == "audio":
        cmd.extend(["-c:a", "aac", "-b:a", _hls_audio_bitrate(), "-ac", "2"])
    else:
        max_height = _ffmpeg_max_video_height()
        scale_height = int(target_height or 0)
        if not scale_height and max_height and int(source_height or 0) > max_height:
            scale_height = int(max_height)
        cmd.extend([
            "-c:v",
            "libx264",
            "-threads",
            str(_ffmpeg_thread_count()),
            "-preset",
            _ffmpeg_preset(),
        ])
        bitrate_args = _ffmpeg_bitrate_args(target_bitrate)
        if bitrate_args:
            cmd.extend(bitrate_args)
        else:
            cmd.extend(["-crf", "23"])
        if scale_height and int(source_height or 0) > scale_height:
            cmd.extend(["-vf", f"scale=-2:{int(scale_height)}"])
        keyframe_seconds = max(1, int(segment_seconds or DEFAULT_HLS_SEGMENT_SECONDS))
        cmd.extend([
            "-force_key_frames",
            f"expr:gte(t,n_forced*{keyframe_seconds})",
            "-sc_threshold",
            "0",
        ])
        if not video_only:
            cmd.extend([
                "-c:a",
                "aac",
                "-b:a",
                _hls_audio_bitrate(),
                "-ac",
                "2",
            ])
    cmd.extend([
        "-f",
        "hls",
        "-hls_time",
        str(int(segment_seconds)),
        "-hls_playlist_type",
        "vod",
        "-hls_segment_type",
        "fmp4",
        "-hls_fmp4_init_filename",
        "init.mp4",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_filename",
        segment_pattern,
        str(playlist_path),
    ])
    timeout_seconds = _ffmpeg_timeout_seconds()
    started_at = time.monotonic()
    def run_hls_command(command):
        process = None
        stderr_chunks = []
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            last_progress = 0.0
            duration = max(0.0, float(duration_seconds or 0))
            while True:
                if timeout_seconds > 0 and time.monotonic() - started_at > timeout_seconds:
                    process.kill()
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                ready, _, _ = select.select([process.stdout], [], [], 0.5) if process.stdout else ([], [], [])
                if ready:
                    line = process.stdout.readline()
                    if not line:
                        if process.poll() is not None:
                            break
                        continue
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_ms" and duration > 0:
                        try:
                            current = max(0.0, min(1.0, float(value or 0) / 1_000_000.0 / duration))
                        except Exception:
                            current = 0.0
                        if current >= last_progress + 0.01:
                            last_progress = current
                            if progress_callback:
                                progress_callback(current)
                    elif key == "progress" and value == "end" and progress_callback:
                        progress_callback(1.0)
                    continue
                if process.poll() is not None:
                    break
            if process.stderr:
                stderr_chunks.append(process.stderr.read() or "")
            return_code = process.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command, output="", stderr="".join(stderr_chunks))
        finally:
            if process is not None and process.poll() is None:
                process.kill()

    try:
        run_hls_command(cmd)
    except subprocess.CalledProcessError:
        if copy_codecs and _ffmpeg_copy_fallback_transcode_enabled():
            shutil.rmtree(variant_dir, ignore_errors=True)
            return _run_ffmpeg_hls(
                source_path,
                derivative_dir=derivative_dir,
                media_type=media_type,
                variant_name=variant_name,
                ffmpeg_bin=ffmpeg_bin,
                segment_seconds=segment_seconds,
                duration_seconds=duration_seconds,
                source_height=source_height,
                target_height=target_height,
                target_bitrate=target_bitrate,
                copy_codecs=False,
                copy_video_transcode_audio=copy_video_transcode_audio,
                video_only=video_only,
                audio_stream_index=audio_stream_index,
                progress_callback=progress_callback,
            )
        raise
    return variant_name, playlist_path, variant_dir / "init.mp4"


def _parse_variant_playlist(playlist_path):
    rows = []
    current_duration = 0.0
    init_name = ""
    for raw_line in Path(playlist_path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            marker = 'URI="'
            start = line.find(marker)
            if start >= 0:
                start += len(marker)
                end = line.find('"', start)
                if end > start:
                    init_name = line[start:end]
        elif line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1].split(",", 1)[0].strip()
            current_duration = _safe_float(value, 0.0)
        elif not line.startswith("#"):
            rows.append({
                "filename": line,
                "duration_seconds": current_duration,
            })
            current_duration = 0.0
    return init_name, rows


def _directory_total_bytes(path):
    total = 0
    try:
        for item in Path(path).rglob("*"):
            if item.is_file():
                total += int(item.stat().st_size)
    except Exception:
        return 0
    return total


def _format_bytes_short(value):
    num = _safe_int(value, 0)
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    if num < 1024 * 1024 * 1024:
        return f"{num / 1024 / 1024:.1f} MB"
    return f"{num / 1024 / 1024 / 1024:.2f} GB"


def _record_hls_variant(
    conn,
    *,
    asset_id,
    variant_name,
    init_name,
    segments,
    derivative_root_rel,
    storage_root,
    now,
    width=0,
    height=0,
    bitrate=0,
    codec="",
    media_kind="variant",
    label="",
    language="",
    stream_index=-1,
    is_default=False,
):
    cur = conn.execute(
        """
        INSERT INTO media_stream_variants (
            asset_id, name, media_kind, label, language, stream_index, is_default,
            width, height, bitrate, codec, playlist_path, init_segment_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(asset_id),
            str(variant_name),
            str(media_kind or "variant"),
            str(label or ""),
            str(language or ""),
            _safe_int(stream_index, -1),
            1 if is_default else 0,
            int(width or 0),
            int(height or 0),
            int(bitrate or 0),
            str(codec or ""),
            f"{derivative_root_rel}/{variant_name}/playlist.m3u8",
            f"{derivative_root_rel}/{variant_name}/{init_name}" if init_name else "",
            now,
        ),
    )
    variant_id = cur.lastrowid
    for index, segment in enumerate(segments, start=1):
        segment_rel = f"{derivative_root_rel}/{variant_name}/{segment['filename']}"
        segment_file = resolve_storage_path(storage_root, segment_rel)
        conn.execute(
            """
            INSERT INTO media_stream_segments (
                variant_id, sequence_number, filename, path, duration_seconds, byte_size, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(variant_id),
                index,
                str(segment["filename"]),
                segment_rel,
                float(segment["duration_seconds"] or 0.0),
                int(segment_file.stat().st_size if segment_file.exists() else 0),
                now,
            ),
        )
    return variant_id


def _preferred_playback_quality_name(variants):
    rows = [variant for variant in (variants or []) if isinstance(variant, dict) and variant.get("name")]
    if not rows:
        return ""
    for height in (720, 480):
        match = next((variant for variant in rows if _safe_int(variant.get("height"), 0) == height), None)
        if match:
            return str(match.get("name") or "")
    non_original = [
        variant for variant in rows
        if str(variant.get("name") or "") not in {"original", "audio"} and _safe_int(variant.get("height"), 0) > 0
    ]
    if non_original:
        non_original.sort(key=lambda item: abs(_safe_int(item.get("height"), 0) - 720))
        return str(non_original[0].get("name") or "")
    return str(rows[0].get("name") or "")


def _fallback_playback_quality_name(variants):
    rows = [variant for variant in (variants or []) if isinstance(variant, dict) and variant.get("name")]
    match = next((variant for variant in rows if _safe_int(variant.get("height"), 0) == 480), None)
    return str(match.get("name") or "") if match else ""


def prepare_stream_asset(
    conn,
    *,
    file_row,
    storage_root,
    server_file_fernet=None,
    ffprobe_bin="ffprobe",
    ffmpeg_bin="ffmpeg",
    progress_callback=None,
):
    ensure_media_stream_schema(conn)
    if not file_row or _row_value(file_row, "deleted_at"):
        raise ValueError("file not found")
    if is_e2ee_file(file_row):
        return _mark_asset_unavailable(conn, file_row=file_row, reason=STRICT_E2EE_SERVER_TRANSCODE_DISABLED_REASON)
    asset = _upsert_asset_row(conn, file_row=file_row, status="processing")
    started_at = _now()
    derivative_root_rel = _derivative_root_relpath(file_row["id"])
    derivative_root = resolve_storage_path(storage_root, derivative_root_rel, create_parent=True)
    conn.execute("DELETE FROM media_stream_segments WHERE variant_id IN (SELECT id FROM media_stream_variants WHERE asset_id=?)", (int(asset["id"]),))
    conn.execute("DELETE FROM media_stream_variants WHERE asset_id=?", (int(asset["id"]),))
    conn.execute("DELETE FROM media_stream_subtitles WHERE asset_id=?", (int(asset["id"]),))
    _safe_commit(conn)
    if derivative_root.exists():
        shutil.rmtree(derivative_root)
    derivative_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"hackme_stream_{file_row['id']}_") as temp_dir:
        source_path = resolve_file_storage_path(storage_root, file_row)
        prepared_source = source_path
        if is_server_encrypted_file(file_row):
            if progress_callback:
                progress_callback(20, "decrypting", "正在以外部程序解密伺服器端加密影音，主站可繼續操作。")
            temp_source = Path(temp_dir) / (str(file_row["original_filename_plain_for_public"] or file_row["id"]) or "media.bin")
            write_decrypted_server_encrypted_file(
                source_path,
                temp_source,
                server_file_fernet,
                progress_callback=(
                    (lambda written, total: progress_callback(
                        20 + int((max(0, min(int(written or 0), int(total or 0))) / max(1, int(total or 0))) * 9),
                        "decrypting",
                        f"正在解密伺服器端加密影音：{_format_bytes_short(written)} / {_format_bytes_short(total)}。",
                    ))
                    if progress_callback else None
                ),
            )
            prepared_source = temp_source
        try:
            if progress_callback:
                progress_callback(30, "probing", "正在讀取影音格式與長度。")
            probe_payload = _run_probe(prepared_source, ffprobe_bin=ffprobe_bin)
            metadata = _parse_probe_metadata(probe_payload)
            copy_codecs = _metadata_supports_stream_copy(metadata)
            audio_streams = _hls_audio_streams(metadata)
            external_audio = _should_use_external_hls_audio(metadata)
            copy_hint = _metadata_supports_video_stream_copy(metadata) if external_audio else copy_codecs
            subtitle_rows, subtitle_errors = _extract_subtitles_to_webvtt(
                prepared_source,
                derivative_dir=derivative_root,
                derivative_root_rel=derivative_root_rel,
                metadata=metadata,
                ffmpeg_bin=ffmpeg_bin,
            )
            if progress_callback:
                if external_audio:
                    detail = "正在分離影片與音軌建立 HLS；可保留多音軌並降低整片重轉成本。"
                else:
                    detail = "正在以低負載快速封裝建立 HLS；進度會顯示在任務中心。" if copy_hint else "正在建立 HLS 播放清單與片段，進度會顯示在任務中心。"
                progress_callback(40, "transcoding", detail)
            variant_specs = _hls_variant_specs(metadata, external_audio=external_audio)
            if progress_callback:
                progress_callback(42, "transcoding", f"正在建立 {len(variant_specs)} 組 HLS 畫質。")
            master_manifest_path = derivative_root / "master.m3u8"
            now = _now()
            manifest_rows = []
            audio_manifest_rows = []
            variant_errors = list(subtitle_errors or [])
            original_variant_total_bytes = 0
            source_file_total_bytes = _safe_int(_row_value(file_row, "size_bytes", 0), 0)
            if source_file_total_bytes <= 0:
                try:
                    source_file_total_bytes = int(Path(prepared_source).stat().st_size)
                except Exception:
                    source_file_total_bytes = 0
            total_specs = max(1, len(variant_specs))
            for spec_index, spec in enumerate(variant_specs):
                start_percent = 42 + int((spec_index / total_specs) * 48)
                end_percent = 42 + int(((spec_index + 1) / total_specs) * 48)
                if progress_callback:
                    action = "封裝" if spec.get("copy_codecs") else "轉碼"
                    progress_callback(start_percent, "transcoding", f"正在{action} HLS 畫質：{spec.get('label') or spec.get('name')}。")
                try:
                    variant_name, playlist_path, init_segment_path = _run_ffmpeg_hls(
                        prepared_source,
                        derivative_dir=derivative_root,
                        media_type=metadata["media_type"],
                        variant_name=spec["name"],
                        ffmpeg_bin=ffmpeg_bin,
                        duration_seconds=metadata["duration_seconds"],
                        source_height=metadata["height"],
                        target_height=spec.get("target_height") or 0,
                        target_bitrate=spec.get("bitrate") or 0,
                        copy_codecs=bool(spec.get("copy_codecs")),
                        copy_video_transcode_audio=bool(spec.get("copy_video_transcode_audio")),
                        video_only=bool(external_audio),
                        progress_callback=(
                            (lambda ratio, low=start_percent, high=end_percent, label=spec.get("label"), copied=bool(spec.get("copy_codecs")): progress_callback(
                                low + int(max(0.0, min(1.0, float(ratio or 0))) * max(1, high - low)),
                                "transcoding",
                                (f"HLS 外部程序正在低負載封裝 {label}；你可以先做別的事，完成後會通知。"
                                 if copied else
                                 f"HLS 外部程序正在轉碼 {label}；你可以先做別的事，完成後會通知。")
                            ))
                            if progress_callback else None
                        ),
                    )
                except Exception as exc:
                    label = str(spec.get("label") or spec.get("name") or "unknown")
                    if manifest_rows and spec.get("name") not in {"original", "audio"}:
                        variant_errors.append(f"{label}: {str(exc)[:180]}")
                        if progress_callback:
                            progress_callback(
                                end_percent,
                                "transcoding",
                                f"HLS 畫質 {label} 建立失敗，已保留其他可用畫質。",
                            )
                        continue
                    raise
                init_name, segments = _parse_variant_playlist(playlist_path)
                variant_total_bytes = _directory_total_bytes(Path(playlist_path).parent)
                if variant_name == "original":
                    original_variant_total_bytes = variant_total_bytes
                variant_size_baseline = original_variant_total_bytes or source_file_total_bytes
                if (
                    variant_name not in {"original", "audio"}
                    and variant_size_baseline > 0
                    and variant_total_bytes > variant_size_baseline
                    and manifest_rows
                ):
                    label = str(spec.get("label") or spec.get("name") or variant_name)
                    baseline_label = "原畫質" if original_variant_total_bytes > 0 else "原始檔"
                    variant_errors.append(
                        f"{label}: 衍生檔 {_format_bytes_short(variant_total_bytes)} 大於{baseline_label} "
                        f"{_format_bytes_short(variant_size_baseline)}，已刪除並隱藏"
                    )
                    shutil.rmtree(Path(playlist_path).parent, ignore_errors=True)
                    if progress_callback:
                        progress_callback(
                            end_percent,
                            "transcoding",
                            f"HLS 畫質 {label} 比原畫質更大，已刪除並隱藏該畫質選項。",
                        )
                    continue
                _record_hls_variant(
                    conn,
                    asset_id=asset["id"],
                    variant_name=variant_name,
                    init_name=init_name,
                    segments=segments,
                    derivative_root_rel=derivative_root_rel,
                    storage_root=storage_root,
                    now=now,
                    width=int(spec.get("width") or 0),
                    height=int(spec.get("height") or 0),
                    bitrate=int(spec.get("bitrate") or 0),
                    codec=str(spec.get("codec") or metadata["codec"] or ""),
                    media_kind="variant",
                    label=str(spec.get("label") or ""),
                )
                _safe_commit(conn)
                manifest_rows.append({
                    "name": variant_name,
                    "playlist_name": "playlist.m3u8",
                    "bitrate": int(spec.get("bitrate") or 0),
                    "width": int(spec.get("width") or 0),
                    "height": int(spec.get("height") or 0),
                    "codec": spec.get("codec") or metadata.get("codec_tag") or metadata["codec"],
                })
            if external_audio:
                if progress_callback:
                    progress_callback(90, "transcoding", f"正在建立 {len(audio_streams)} 條 HLS 音軌。")
                for audio_index, stream in enumerate(audio_streams, start=1):
                    language = _track_language(stream.get("language") or "und")
                    audio_name = f"audio_{audio_index:02d}_{language}"
                    label = _audio_label(stream, audio_index)
                    try:
                        variant_name, playlist_path, init_segment_path = _run_ffmpeg_hls(
                            prepared_source,
                            derivative_dir=derivative_root,
                            media_type="audio",
                            variant_name=audio_name,
                            ffmpeg_bin=ffmpeg_bin,
                            duration_seconds=metadata["duration_seconds"],
                            source_height=0,
                            target_height=0,
                            target_bitrate=0,
                            copy_codecs=False,
                            audio_stream_index=stream.get("index"),
                            progress_callback=None,
                        )
                    except Exception as exc:
                        variant_errors.append(f"{label}: 音軌建立失敗 {str(exc)[:180]}")
                        continue
                    init_name, segments = _parse_variant_playlist(playlist_path)
                    bitrate = _audio_bitrate_to_bits_per_second(_hls_audio_bitrate())
                    _record_hls_variant(
                        conn,
                        asset_id=asset["id"],
                        variant_name=variant_name,
                        init_name=init_name,
                        segments=segments,
                        derivative_root_rel=derivative_root_rel,
                        storage_root=storage_root,
                        now=now,
                        width=0,
                        height=0,
                        bitrate=bitrate,
                        codec="aac",
                        media_kind="audio",
                        label=label,
                        language=language,
                        stream_index=stream.get("index"),
                        is_default=bool(stream.get("is_default")) or audio_index == 1,
                    )
                    _safe_commit(conn)
                    audio_manifest_rows.append({
                        "name": variant_name,
                        "playlist_name": "playlist.m3u8",
                        "bitrate": bitrate,
                        "codec": "aac",
                        "label": label,
                        "language": language,
                        "is_default": bool(stream.get("is_default")) or audio_index == 1,
                    })
                if not audio_manifest_rows:
                    raise RuntimeError("HLS 沒有成功產生任何音軌")
            if progress_callback:
                progress_callback(92, "finalizing", "HLS 片段已產生，正在寫入播放索引。")
            if not manifest_rows:
                raise RuntimeError("HLS 沒有成功產生任何可播放畫質")
            for subtitle in subtitle_rows:
                conn.execute(
                    """
                    INSERT INTO media_stream_subtitles (
                        asset_id, name, label, language, codec, path, is_default, is_forced, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(asset["id"]),
                        str(subtitle.get("name") or ""),
                        str(subtitle.get("label") or ""),
                        str(subtitle.get("language") or "und"),
                        str(subtitle.get("codec") or ""),
                        str(subtitle.get("path") or ""),
                        1 if subtitle.get("is_default") else 0,
                        1 if subtitle.get("is_forced") else 0,
                        now,
                    ),
                )
            _write_master_manifest_variants(master_manifest_path, manifest_rows, audio_tracks=audio_manifest_rows)
            asset = _set_asset_ready(
                conn,
                file_row=file_row,
                master_manifest_path=f"{derivative_root_rel}/master.m3u8",
                duration_seconds=metadata["duration_seconds"],
                warning_message="; ".join(variant_errors)[:800],
            )
            if progress_callback:
                progress_callback(98, "finalizing", "HLS 索引已寫入，正在完成任務紀錄。")
            _record_job(conn, asset_id=asset["id"], status="ready", started_at=started_at)
            _safe_commit(conn)
            return serialize_stream_asset(conn, file_row["id"])
        except Exception as exc:
            asset = _set_asset_failed(conn, file_row=file_row, reason=str(exc))
            _record_job(conn, asset_id=asset["id"], status="failed", error_message=str(exc), started_at=started_at)
            _safe_commit(conn)
            raise


def get_stream_status(conn, *, file_row, include_segments=True):
    ensure_media_stream_schema(conn)
    if not file_row or _row_value(file_row, "deleted_at"):
        return None
    asset = serialize_stream_asset(conn, file_row["id"], include_segments=include_segments)
    if asset:
        return asset
    if is_e2ee_file(file_row):
        return {
            "uploaded_file_id": file_row["id"],
            "source_mode": str(file_row["privacy_mode"] or "e2ee"),
            "media_type": _file_media_type(file_row),
            "status": "unavailable",
            "storage_mode": MEDIA_STREAM_STORAGE_MODE,
            "master_manifest_path": "",
            "duration_seconds": 0.0,
            "source_mime_type": str(file_row["mime_type_plain_for_public"] or mimetypes.guess_type(str(file_row["original_filename_plain_for_public"] or ""))[0] or ""),
            "source_size_bytes": _safe_int(file_row["size_bytes"], 0),
            "error_message": STRICT_E2EE_SERVER_TRANSCODE_DISABLED_REASON,
            "variants": [],
            "audio_tracks": [],
            "subtitles": [],
        }
    return {
        "uploaded_file_id": file_row["id"],
        "source_mode": str(file_row["privacy_mode"] or "standard_plain"),
        "media_type": _file_media_type(file_row),
        "status": "pending",
        "storage_mode": MEDIA_STREAM_STORAGE_MODE,
        "master_manifest_path": "",
        "duration_seconds": 0.0,
        "source_mime_type": str(file_row["mime_type_plain_for_public"] or mimetypes.guess_type(str(file_row["original_filename_plain_for_public"] or ""))[0] or ""),
        "source_size_bytes": _safe_int(file_row["size_bytes"], 0),
        "error_message": "",
        "variants": [],
        "audio_tracks": [],
        "subtitles": [],
    }


def cleanup_stream_asset(conn, *, uploaded_file_id, storage_root):
    ensure_media_stream_schema(conn)
    file_id = str(uploaded_file_id or "").strip()
    if not file_id:
        return {"file_id": file_id, "removed": False}
    asset_rows = conn.execute(
        "SELECT id FROM media_stream_assets WHERE uploaded_file_id=?",
        (file_id,),
    ).fetchall()
    asset_ids = [int(row["id"]) for row in asset_rows]
    variant_count = 0
    segment_count = 0
    subtitle_count = 0
    if asset_ids:
        placeholders = ",".join("?" for _ in asset_ids)
        variant_rows = conn.execute(
            f"SELECT id FROM media_stream_variants WHERE asset_id IN ({placeholders})",
            asset_ids,
        ).fetchall()
        variant_ids = [int(row["id"]) for row in variant_rows]
        variant_count = len(variant_ids)
        subtitle_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM media_stream_subtitles WHERE asset_id IN ({placeholders})",
            asset_ids,
        ).fetchone()["c"]
        if variant_ids:
            variant_placeholders = ",".join("?" for _ in variant_ids)
            segment_count = conn.execute(
                f"SELECT COUNT(*) AS c FROM media_stream_segments WHERE variant_id IN ({variant_placeholders})",
                variant_ids,
            ).fetchone()["c"]
            conn.execute(
                f"DELETE FROM media_stream_segments WHERE variant_id IN ({variant_placeholders})",
                variant_ids,
            )
        conn.execute(f"DELETE FROM media_stream_subtitles WHERE asset_id IN ({placeholders})", asset_ids)
        conn.execute(f"DELETE FROM media_stream_variants WHERE asset_id IN ({placeholders})", asset_ids)
        conn.execute(f"DELETE FROM media_stream_jobs WHERE asset_id IN ({placeholders})", asset_ids)
        conn.execute(f"DELETE FROM media_stream_assets WHERE id IN ({placeholders})", asset_ids)
    try:
        root = resolve_storage_path(storage_root, _derivative_root_relpath(file_id))
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
    except Exception:
        pass
    return {
        "file_id": file_id,
        "removed": bool(asset_ids),
        "assets_removed": len(asset_ids),
        "variants_removed": int(variant_count or 0),
        "segments_removed": int(segment_count or 0),
        "subtitles_removed": int(subtitle_count or 0),
    }



def _as_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _policy_int(settings, key, default, minimum=0, maximum=36500):
    try:
        value = int((settings or {}).get(key, default))
    except Exception:
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def cold_hls_policy(settings=None):
    settings = settings or {}
    raw_keep = str(settings.get("video_hls_warm_keep_variants") or "original,480p")
    keep_variants = [part.strip().lower() for part in raw_keep.split(",") if part.strip()]
    raw_mobile_keep = str(settings.get("video_hls_mobile_floor_keep_variants") or "480p")
    mobile_keep_variants = [part.strip().lower() for part in raw_mobile_keep.split(",") if part.strip()]
    return {
        "enabled": _as_bool(settings.get("video_hls_cold_cleanup_enabled"), True),
        "protect_days_after_upload": _policy_int(settings, "video_hls_protect_days_after_upload", 7, 0, 3650),
        "warm_plays_30d_below": _policy_int(settings, "video_hls_warm_plays_30d_below", 20, 1, 1000000),
        "cold_plays_30d_below": _policy_int(settings, "video_hls_cold_plays_30d_below", 5, 0, 1000000),
        "cold_no_play_days": _policy_int(settings, "video_hls_cold_no_play_days", 14, 1, 3650),
        "very_cold_no_play_days": _policy_int(settings, "video_hls_very_cold_no_play_days", 90, 1, 36500),
        "rebuild_plays_24h": _policy_int(settings, "video_hls_rebuild_plays_24h", 3, 1, 1000000),
        "rebuild_plays_7d": _policy_int(settings, "video_hls_rebuild_plays_7d", 5, 1, 1000000),
        "warm_keep_variants": keep_variants or ["original", "480p"],
        "mobile_floor_keep_variants": mobile_keep_variants or ["480p"],
        "max_assets_per_run": _policy_int(settings, "video_hls_cold_cleanup_max_assets_per_run", 25, 1, 10000),
    }


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _variant_keep_token_matches(variant, token):
    token = str(token or "").strip().lower()
    name = str(variant.get("name") if isinstance(variant, dict) else variant["name"] or "").strip().lower()
    height = _safe_int(variant.get("height") if isinstance(variant, dict) else variant["height"], 0)
    media_kind = str(variant.get("media_kind") if isinstance(variant, dict) else variant["media_kind"] or "variant").strip().lower()
    if media_kind != "variant":
        return True
    if token == "original" and name == "original":
        return True
    if token.endswith("p") and token[:-1].isdigit():
        return height == int(token[:-1])
    if token.isdigit():
        return height == int(token)
    return name == token


def _variant_rel_dir(row):
    playlist_path = str(row["playlist_path"] or "")
    if not playlist_path:
        return ""
    return str(Path(playlist_path).parent)


def _remove_variant_rows(conn, *, storage_root, variants):
    removed = {"variants_removed": 0, "segments_removed": 0, "bytes_removed": 0, "dirs_removed": []}
    for row in variants or []:
        variant_id = int(row["id"])
        for segment in conn.execute("SELECT path, byte_size FROM media_stream_segments WHERE variant_id=?", (variant_id,)).fetchall():
            removed["segments_removed"] += 1
            removed["bytes_removed"] += _safe_int(segment["byte_size"], 0)
        rel_dir = _variant_rel_dir(row)
        if rel_dir:
            try:
                target = resolve_storage_path(storage_root, rel_dir)
                if target.exists():
                    removed["bytes_removed"] += _directory_total_bytes(target)
                    shutil.rmtree(target, ignore_errors=True)
                    removed["dirs_removed"].append(rel_dir)
            except Exception:
                pass
        conn.execute("DELETE FROM media_stream_segments WHERE variant_id=?", (variant_id,))
        conn.execute("DELETE FROM media_stream_variants WHERE id=?", (variant_id,))
        removed["variants_removed"] += 1
    return removed


def _rewrite_master_manifest_from_db(conn, *, asset_id, storage_root):
    asset = conn.execute("SELECT * FROM media_stream_assets WHERE id=?", (int(asset_id),)).fetchone()
    if not asset or not asset["master_manifest_path"]:
        return False
    variants = conn.execute(
        "SELECT * FROM media_stream_variants WHERE asset_id=? AND media_kind='variant' ORDER BY id ASC",
        (int(asset_id),),
    ).fetchall()
    audio_tracks = conn.execute(
        "SELECT * FROM media_stream_variants WHERE asset_id=? AND media_kind='audio' ORDER BY is_default DESC, id ASC",
        (int(asset_id),),
    ).fetchall()
    if not variants:
        return False
    manifest_rows = []
    for row in variants:
        manifest_rows.append({
            "name": str(row["name"] or ""),
            "playlist_name": Path(str(row["playlist_path"] or "playlist.m3u8")).name or "playlist.m3u8",
            "bitrate": _safe_int(row["bitrate"], 0),
            "width": _safe_int(row["width"], 0),
            "height": _safe_int(row["height"], 0),
            "codec": str(row["codec"] or ""),
        })
    audio_rows = []
    for row in audio_tracks:
        audio_rows.append({
            "name": str(row["name"] or ""),
            "playlist_name": Path(str(row["playlist_path"] or "playlist.m3u8")).name or "playlist.m3u8",
            "bitrate": _safe_int(row["bitrate"], 0),
            "codec": str(row["codec"] or "aac"),
            "label": str(row["label"] or row["language"] or row["name"] or "音軌"),
            "language": str(row["language"] or "und"),
            "is_default": bool(row["is_default"]),
        })
    target = resolve_storage_path(storage_root, str(asset["master_manifest_path"] or ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_master_manifest_variants(target, manifest_rows, audio_tracks=audio_rows)
    conn.execute("UPDATE media_stream_assets SET updated_at=? WHERE id=?", (_now(), int(asset_id)))
    return True


def prune_stream_asset_variants(conn, *, uploaded_file_id, storage_root, keep_variants=None, dry_run=False):
    ensure_media_stream_schema(conn)
    file_id = str(uploaded_file_id or "").strip()
    asset = _asset_row(conn, file_id)
    if not asset or str(asset["status"] or "") != "ready":
        return {"file_id": file_id, "changed": False, "reason": "asset_not_ready"}
    keep_variants = list(keep_variants or ["original", "480p"])
    variants = conn.execute(
        "SELECT * FROM media_stream_variants WHERE asset_id=? AND media_kind='variant' ORDER BY id ASC",
        (int(asset["id"]),),
    ).fetchall()
    remove_rows = []
    keep_rows = []
    for row in variants:
        if any(_variant_keep_token_matches(row, token) for token in keep_variants):
            keep_rows.append(row)
        else:
            remove_rows.append(row)
    if not remove_rows:
        return {"file_id": file_id, "changed": False, "reason": "already_pruned", "kept": [str(row["name"] or "") for row in keep_rows]}
    if not keep_rows:
        return {"file_id": file_id, "changed": False, "reason": "no_safe_variant_to_keep"}
    result = {
        "file_id": file_id,
        "changed": True,
        "action": "prune_variants",
        "kept": [str(row["name"] or "") for row in keep_rows],
        "removed": [str(row["name"] or "") for row in remove_rows],
        "dry_run": bool(dry_run),
    }
    if dry_run:
        return result
    removed = _remove_variant_rows(conn, storage_root=storage_root, variants=remove_rows)
    _rewrite_master_manifest_from_db(conn, asset_id=int(asset["id"]), storage_root=storage_root)
    result.update(removed)
    return result


def _video_hls_usage_row(conn, *, now, video_id):
    cut24 = _iso(now - timedelta(days=1))
    cut7 = _iso(now - timedelta(days=7))
    cut30 = _iso(now - timedelta(days=30))
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN counted=1 AND created_at>=? THEN 1 ELSE 0 END), 0) AS plays_24h,
            COALESCE(SUM(CASE WHEN counted=1 AND created_at>=? THEN 1 ELSE 0 END), 0) AS plays_7d,
            COALESCE(SUM(CASE WHEN counted=1 AND created_at>=? THEN 1 ELSE 0 END), 0) AS plays_30d,
            MAX(CASE WHEN counted=1 THEN created_at ELSE NULL END) AS last_played_at
        FROM video_views
        WHERE video_id=?
        """,
        (cut24, cut7, cut30, int(video_id)),
    ).fetchone()
    return {
        "plays_24h": _safe_int(row["plays_24h"] if row else 0, 0),
        "plays_7d": _safe_int(row["plays_7d"] if row else 0, 0),
        "plays_30d": _safe_int(row["plays_30d"] if row else 0, 0),
        "last_played_at": str(row["last_played_at"] or "") if row else "",
    }


def _days_since(value, now):
    if not value:
        return 999999
    try:
        return max(0, (now - datetime.fromisoformat(str(value))).days)
    except Exception:
        return 999999


def _video_hls_policy_action(video_row, usage, policy, *, now):
    age_days = _days_since(video_row["created_at"], now)
    last_played_days = _days_since(usage.get("last_played_at"), now)
    plays_30d = _safe_int(usage.get("plays_30d"), 0)
    visibility = str(video_row["visibility"] or "public")
    active_share_count = _safe_int(video_row["active_share_count"], 0) if "active_share_count" in video_row.keys() else 0
    if age_days < policy["protect_days_after_upload"]:
        return "keep", "protected_new_upload"
    if last_played_days >= policy["very_cold_no_play_days"]:
        if visibility == "private" and active_share_count <= 0:
            return "delete_all", "very_cold_private_no_share"
        return "prune_mobile_floor", "very_cold_keep_mobile_floor"
    if plays_30d < policy["cold_plays_30d_below"] and last_played_days >= policy["cold_no_play_days"]:
        return "prune_mobile_floor", "cold_keep_mobile_floor"
    if plays_30d < policy["warm_plays_30d_below"]:
        return "prune", "warm_storage_saver"
    return "keep", "active_or_hot"


def run_video_hls_cold_cleanup(conn, *, storage_root, settings=None, now=None, dry_run=False):
    ensure_media_stream_schema(conn)
    now = now or datetime.now()
    policy = cold_hls_policy(settings)
    result = {
        "enabled": bool(policy["enabled"]),
        "dry_run": bool(dry_run),
        "policy": policy,
        "checked": 0,
        "kept": 0,
        "pruned": 0,
        "deleted": 0,
        "skipped": 0,
        "items": [],
    }
    if not policy["enabled"]:
        return result
    rows = conn.execute(
        """
        SELECT v.id AS video_id, v.cloud_file_id, v.created_at, v.visibility, v.status AS video_status,
               a.id AS asset_id, a.status AS asset_status, a.updated_at AS asset_updated_at,
               (
                   SELECT COUNT(*)
                   FROM video_share_links sl
                   WHERE sl.video_id = v.id
                     AND sl.revoked_at IS NULL
                     AND (sl.expires_at IS NULL OR sl.expires_at='' OR sl.expires_at>?)
               ) AS active_share_count
        FROM videos v
        JOIN media_stream_assets a ON a.uploaded_file_id = v.cloud_file_id
        WHERE v.deleted_at IS NULL AND v.status='ready' AND a.status='ready'
        ORDER BY a.updated_at ASC
        LIMIT ?
        """,
        (_iso(now), int(policy["max_assets_per_run"]),),
    ).fetchall()
    for row in rows:
        result["checked"] += 1
        usage = _video_hls_usage_row(conn, now=now, video_id=int(row["video_id"]))
        action, reason = _video_hls_policy_action(row, usage, policy, now=now)
        item = {
            "video_id": int(row["video_id"]),
            "file_id": str(row["cloud_file_id"] or ""),
            "action": action,
            "reason": reason,
            "active_share_count": _safe_int(row["active_share_count"], 0),
            **usage,
        }
        if action == "keep":
            result["kept"] += 1
        elif action == "prune":
            prune = prune_stream_asset_variants(
                conn,
                uploaded_file_id=row["cloud_file_id"],
                storage_root=storage_root,
                keep_variants=policy["warm_keep_variants"],
                dry_run=dry_run,
            )
            item["prune"] = prune
            if prune.get("changed"):
                result["pruned"] += 1
            else:
                result["skipped"] += 1
        elif action == "prune_mobile_floor":
            prune = prune_stream_asset_variants(
                conn,
                uploaded_file_id=row["cloud_file_id"],
                storage_root=storage_root,
                keep_variants=policy["mobile_floor_keep_variants"],
                dry_run=dry_run,
            )
            item["prune"] = prune
            if prune.get("changed"):
                result["pruned"] += 1
            else:
                result["skipped"] += 1
        elif action == "delete_all":
            if dry_run:
                item["cleanup"] = {"removed": True, "dry_run": True}
            else:
                item["cleanup"] = cleanup_stream_asset(conn, uploaded_file_id=row["cloud_file_id"], storage_root=storage_root)
            result["deleted"] += 1
        else:
            result["skipped"] += 1
        result["items"].append(item)
    return result

def _premium_hls_profile_policy(status=None):
    variants = (status or {}).get("variants") if isinstance(status, dict) else []
    audio_tracks = (status or {}).get("audio_tracks") if isinstance(status, dict) else []
    present_names = [
        str(item.get("name") or "")
        for item in (variants or [])
        if isinstance(item, dict)
    ]
    present_names = [name for name in present_names if name]
    has_original = "original" in present_names if variants is not None else None
    quality_heights = sorted({
        _safe_int(item.get("height"), 0)
        for item in (variants or [])
        if isinstance(item, dict)
        and str(item.get("name") or "") != "original"
        and _safe_int(item.get("height"), 0) > 0
    })
    audio_bitrates = [
        _safe_int(item.get("bitrate"), 0)
        for item in (audio_tracks or [])
        if isinstance(item, dict) and _safe_int(item.get("bitrate"), 0) > 0
    ]
    asset_audio_bitrate = max(audio_bitrates) if audio_bitrates else 0
    prepared = bool(variants)
    asset_profile = "not_prepared"
    asset_candidates = []
    asset_profile_confidence = "none"
    if prepared:
        if has_original:
            asset_profile = "full"
            asset_candidates = ["full"]
            asset_profile_confidence = "high"
        elif quality_heights == [480, 720]:
            asset_profile = "storage_saver"
            asset_candidates = ["storage_saver"]
            asset_profile_confidence = "high"
        elif quality_heights == [480]:
            if asset_audio_bitrate and asset_audio_bitrate <= 128000:
                asset_profile = "mobile_saver"
                asset_candidates = ["mobile_saver"]
                asset_profile_confidence = "high"
            elif asset_audio_bitrate and asset_audio_bitrate > 128000:
                asset_profile = "storage_saver"
                asset_candidates = ["storage_saver"]
                asset_profile_confidence = "medium"
            else:
                asset_profile = "storage_saver"
                asset_candidates = ["storage_saver", "mobile_saver"]
                asset_profile_confidence = "ambiguous"
        else:
            asset_profile = "custom"
            asset_candidates = ["custom"]
            asset_profile_confidence = "high"

    current_profile = _hls_effective_profile()
    original_mode = _hls_original_variant_mode()
    current_audio_bitrate = _hls_audio_bitrate()
    target_audio_bitrate = _audio_bitrate_to_bits_per_second(current_audio_bitrate)
    expected_heights = _hls_quality_heights()
    if not prepared:
        profile_matches_asset = None
    elif current_profile in asset_candidates:
        profile_matches_asset = True
    else:
        original_matches = True
        if original_mode == "always":
            original_matches = bool(has_original)
        elif original_mode == "never":
            original_matches = not bool(has_original)
        heights_match = True
        if quality_heights:
            heights_match = bool(expected_heights) and all(height in expected_heights for height in quality_heights)
        audio_matches = True
        if asset_audio_bitrate:
            audio_matches = int(asset_audio_bitrate) == int(target_audio_bitrate)
        profile_matches_asset = bool(original_matches and heights_match and audio_matches)
    profile_drift = profile_matches_asset is False
    rebuild_reason = ""
    if not prepared:
        rebuild_reason = "hls_not_ready"
    elif profile_drift:
        rebuild_reason = "current_profile_policy_differs_from_prepared_asset"
    return {
        "version": "2026.05.28-profile-drift-v1",
        "current_profile": current_profile,
        "requested_profile": current_profile,
        "quality_heights": expected_heights,
        "audio_bitrate": current_audio_bitrate,
        "original_variant_mode": original_mode,
        "original_variant_present": has_original,
        "asset_status": str((status or {}).get("status") or "not_prepared") if isinstance(status, dict) else "not_prepared",
        "asset_profile": asset_profile,
        "asset_profile_candidates": asset_candidates,
        "asset_profile_confidence": asset_profile_confidence,
        "asset_profile_ambiguous": asset_profile_confidence == "ambiguous",
        "asset_variant_names": present_names,
        "asset_quality_heights": quality_heights,
        "asset_audio_bitrate": asset_audio_bitrate,
        "profile_matches_asset": profile_matches_asset,
        "profile_drift": profile_drift,
        "rebuild_recommended": bool(profile_drift),
        "rebuild_reason": rebuild_reason,
        "profile_env": "HACKME_MEDIA_HLS_PROFILE",
        "quality_heights_env": "HACKME_MEDIA_HLS_QUALITY_HEIGHTS",
        "audio_bitrate_env": "HACKME_MEDIA_HLS_AUDIO_BITRATE",
        "original_variant_env": "HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE",
        "profiles": [
            {
                "id": "full",
                "label": "Premium Full",
                "relative_fee": "premium_highest",
                "quality_heights": [480, 720],
                "includes_original_variant": True,
                "audio_bitrate": DEFAULT_HLS_AUDIO_BITRATE,
                "storage_cost": "highest",
                "best_for": ["原畫質需求", "大螢幕觀看", "最高相容性"],
                "tradeoffs": ["衍生檔儲存最高", "清理與快取成本最高"],
            },
            {
                "id": "storage_saver",
                "label": "Premium Storage Saver",
                "relative_fee": "premium_balanced",
                "quality_heights": [480, 720],
                "includes_original_variant": False,
                "audio_bitrate": DEFAULT_HLS_AUDIO_BITRATE,
                "storage_cost": "medium",
                "best_for": ["需要 720p", "重視儲存成本", "多人分享"],
                "tradeoffs": ["不提供 original HLS rendition"],
            },
            {
                "id": "mobile_saver",
                "label": "Premium Mobile Saver",
                "relative_fee": "premium_lowest",
                "quality_heights": [480],
                "includes_original_variant": False,
                "audio_bitrate": "128k",
                "storage_cost": "lowest",
                "best_for": ["手機觀看", "低費率 Premium", "長片庫大量保存"],
                "tradeoffs": ["不提供 720p/original HLS rendition"],
            },
        ],
    }


def _streaming_service_options(
    *,
    direct_available,
    direct_reason="",
    realtime_proxy_available,
    realtime_proxy_reason="realtime_proxy_not_enabled",
    realtime_proxy_status="disabled",
    prepared_hls_available,
    media_type,
    status,
):
    premium_profile_policy = _premium_hls_profile_policy(status)
    source_status = str((status or {}).get("status") or "not_prepared")
    prepared_reason = "ready" if prepared_hls_available else source_status
    if prepared_reason == "not_prepared":
        prepared_reason = "hls_not_prepared"
    premium_summary_suffix = " 目前資產與現行 Premium profile 不一致，建議排程重建 HLS。" if premium_profile_policy.get("profile_drift") else ""
    proxy_reason = "available" if realtime_proxy_available else (realtime_proxy_reason or "realtime_proxy_not_enabled")
    return [
        {
            "mode": "realtime_proxy",
            "label": "即時轉封裝",
            "service_tier": "standard",
            "service_tier_label": "Standard",
            "fee_level": "middle",
            "fee_label": "中",
            "billing_basis": "per_viewer_cpu",
            "available": bool(realtime_proxy_available),
            "availability_reason": proxy_reason,
            "implementation_status": realtime_proxy_status or ("ready" if realtime_proxy_available else "disabled"),
            "requires_preparation": False,
            "requires_background_job": False,
            "derivative_storage": False,
            "seek_quality": "approximate",
            "server_cost": "per_viewer_cpu",
            "cost_drivers": ["ffmpeg_process_per_viewer", "audio_transcode_cpu", "process_cleanup"],
            "advantages": [
                "不必等待完整 HLS 預處理",
                "可針對選定音軌即時輸出瀏覽器較好播放的格式",
                "比原始檔直出更能處理 MKV 或特殊音訊來源",
            ],
            "tradeoffs": [
                "每位觀看者都會消耗即時 CPU",
                "跳轉時間較近似，不能保證像 HLS 一樣穩定",
                "不適合多人同時觀看同一支熱門影片",
            ],
            "best_for": ["需要快速上線的 MKV", "少量觀看的多國語音影片", "不想先保存衍生檔的情境"],
            "media_support": {
                "media_type": media_type,
                "multi_quality": False,
                "multi_audio": "selected_track_only",
                "multi_subtitle": "external_or_browser_dependent",
                "share_ready": True,
                "cache_friendly": False,
            },
            "customer_summary": "中階費率；用即時 CPU 換取較好的格式相容性，但併發越高成本越高。",
            "notes": "可即時選音軌並轉成瀏覽器較容易播放的格式；跳轉時間為近似，每位觀看者都會消耗即時 CPU。Production 應以 feature flag、併發限制與程序 timeout 控制。",
        },
        {
            "mode": "prepared_hls",
            "label": "預處理 HLS",
            "service_tier": "premium",
            "service_tier_label": "Premium",
            "fee_level": "highest",
            "fee_label": "最高",
            "billing_basis": "preprocess_storage_cache",
            "available": bool(prepared_hls_available),
            "availability_reason": prepared_reason,
            "implementation_status": "ready",
            "requires_preparation": True,
            "requires_background_job": True,
            "derivative_storage": True,
            "seek_quality": "stable",
            "server_cost": "preprocess_and_storage",
            "cost_drivers": ["worker_cpu", "derivative_storage", "segment_indexing", "cache_lifecycle", "cleanup"],
            "profile_policy": premium_profile_policy,
            "advantages": [
                "可分段播放與快取",
                "支援多畫質、多音軌與 WebVTT 字幕選單",
                "分享影音與分享檔案預覽可一致套用授權",
                "多人觀看時比即時轉封裝穩定",
            ],
            "tradeoffs": [
                "上傳或發布後需要等待背景處理",
                "平台需要保存並清理串流衍生檔",
                "轉檔與抽取字幕會消耗 worker 資源",
            ],
            "best_for": ["大檔案", "公開影音", "多人觀看", "多音軌", "多字幕", "需要穩定分享"],
            "media_support": {
                "media_type": media_type,
                "multi_quality": True,
                "multi_audio": True,
                "multi_subtitle": True,
                "share_ready": True,
                "cache_friendly": True,
            },
            "customer_summary": "最高費率；平台先建立可快取、可授權、可分段播放的服務版本。" + premium_summary_suffix,
            "notes": "預先建立可快取的 HLS 衍生檔，支援多畫質、多音軌與較穩定跳轉。",
        },
    ]


def _realtime_proxy_audio_tracks_for_payload(file_row, *, storage_root=None, ffprobe_bin="ffprobe"):
    if not storage_root:
        return []
    try:
        path = resolve_file_storage_path(storage_root, file_row)
    except Exception:
        return []
    if not path.exists():
        return []
    try:
        return realtime_proxy_audio_tracks_for_source(path, ffprobe_bin=ffprobe_bin)
    except Exception:
        return []


def _realtime_proxy_probe_for_payload(file_row, *, storage_root=None, ffprobe_bin="ffprobe"):
    if not storage_root:
        return {}
    try:
        path = resolve_file_storage_path(storage_root, file_row)
    except Exception:
        return {}
    if not path.exists():
        return {}
    try:
        metadata = _parse_probe_metadata(_run_probe(path, ffprobe_bin=ffprobe_bin))
        return {
            "audio_tracks": _realtime_proxy_audio_track_rows(metadata),
            "mse_content_type": realtime_proxy_mse_content_type(metadata),
        }
    except Exception:
        return {}


def _source_file_available(file_row, *, storage_root=None):
    if not storage_root:
        return True
    try:
        return resolve_file_storage_path(storage_root, file_row).is_file()
    except Exception:
        return False


def _storage_relative_file_available(storage_root, relative_path):
    if not storage_root:
        return bool(relative_path)
    try:
        return resolve_storage_path(storage_root, relative_path).is_file()
    except Exception:
        return False


def stream_playback_payload(conn, *, file_row, video_id, storage_root=None, ffprobe_bin="ffprobe"):
    status = get_stream_status(conn, file_row=file_row, include_segments=False)
    media_type = _file_media_type(file_row)
    direct_url = f"/api/videos/{int(video_id)}/stream"
    source_file_available = _source_file_available(file_row, storage_root=storage_root)
    direct_browser_status = direct_browser_playback_status(file_row, storage_root=storage_root, ffprobe_bin=ffprobe_bin) if source_file_available else {
        "available": False,
        "reason": "source_file_missing",
        "detail": "原始影音實體檔案不存在。",
    }
    # Published video playback deliberately avoids Direct. Direct is reserved for
    # cloud-drive preview of browser-native files; videos use HLS or realtime proxy.
    direct_fallback_allowed = False
    realtime_proxy_state = realtime_proxy_availability(file_row)
    if not source_file_available:
        realtime_proxy_state = {
            **realtime_proxy_state,
            "available": False,
            "reason": "source_file_missing",
            "implementation_status": "source_missing",
        }
    realtime_proxy_available = bool(source_file_available and realtime_proxy_state.get("available"))
    realtime_proxy_probe = _realtime_proxy_probe_for_payload(file_row, storage_root=storage_root, ffprobe_bin=ffprobe_bin) if realtime_proxy_available else {}
    realtime_proxy_url = f"/api/videos/{int(video_id)}/realtime-proxy" if realtime_proxy_available else ""
    master_manifest_available = bool(
        status
        and status.get("status") == "ready"
        and status.get("master_manifest_path")
        and _storage_relative_file_available(storage_root, status.get("master_manifest_path"))
    )
    available_variants = [
        variant for variant in (status.get("variants") if status else []) or []
        if variant.get("name") and variant.get("playlist_path") and _storage_relative_file_available(storage_root, variant.get("playlist_path"))
    ]
    available_audio_tracks = [
        track for track in (status.get("audio_tracks") if status else []) or []
        if track.get("name") and track.get("playlist_path") and _storage_relative_file_available(storage_root, track.get("playlist_path"))
    ]
    available_subtitles = [
        subtitle for subtitle in (status.get("subtitles") if status else []) or []
        if subtitle.get("name") and subtitle.get("path") and _storage_relative_file_available(storage_root, subtitle.get("path"))
    ]
    prepared_hls_available = bool(
        master_manifest_available
        and (
            available_variants
            or (media_type == "audio" and available_audio_tracks)
        )
    )
    payload = {
        "mode": "realtime_proxy" if realtime_proxy_available else "waiting_stream",
        "media_type": media_type,
        "source_mode": str(file_row["privacy_mode"] or "standard_plain"),
        "fallback_url": "",
        "stream_url": "",
        "realtime_proxy_url": realtime_proxy_url,
        "master_url": "",
        "hls_js_url": HLS_JS_URL,
        "player_strategy": "realtime_proxy" if realtime_proxy_available else "hls_required",
            "stream_warning": "目前使用即時轉封裝。" if realtime_proxy_available else "影音播放不使用 Direct；HLS 尚未就緒且即時轉封裝不可用。",
        "status": status,
        "variants": [],
        "audio_tracks": [],
        "subtitles": [],
        "streaming_ready": False,
        "direct_fallback_allowed": direct_fallback_allowed,
        "direct_browser_playback": direct_browser_status,
        "service_policy": {
            "version": "2026.05.28-002",
            "customer_selectable_modes": ["realtime_proxy", "prepared_hls"],
            "default_mode": "prepared_hls",
            "recommended_mode": "prepared_hls",
            "multi_track_recommended_mode": "prepared_hls",
            "fee_model": "basic_standard_premium",
            "fee_difference_reason": "服務費差異來自檔案流量、每位觀看者即時 CPU、預處理 worker CPU、衍生檔儲存與快取清理成本。",
            "strict_e2ee_server_transcode": False,
            "realtime_proxy_enabled": realtime_proxy_enabled(),
            "realtime_proxy_max_concurrent": realtime_proxy_max_concurrent(),
            "premium_hls_profile_policy": _premium_hls_profile_policy(status),
        },
        "realtime_proxy": {
            **realtime_proxy_state,
            "url": realtime_proxy_url,
            "selected_audio_query_param": "audio",
            "start_seconds_query_param": "start",
            "output_container": "fragmented_mp4",
            "mse_content_type": str(realtime_proxy_probe.get("mse_content_type") or 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
            "video_strategy": "mobile_h264_baseline_transcode",
            "audio_strategy": "selected_track_to_aac_stereo",
        },
        "streaming_options": _streaming_service_options(
            direct_available=False,
            direct_reason="video_direct_disabled",
            realtime_proxy_available=realtime_proxy_available,
            realtime_proxy_reason=str(realtime_proxy_state.get("reason") or ""),
            realtime_proxy_status=str(realtime_proxy_state.get("implementation_status") or ""),
            prepared_hls_available=prepared_hls_available,
            media_type=media_type,
            status=status,
        ),
    }
    if available_subtitles:
        payload["subtitles"] = [
            {
                "name": str(item.get("name") or ""),
                "label": str(item.get("label") or item.get("language") or "字幕"),
                "language": str(item.get("language") or "und"),
                "is_default": bool(item.get("is_default")),
                "is_forced": bool(item.get("is_forced")),
                "url": f"/api/videos/{int(video_id)}/hls/subtitles/{item.get('name')}.vtt",
            }
            for item in available_subtitles
            if item.get("name")
        ]
    if prepared_hls_available:
        variants = []
        audio_tracks = []
        muxed_audio_present = False
        for variant in available_variants:
            name = str(variant.get("name") or "").strip()
            if not name:
                continue
            width = _safe_int(variant.get("width"), 0)
            height = _safe_int(variant.get("height"), 0)
            label = "原畫質" if name == "original" else (f"{height}p" if height else name)
            if name == "original" and height:
                label = f"原畫質 {height}p"
            codec_text = str(variant.get("codec") or "")
            variant_has_muxed_audio = "mp4a" in hls_codec_string(width=width or 1, height=height or 1, codec=codec_text).lower()
            if variant_has_muxed_audio:
                muxed_audio_present = True
            variants.append({
                "name": name,
                "label": label,
                "width": width,
                "height": height,
                "bitrate": _safe_int(variant.get("bitrate"), 0),
                "codec": codec_text,
                "has_muxed_audio": variant_has_muxed_audio,
                "size_bytes": _safe_int(variant.get("segments_total_bytes"), 0),
                "segments_total_bytes": _safe_int(variant.get("segments_total_bytes"), 0),
                "source_size_bytes": _safe_int(status.get("source_size_bytes"), 0),
                "playlist_url": f"/api/videos/{int(video_id)}/hls/{name}/playlist.m3u8",
            })
        for track in available_audio_tracks:
            name = str(track.get("name") or "").strip()
            if not name:
                continue
            audio_tracks.append({
                "name": name,
                "label": str(track.get("label") or track.get("language") or name),
                "language": str(track.get("language") or "und"),
                "is_default": bool(track.get("is_default")),
                "codec": str(track.get("codec") or ""),
                "bitrate": _safe_int(track.get("bitrate"), 0),
                "stream_index": _safe_int(track.get("stream_index"), -1),
                "size_bytes": _safe_int(track.get("segments_total_bytes"), 0),
                "segments_total_bytes": _safe_int(track.get("segments_total_bytes"), 0),
                "playlist_url": f"/api/videos/{int(video_id)}/hls/{name}/playlist.m3u8",
            })
        default_quality = _preferred_playback_quality_name(variants)
        fallback_quality = _fallback_playback_quality_name(variants)
        premium_policy = _premium_hls_profile_policy(status)
        if not audio_tracks and muxed_audio_present:
            audio_tracks.append({
                "name": "muxed_audio",
                "label": "內嵌音軌",
                "language": "und",
                "is_default": True,
                "codec": "aac",
                "bitrate": _safe_int(premium_policy.get("asset_audio_bitrate"), 0),
                "stream_index": -1,
                "size_bytes": 0,
                "segments_total_bytes": 0,
                "playlist_url": "",
                "source": "muxed_hls_variant",
                "selectable": False,
            })
        payload["mode"] = "hls"
        payload["master_url"] = f"/api/videos/{int(video_id)}/hls/master.m3u8"
        payload["player_strategy"] = "native_hls_or_hlsjs"
        payload["stream_warning"] = ""
        payload["streaming_ready"] = True
        payload["variants"] = variants
        payload["audio_tracks"] = audio_tracks
        payload["default_quality"] = default_quality
        payload["fallback_quality"] = fallback_quality
        payload["quality_policy"] = {
            "default_height": 720,
            "fallback_height": 480,
            "default_quality": default_quality,
            "fallback_quality": fallback_quality,
            "derivatives_quota_exempt": HLS_DERIVATIVES_QUOTA_EXEMPT,
            "larger_derivatives_hidden": True,
            "hls_profile": premium_policy["current_profile"],
            "hls_audio_bitrate": premium_policy["audio_bitrate"],
            "original_variant_mode": premium_policy["original_variant_mode"],
            "original_variant_present": premium_policy["original_variant_present"],
            "asset_profile": premium_policy["asset_profile"],
            "asset_profile_candidates": premium_policy["asset_profile_candidates"],
            "asset_profile_confidence": premium_policy["asset_profile_confidence"],
            "asset_quality_heights": premium_policy["asset_quality_heights"],
            "asset_audio_bitrate": premium_policy["asset_audio_bitrate"],
            "profile_matches_asset": premium_policy["profile_matches_asset"],
            "profile_drift": premium_policy["profile_drift"],
            "rebuild_recommended": premium_policy["rebuild_recommended"],
            "rebuild_reason": premium_policy["rebuild_reason"],
            "note": "480p/720p/1080p HLS 衍生檔是服務端串流快取，不計入上傳者雲端硬碟配額；若衍生檔比原畫質更大會自動刪除並隱藏。",
        }
    if realtime_proxy_available and not payload["audio_tracks"]:
        payload["audio_tracks"] = [
            {
                "name": str(track.get("name") or ""),
                "label": str(track.get("label") or track.get("language") or track.get("name") or "音軌"),
                "language": str(track.get("language") or "und"),
                "is_default": bool(track.get("is_default")),
                "codec": str(track.get("codec") or ""),
                "bitrate": _safe_int(track.get("bitrate"), 0),
                "stream_index": _safe_int(track.get("stream_index"), -1),
                "channels": _safe_int(track.get("channels"), 0),
                "channel_layout": str(track.get("channel_layout") or ""),
                "playlist_url": "",
                "source": "realtime_proxy_probe",
            }
            for track in (realtime_proxy_probe.get("audio_tracks") or [])
            if track.get("name")
        ]
    return payload
