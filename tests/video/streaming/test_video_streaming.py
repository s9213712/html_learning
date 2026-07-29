import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Flask, jsonify, make_response

import routes.videos as video_routes
import scripts.media.hls_prepare_worker as hls_prepare_worker
from services.media.e2ee_streaming import (
    _normalize_manifest,
    cleanup_e2ee_stream_v2_assets,
    resolve_e2ee_chunk_response,
    upsert_e2ee_stream_v2_variant,
)
from services.media import streaming as media_streaming
from services.job_center import create_job, get_job_by_source, update_job
from routes.videos import register_video_routes
from services.storage.cloud_drive import encrypt_server_encrypted_chunked_stream, ensure_cloud_drive_attachment_schema
from services.system.audit import audit as runtime_audit
from services.system.audit import configure_audit_service
from services.users.member_levels import ensure_member_level_rules_schema
from services.storage.storage_albums import ensure_storage_album_schema
from services.security.upload_security import ensure_upload_security_schema, update_cloud_drive_security_policy
from services.media.videos import ensure_video_schema, publish_video


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _passthrough(fn):
    return fn


def _init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            nickname TEXT,
            role TEXT NOT NULL
        );
        INSERT INTO users (id, username, nickname, role) VALUES
            (1, 'alice', 'Alice', 'user'),
            (2, 'viewer', 'Viewer', 'user'),
            (3, 'manager', 'Manager', 'manager');
        """
    )
    ensure_member_level_rules_schema(conn)
    ensure_upload_security_schema(conn)
    ensure_cloud_drive_attachment_schema(conn)
    ensure_storage_album_schema(conn)
    update_cloud_drive_security_policy(conn, {"scanner_enabled": False})
    conn.commit()
    conn.close()


def _build_app(db_path, storage_root, fernet, current_user, *, audit_func=None, extra_deps=None):
    public_dir = str(Path(__file__).resolve().parents[3] / "public")
    app = Flask(__name__, static_folder=public_dir, static_url_path="")
    app.testing = True
    app.secret_key = "video-streaming-test-secret"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    deps = {
        "STORAGE_DIR": str(storage_root),
        "audit": audit_func or (lambda *args, **kwargs: None),
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: current_user,
        "get_db": get_db,
        "get_system_settings": lambda: {},
        "get_member_level_rule": lambda conn, level: {
            "can_upload_attachment": True,
            "attachment_quota_mb": 100,
            "max_attachment_size_mb": 100,
            "upload_rate_limit_per_day": 50,
        },
        "get_ua": lambda: "pytest-video-streaming",
        "json_resp": _json_resp,
        "points_service": None,
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "server_file_fernet": fernet,
    }
    deps.update(extra_deps or {})
    register_video_routes(app, deps)
    return app


def _share_session_query(unlock_response):
    payload = unlock_response.get_json() or {}
    share_session_id = str(payload.get("share_session_id") or "").strip()
    assert share_session_id
    return f"?share_session={share_session_id}"


def _configure_real_audit_for_test(db_path, runtime_root):
    runtime_root.mkdir(parents=True, exist_ok=True)
    log_dir = runtime_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    anchor_dir = runtime_root / "anchors"
    anchor_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    def get_db():
        audit_conn = sqlite3.connect(db_path)
        audit_conn.row_factory = sqlite3.Row
        return audit_conn

    configure_audit_service(
        get_db=get_db,
        chain_seed="pytest-video-audit-seed",
        integrity_key=b"pytest-video-audit-key-32-bytes!",
        audit_log_path=str(log_dir / "audit.log"),
        audit_anchor_path=str(anchor_dir / "audit_anchors.log"),
        audit_anchor_latest_path=str(anchor_dir / "audit_latest_anchor.json"),
        audit_anchor_interval_seconds=0,
    )
    return runtime_audit


def _seed_uploaded_file(conn, storage_root, *, file_id, owner_user_id, filename, mime, privacy_mode="standard_plain", payload=b"demo-media"):
    rel = f"users/{owner_user_id}/{file_id}/{filename}"
    target = storage_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    conn.execute(
        """
        INSERT INTO uploaded_files (
            id, owner_user_id, storage_path, privacy_mode, risk_level, scan_status,
            original_filename_plain_for_public, mime_type_plain_for_public, size_bytes, created_at
        ) VALUES (?, ?, ?, ?, 'low', 'clean', ?, ?, ?, '2026-01-01T00:00:00')
        """,
        (file_id, owner_user_id, rel, privacy_mode, filename, mime, len(payload)),
    )
    return target


def _fake_probe_payload(media_type="video"):
    streams = [{"codec_type": "audio", "codec_name": "aac", "bit_rate": "128000"}]
    if media_type == "video":
        streams.insert(0, {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720, "bit_rate": "900000"})
    return {
        "format": {
            "duration": "12.5",
            "bit_rate": "1024000",
        },
        "streams": streams,
    }


def _require_ffmpeg_tools():
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    if not ffmpeg_bin or not ffprobe_bin:
        pytest.skip("ffmpeg/ffprobe not available")
    return ffmpeg_bin, ffprobe_bin


def _ffprobe_json(path, *, ffprobe_bin="ffprobe"):
    result = subprocess.run(
        [
            str(ffprobe_bin),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout or "{}")


def _make_multiaudio_mkv_fixture(tmp_path, *, ffmpeg_bin="ffmpeg"):
    subtitle = tmp_path / "proxy-fixture.srt"
    subtitle.write_text("1\n00:00:00,200 --> 00:00:01,200\n字幕不應進入 Standard MP4\n", encoding="utf-8")
    output = tmp_path / "proxy-fixture.mkv"
    cmd = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=3:size=160x90:rate=15",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=3",
        "-i",
        str(subtitle),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:a:0",
        "-map",
        "3:s:0",
        "-metadata:s:a:0",
        "language=jpn",
        "-metadata:s:a:0",
        "title=Japanese",
        "-metadata:s:a:1",
        "language=eng",
        "-metadata:s:a:1",
        "title=English",
        "-metadata:s:s:0",
        "language=zh",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-c:s",
        "srt",
        "-shortest",
        str(output),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    return output


def _fake_hls_package(
    source_path,
    *,
    derivative_dir,
    media_type,
    variant_name=None,
    ffmpeg_bin="ffmpeg",
    segment_seconds=4,
    duration_seconds=0,
    source_height=0,
    target_height=0,
    target_bitrate=0,
    copy_codecs=False,
    video_only=False,
    audio_stream_index=None,
    progress_callback=None,
    **_kwargs,
):
    variant_name = variant_name or ("audio" if media_type == "audio" else "original")
    variant_dir = Path(derivative_dir) / variant_name
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "init.mp4").write_bytes(b"init-segment")
    (variant_dir / "seg_00001.m4s").write_bytes(b"segment-1")
    (variant_dir / "seg_00002.m4s").write_bytes(b"segment-2")
    (variant_dir / "playlist.m3u8").write_text(
        "#EXTM3U\n"
        "#EXT-X-VERSION:7\n"
        "#EXT-X-MAP:URI=\"init.mp4\"\n"
        "#EXTINF:4.0,\n"
        "seg_00001.m4s\n"
        "#EXTINF:3.5,\n"
        "seg_00002.m4s\n"
        "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    if progress_callback:
        progress_callback(1.0)
    return variant_name, variant_dir / "playlist.m3u8", variant_dir / "init.mp4"


def _mark_file_as_e2ee(conn, file_id):
    conn.execute(
        """
        UPDATE uploaded_files
        SET privacy_mode='e2ee',
            original_filename_encrypted='sealed:metadata',
            encryption_algorithm='AES-GCM',
            encryption_version='browser_passphrase_pbkdf2_v2',
            nonce='nonce-123',
            ciphertext_sha256=?
        WHERE id=?
        """,
        ("a" * 64, file_id),
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _seed_real_e2ee_file(conn, storage_root, *, file_id, owner_user_id, filename, mime, plaintext):
    file_key = AESGCM.generate_key(bit_length=256)
    file_aead = AESGCM(file_key)
    file_nonce = os.urandom(12)
    ciphertext = file_aead.encrypt(file_nonce, plaintext, None)
    metadata_nonce = os.urandom(12)
    encrypted_metadata = json.dumps(
        {
            "alg": "AES-GCM",
            "v": 1,
            "nonce": _b64(metadata_nonce),
            "ciphertext": _b64(
                file_aead.encrypt(
                    metadata_nonce,
                    json.dumps(
                        {
                            "filename": filename,
                            "mime_type": mime,
                            "size_bytes": len(plaintext),
                        }
                    ).encode("utf-8"),
                    None,
                )
            ),
        }
    )
    _seed_uploaded_file(
        conn,
        storage_root,
        file_id=file_id,
        owner_user_id=owner_user_id,
        filename=filename,
        mime=mime,
        privacy_mode="e2ee",
        payload=ciphertext,
    )
    conn.execute(
        """
        UPDATE uploaded_files
        SET privacy_mode='e2ee',
            original_filename_encrypted=?,
            encryption_algorithm='AES-GCM',
            encryption_version='browser-passphrase-v2',
            nonce=?,
            ciphertext_sha256=?,
            size_bytes=?
        WHERE id=?
        """,
        (
            encrypted_metadata,
            _b64(file_nonce),
            hashlib.sha256(ciphertext).hexdigest(),
            len(ciphertext),
            file_id,
        ),
    )

    share_key = AESGCM.generate_key(bit_length=256)
    share_nonce = os.urandom(12)
    share_envelope = json.dumps(
        {
            "alg": "AES-GCM",
            "v": 1,
            "nonce": _b64(share_nonce),
            "ciphertext": _b64(AESGCM(share_key).encrypt(share_nonce, file_key, None)),
        }
    )
    return {
        "plaintext": plaintext,
        "file_key": file_key,
        "file_nonce_b64": _b64(file_nonce),
        "encrypted_metadata": encrypted_metadata,
        "share_key": share_key,
        "share_fragment_key": _b64url(share_key),
        "share_wrapped_file_key_envelope": share_envelope,
    }


def _unwrap_shared_file_key_from_fragment(envelope_text, fragment_key):
    envelope = json.loads(envelope_text)
    share_key = _b64url_decode(fragment_key)
    return AESGCM(share_key).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["ciphertext"]), None)


def _decrypt_shared_metadata(file_key, encrypted_metadata):
    envelope = json.loads(encrypted_metadata)
    plaintext = AESGCM(file_key).decrypt(
        base64.b64decode(envelope["nonce"]),
        base64.b64decode(envelope["ciphertext"]),
        None,
    )
    return json.loads(plaintext.decode("utf-8"))


def _build_real_stream_v2_manifest_and_bundle(file_key, plaintext, *, content_type):
    aead = AESGCM(file_key)
    chunk_plaintext_size = 7
    chunks = []
    bundle_parts = []
    cipher_offset = 0
    plain_offset = 0
    chunk_index = 0
    while plain_offset < len(plaintext):
        chunk_plain = plaintext[plain_offset:plain_offset + chunk_plaintext_size]
        nonce = os.urandom(12)
        cipher = aead.encrypt(nonce, chunk_plain, None)
        chunks.append(
            {
                "chunk_index": chunk_index,
                "nonce": _b64(nonce),
                "ciphertext_offset": cipher_offset,
                "ciphertext_size": len(cipher),
                "plaintext_offset": plain_offset,
                "plaintext_size": len(chunk_plain),
                "ciphertext_sha256": hashlib.sha256(cipher).hexdigest(),
            }
        )
        bundle_parts.append(cipher)
        cipher_offset += len(cipher)
        plain_offset += len(chunk_plain)
        chunk_index += 1
    manifest = {
        "e2ee_stream_version": 2,
        "chunk_size": chunk_plaintext_size,
        "chunk_count": len(chunks),
        "content_type": content_type,
        "duration_hint": 3.5,
        "byte_range_hint": {"total_plaintext_bytes": len(plaintext)},
        "chunks": chunks,
    }
    return manifest, b"".join(bundle_parts)


def test_prepare_stream_asset_builds_hls_derivatives_for_plain_video(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="plain-video", owner_user_id=1, filename="clip.mp4", mime="video/mp4")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='plain-video'").fetchone()

    monkeypatch.setattr(media_streaming, "_run_probe", lambda *args, **kwargs: _fake_probe_payload("video"))
    monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", _fake_hls_package)

    asset = media_streaming.prepare_stream_asset(
        conn,
        file_row=row,
        storage_root=storage_root,
        server_file_fernet=None,
    )

    assert asset["status"] == "ready"
    assert asset["media_type"] == "video"
    assert asset["master_manifest_path"] == "media_derivatives/plain-video/master.m3u8"
    assert [variant["name"] for variant in asset["variants"]] == ["original", "q480"]
    assert [seg["filename"] for seg in asset["variants"][0]["segments"]] == ["seg_00001.m4s", "seg_00002.m4s"]
    master = storage_root / "media_derivatives" / "plain-video" / "master.m3u8"
    assert master.exists()
    master_text = master.read_text(encoding="utf-8")
    assert 'CODECS="avc1.64001f,mp4a.40.2"' in master_text
    assert 'CODECS="h264"' not in master_text


def test_prepare_stream_asset_can_skip_original_variant_for_storage_saver(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="storage-saver-video", owner_user_id=1, filename="clip.mp4", mime="video/mp4")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='storage-saver-video'").fetchone()

    monkeypatch.setenv("HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE", "never")
    monkeypatch.setattr(media_streaming, "_run_probe", lambda *args, **kwargs: _fake_probe_payload("video"))
    monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", _fake_hls_package)

    asset = media_streaming.prepare_stream_asset(
        conn,
        file_row=row,
        storage_root=storage_root,
        server_file_fernet=None,
    )

    assert [variant["name"] for variant in asset["variants"]] == ["q480"]
    master_text = (storage_root / "media_derivatives" / "storage-saver-video" / "master.m3u8").read_text(encoding="utf-8")
    assert "original/playlist.m3u8" not in master_text
    assert "q480/playlist.m3u8" in master_text
    playback = media_streaming.stream_playback_payload(conn, file_row=row, video_id=12)
    assert playback["default_quality"] == "q480"
    assert playback["quality_policy"]["hls_profile"] == "custom"
    assert playback["quality_policy"]["original_variant_mode"] == "never"
    assert playback["quality_policy"]["original_variant_present"] is False
    assert playback["quality_policy"]["asset_profile"] == "storage_saver"
    assert playback["quality_policy"]["profile_drift"] is False
    assert playback["service_policy"]["premium_hls_profile_policy"]["current_profile"] == "custom"
    assert playback["service_policy"]["premium_hls_profile_policy"]["profile_matches_asset"] is True


def test_hls_original_variant_skip_falls_back_when_no_quality_variant(monkeypatch):
    monkeypatch.setenv("HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE", "never")
    metadata = {
        "media_type": "video",
        "width": 854,
        "height": 480,
        "bitrate": 900000,
        "codec": "h264",
        "codec_tag": "avc1",
    }

    specs = media_streaming._hls_variant_specs(metadata)

    assert [spec["name"] for spec in specs] == ["original"]


def test_hls_mobile_saver_profile_sets_ladder_original_policy_and_audio_bitrate(monkeypatch):
    monkeypatch.setenv("HACKME_MEDIA_HLS_PROFILE", "mobile_saver")
    metadata = {
        "media_type": "video",
        "width": 1920,
        "height": 1080,
        "bitrate": 5_000_000,
        "codec": "h264",
        "codec_tag": "avc1",
    }

    specs = media_streaming._hls_variant_specs(metadata)

    assert media_streaming._hls_original_variant_mode() == "never"
    assert media_streaming._hls_audio_bitrate() == "128k"
    assert [spec["name"] for spec in specs] == ["q480"]


def test_premium_hls_profile_policy_detects_full_asset_drift_when_mobile_policy(monkeypatch):
    monkeypatch.setenv("HACKME_MEDIA_HLS_PROFILE", "mobile_saver")
    status = {
        "status": "ready",
        "variants": [
            {"name": "original", "height": 1080, "bitrate": 4_000_000},
            {"name": "q480", "height": 480, "bitrate": 1_400_000},
            {"name": "q720", "height": 720, "bitrate": 2_800_000},
        ],
        "audio_tracks": [{"name": "audio_01_jpn", "bitrate": 160_000}],
    }

    policy = media_streaming._premium_hls_profile_policy(status)

    assert policy["current_profile"] == "mobile_saver"
    assert policy["asset_profile"] == "full"
    assert policy["asset_profile_candidates"] == ["full"]
    assert policy["profile_matches_asset"] is False
    assert policy["profile_drift"] is True
    assert policy["rebuild_recommended"] is True
    assert policy["rebuild_reason"] == "current_profile_policy_differs_from_prepared_asset"


def test_premium_hls_profile_policy_matches_storage_saver_asset(monkeypatch):
    monkeypatch.setenv("HACKME_MEDIA_HLS_PROFILE", "storage_saver")
    status = {
        "status": "ready",
        "variants": [
            {"name": "q480", "height": 480, "bitrate": 1_400_000},
            {"name": "q720", "height": 720, "bitrate": 2_800_000},
        ],
        "audio_tracks": [{"name": "audio_01_jpn", "bitrate": 160_000}],
    }

    policy = media_streaming._premium_hls_profile_policy(status)

    assert policy["current_profile"] == "storage_saver"
    assert policy["asset_profile"] == "storage_saver"
    assert policy["asset_quality_heights"] == [480, 720]
    assert policy["asset_audio_bitrate"] == 160_000
    assert policy["profile_matches_asset"] is True
    assert policy["profile_drift"] is False
    assert policy["rebuild_recommended"] is False


def test_premium_hls_profile_policy_does_not_false_drift_ambiguous_q480_asset(monkeypatch):
    monkeypatch.setenv("HACKME_MEDIA_HLS_PROFILE", "mobile_saver")
    status = {
        "status": "ready",
        "variants": [{"name": "q480", "height": 480, "bitrate": 1_400_000}],
        "audio_tracks": [],
    }

    policy = media_streaming._premium_hls_profile_policy(status)

    assert policy["asset_profile"] == "storage_saver"
    assert policy["asset_profile_ambiguous"] is True
    assert policy["asset_profile_candidates"] == ["storage_saver", "mobile_saver"]
    assert policy["profile_matches_asset"] is True
    assert policy["profile_drift"] is False
    assert policy["rebuild_recommended"] is False


def test_prepare_stream_asset_extracts_embedded_text_subtitles(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="subbed-video", owner_user_id=1, filename="clip.mkv", mime="video/x-matroska")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='subbed-video'").fetchone()

    def probe_payload(*_args, **_kwargs):
        payload = _fake_probe_payload("video")
        payload["streams"].append({
            "index": 2,
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "tags": {"language": "zh", "title": "繁中"},
            "disposition": {"default": 1},
        })
        return payload

    def fake_run(cmd, **_kwargs):
        output_path = Path(cmd[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n字幕\n", encoding="utf-8")
        return type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(media_streaming, "_run_probe", probe_payload)
    monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", _fake_hls_package)
    monkeypatch.setattr(media_streaming.subprocess, "run", fake_run)

    asset = media_streaming.prepare_stream_asset(
        conn,
        file_row=row,
        storage_root=storage_root,
        server_file_fernet=None,
    )

    assert asset["status"] == "ready"
    assert len(asset["subtitles"]) == 1
    assert asset["subtitles"][0]["label"] == "繁中"
    assert asset["subtitles"][0]["language"] == "zh"
    assert asset["subtitles"][0]["is_default"] is True
    subtitle_path = storage_root / asset["subtitles"][0]["path"]
    assert subtitle_path.exists()
    playback = media_streaming.stream_playback_payload(conn, file_row=row, video_id=9)
    assert playback["subtitles"][0]["url"].startswith("/api/videos/9/hls/subtitles/")


def test_prepare_stream_asset_preserves_multi_audio_and_many_subtitles(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="multi-track-video", owner_user_id=1, filename="movie.mkv", mime="video/x-matroska")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='multi-track-video'").fetchone()

    def probe_payload(*_args, **_kwargs):
        payload = _fake_probe_payload("video")
        payload["streams"] = [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "bit_rate": "4000000"},
            {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "jpn"}, "disposition": {"default": 1}},
            {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "eng"}, "disposition": {"default": 0}},
        ]
        for idx in range(3, 26):
            payload["streams"].append({
                "index": idx,
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "tags": {"language": "chi" if idx == 8 else f"s{idx}", "title": "Traditional" if idx == 8 else ""},
                "disposition": {"default": 1 if idx == 3 else 0, "forced": 1 if idx == 4 else 0},
            })
        return payload

    hls_calls = []

    def fake_hls(source_path, **kwargs):
        hls_calls.append(kwargs)
        return _fake_hls_package(source_path, **kwargs)

    def fake_run(cmd, **_kwargs):
        output_path = Path(cmd[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n字幕\n", encoding="utf-8")
        return type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()

    # Cloud-drive background workers enable forced stream-copy for the video
    # portion.  It must still keep every source audio track as a selectable
    # external HLS rendition.
    monkeypatch.setenv("HACKME_MEDIA_HLS_FORCE_COPY", "1")
    monkeypatch.setattr(media_streaming, "_run_probe", probe_payload)
    monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", fake_hls)
    monkeypatch.setattr(media_streaming.subprocess, "run", fake_run)

    asset = media_streaming.prepare_stream_asset(
        conn,
        file_row=row,
        storage_root=storage_root,
        server_file_fernet=None,
    )

    assert asset["status"] == "ready"
    assert len(asset["audio_tracks"]) == 2
    assert [track["language"] for track in asset["audio_tracks"]] == ["jpn", "eng"]
    assert len(asset["subtitles"]) == 23
    assert any(item["is_forced"] for item in asset["subtitles"])
    assert any(call.get("video_only") for call in hls_calls if call.get("media_type") == "video")
    assert {call.get("audio_stream_index") for call in hls_calls if call.get("media_type") == "audio"} == {1, 2}

    master = (storage_root / "media_derivatives" / "multi-track-video" / "master.m3u8").read_text(encoding="utf-8")
    assert '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio"' in master
    assert 'LANGUAGE="jpn"' in master
    assert 'LANGUAGE="eng"' in master
    assert 'AUDIO="audio"' in master
    playback = media_streaming.stream_playback_payload(conn, file_row=row, video_id=9)
    assert len(playback["audio_tracks"]) == 2
    assert any(item["is_forced"] for item in playback["subtitles"])
    assert playback["service_policy"]["customer_selectable_modes"] == ["realtime_proxy", "prepared_hls"]
    assert playback["service_policy"]["fee_model"] == "basic_standard_premium"
    assert playback["service_policy"]["multi_track_recommended_mode"] == "prepared_hls"
    premium_policy = playback["service_policy"]["premium_hls_profile_policy"]
    assert premium_policy["current_profile"] == "full"
    assert [profile["id"] for profile in premium_policy["profiles"]] == ["full", "storage_saver", "mobile_saver"]
    assert premium_policy["profiles"][2]["relative_fee"] == "premium_lowest"
    assert playback["streaming_options"][0]["mode"] == "realtime_proxy"
    assert playback["streaming_options"][0]["fee_level"] == "middle"
    assert playback["streaming_options"][0]["available"] is True
    assert playback["streaming_options"][0]["implementation_status"] == "ready"
    assert playback["streaming_options"][0]["billing_basis"] == "per_viewer_cpu"
    assert playback["streaming_options"][1]["mode"] == "prepared_hls"
    assert playback["streaming_options"][1]["fee_level"] == "highest"
    assert playback["streaming_options"][1]["profile_policy"]["profiles"][1]["id"] == "storage_saver"
    assert playback["streaming_options"][1]["media_support"]["multi_audio"] is True
    assert playback["streaming_options"][1]["media_support"]["multi_subtitle"] is True


def test_refresh_stream_subtitles_repairs_ready_asset_without_rebuilding_hls(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="old-hls-video", owner_user_id=1, filename="clip.mkv", mime="video/x-matroska")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='old-hls-video'").fetchone()

    monkeypatch.setattr(media_streaming, "_run_probe", lambda *args, **kwargs: _fake_probe_payload("video"))
    monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", _fake_hls_package)
    asset = media_streaming.prepare_stream_asset(
        conn,
        file_row=row,
        storage_root=storage_root,
        server_file_fernet=None,
    )
    assert asset["status"] == "ready"
    assert asset["subtitles"] == []
    variant_rows_before = conn.execute("SELECT COUNT(*) AS c FROM media_stream_variants").fetchone()["c"]

    def probe_payload(*_args, **_kwargs):
        payload = _fake_probe_payload("video")
        payload["streams"].append({
            "index": 2,
            "codec_type": "subtitle",
            "codec_name": "ass",
            "tags": {"language": "chi", "title": "JPSC"},
            "disposition": {"default": 1},
        })
        return payload

    def fake_run(cmd, **_kwargs):
        output_path = Path(cmd[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n字幕\n", encoding="utf-8")
        return type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(media_streaming, "_run_probe", probe_payload)
    monkeypatch.setattr(media_streaming.subprocess, "run", fake_run)

    repaired = media_streaming.refresh_stream_subtitles(
        conn,
        file_row=row,
        storage_root=storage_root,
        server_file_fernet=None,
    )

    assert len(repaired["subtitles"]) == 1
    assert repaired["subtitles"][0]["label"] == "JPSC"
    assert repaired["subtitles"][0]["language"] == "chi"
    assert repaired["subtitles"][0]["is_default"] is True
    assert (storage_root / repaired["subtitles"][0]["path"]).exists()
    assert conn.execute("SELECT COUNT(*) AS c FROM media_stream_variants").fetchone()["c"] == variant_rows_before
    assert conn.execute(
        "SELECT job_type FROM media_stream_jobs ORDER BY id DESC LIMIT 1"
    ).fetchone()["job_type"] == "refresh_subtitles"


def test_shift_webvtt_text_offsets_cue_times_without_touching_notes():
    source = (
        "WEBVTT\n\n"
        "NOTE 00:00:10.000 should not change\n\n"
        "00:00:01.000 --> 00:00:02.250 align:start\n"
        "字幕\n"
    )

    shifted = media_streaming.shift_webvtt_text(source, 1500)

    assert "NOTE 00:00:10.000 should not change" in shifted
    assert "00:00:02.500 --> 00:00:03.750 align:start" in shifted
    assert media_streaming.shift_webvtt_text(source, -5000).count("00:00:00.000") == 2
    compact = "WEBVTT\n\n00:00.043 --> 00:06.043\n字幕\n"
    assert "00:00:00.543 --> 00:00:06.543" in media_streaming.shift_webvtt_text(compact, 500)


def test_add_stream_subtitle_converts_uploaded_srt_to_webvtt(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="manual-sub-video", owner_user_id=1, filename="clip.mp4", mime="video/mp4")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='manual-sub-video'").fetchone()
    subtitle = tmp_path / "zh.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")

    asset = media_streaming.add_stream_subtitle(
        conn,
        file_row=row,
        storage_root=storage_root,
        subtitle_file_path=subtitle,
        original_filename="zh.srt",
        label="繁中",
        language="zh-Hant",
    )

    assert len(asset["subtitles"]) == 1
    item = asset["subtitles"][0]
    assert item["label"] == "繁中"
    assert item["language"] == "zh-hant"
    text = (storage_root / item["path"]).read_text(encoding="utf-8")
    assert text.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.000" in text
    playback = media_streaming.stream_playback_payload(conn, file_row=row, video_id=12)
    assert playback["subtitles"][0]["url"].endswith(".vtt")


def test_prepare_stream_asset_hides_quality_derivative_larger_than_original(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(
        conn,
        storage_root,
        file_id="oversized-derivative-video",
        owner_user_id=1,
        filename="clip.mp4",
        mime="video/mp4",
        payload=b"x" * 4096,
    )
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='oversized-derivative-video'").fetchone()

    def probe_payload(*_args, **_kwargs):
        payload = _fake_probe_payload("video")
        payload["streams"][0]["width"] = 3840
        payload["streams"][0]["height"] = 2160
        payload["streams"][0]["bit_rate"] = "12000000"
        payload["format"]["bit_rate"] = "12000000"
        return payload

    def fake_hls(source_path, *, derivative_dir, media_type, variant_name=None, **kwargs):
        variant_name, playlist_path, init_path = _fake_hls_package(
            source_path,
            derivative_dir=derivative_dir,
            media_type=media_type,
            variant_name=variant_name,
            **kwargs,
        )
        if variant_name == "q720":
            (Path(derivative_dir) / variant_name / "seg_00002.m4s").write_bytes(b"z" * 8192)
        return variant_name, playlist_path, init_path

    monkeypatch.setenv("HACKME_MEDIA_HLS_QUALITY_HEIGHTS", "720,480")
    monkeypatch.setattr(media_streaming, "_run_probe", probe_payload)
    monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", fake_hls)

    asset = media_streaming.prepare_stream_asset(
        conn,
        file_row=row,
        storage_root=storage_root,
        server_file_fernet=None,
    )

    names = [variant["name"] for variant in asset["variants"]]
    assert names == ["original", "q480"]
    assert "q720" not in names
    assert not (storage_root / "media_derivatives" / "oversized-derivative-video" / "q720").exists()
    assert "720p" in asset["error_message"]
    playback = media_streaming.stream_playback_payload(conn, file_row=row, video_id=9)
    assert playback["default_quality"] == "q480"
    assert playback["fallback_quality"] == "q480"
    assert playback["quality_policy"]["derivatives_quota_exempt"] is True


def test_prepare_stream_asset_decrypts_server_encrypted_media_before_packaging(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    plaintext = b"server-encrypted-video-payload"
    fernet = Fernet(Fernet.generate_key())
    target = _seed_uploaded_file(
        conn,
        storage_root,
        file_id="encrypted-video",
        owner_user_id=1,
        filename="secret.mp4",
        mime="video/mp4",
        privacy_mode="server_encrypted",
        payload=fernet.encrypt(plaintext),
    )
    conn.execute("UPDATE uploaded_files SET size_bytes=? WHERE id='encrypted-video'", (target.stat().st_size,))
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='encrypted-video'").fetchone()

    def fake_probe(source_path, **kwargs):
        assert Path(source_path).read_bytes() == plaintext
        return _fake_probe_payload("video")

    monkeypatch.setattr(media_streaming, "_run_probe", fake_probe)
    monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", _fake_hls_package)

    asset = media_streaming.prepare_stream_asset(
        conn,
        file_row=row,
        storage_root=storage_root,
        server_file_fernet=fernet,
    )

    assert asset["status"] == "ready"
    assert asset["source_mode"] == "server_encrypted"


def test_prepare_stream_asset_reports_chunked_decrypt_progress(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    plaintext = b"chunked-server-encrypted-video-payload" * 4
    fernet = Fernet(Fernet.generate_key())
    target = _seed_uploaded_file(
        conn,
        storage_root,
        file_id="chunked-encrypted-video",
        owner_user_id=1,
        filename="secret.mp4",
        mime="video/mp4",
        privacy_mode="server_encrypted",
        payload=b"placeholder",
    )
    encrypt_server_encrypted_chunked_stream(io.BytesIO(plaintext), target, fernet, size_bytes=len(plaintext), chunk_size=16)
    conn.execute("UPDATE uploaded_files SET size_bytes=? WHERE id='chunked-encrypted-video'", (target.stat().st_size,))
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='chunked-encrypted-video'").fetchone()

    def fake_probe(source_path, **kwargs):
        assert Path(source_path).read_bytes() == plaintext
        return _fake_probe_payload("video")

    progress_events = []
    monkeypatch.setattr(media_streaming, "_run_probe", fake_probe)
    monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", _fake_hls_package)

    asset = media_streaming.prepare_stream_asset(
        conn,
        file_row=row,
        storage_root=storage_root,
        server_file_fernet=fernet,
        progress_callback=lambda percent, stage, detail: progress_events.append((percent, stage, detail)),
    )

    decrypt_events = [event for event in progress_events if event[1] == "decrypting"]
    assert asset["status"] == "ready"
    assert len(decrypt_events) > 1
    assert max(event[0] for event in decrypt_events) > 20
    assert any("正在解密伺服器端加密影音" in event[2] for event in decrypt_events)


def test_prepare_stream_asset_releases_sqlite_writer_before_ffmpeg(tmp_path, monkeypatch):
    db_path = tmp_path / "stream-lock.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path, timeout=1)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="lock-video", owner_user_id=1, filename="clip.mp4", mime="video/mp4")
        conn.execute("CREATE TABLE lock_probe (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        conn.commit()
        row = conn.execute("SELECT * FROM uploaded_files WHERE id='lock-video'").fetchone()

        monkeypatch.setattr(media_streaming, "_run_probe", lambda *args, **kwargs: _fake_probe_payload("video"))

        def fake_hls(source_path, *, derivative_dir, media_type, ffmpeg_bin="ffmpeg", segment_seconds=4, **kwargs):
            other = sqlite3.connect(db_path, timeout=0.1)
            try:
                other.execute("INSERT INTO lock_probe DEFAULT VALUES")
                other.commit()
            finally:
                other.close()
            return _fake_hls_package(
                source_path,
                derivative_dir=derivative_dir,
                media_type=media_type,
                ffmpeg_bin=ffmpeg_bin,
                segment_seconds=segment_seconds,
                **kwargs,
            )

        monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", fake_hls)

        asset = media_streaming.prepare_stream_asset(conn, file_row=row, storage_root=storage_root, server_file_fernet=None)

        assert asset["status"] == "ready"
        assert conn.execute("SELECT COUNT(*) FROM lock_probe").fetchone()[0] == len(asset["variants"])
    finally:
        conn.close()


def test_ffmpeg_hls_limits_video_transcode_threads_and_stdin(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake-video")
    derivative_dir = tmp_path / "derivatives"
    calls = []

    class FakeStdout:
        def __init__(self):
            self.lines = ["progress=end\n", ""]
            self.done = False

        def readline(self):
            line = self.lines.pop(0) if self.lines else ""
            if line == "":
                self.done = True
            return line

    class FakeStderr:
        def read(self):
            return ""

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = 0
            calls.append((cmd, kwargs, self))

        def poll(self):
            return 0 if self.stdout.done else None

        def wait(self):
            return 0

        def kill(self):
            self.stdout.done = True

    def fake_select(readers, _writers, _errors, _timeout):
        return readers, [], []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakePopen(cmd, **kwargs)

    monkeypatch.setenv("HACKME_MEDIA_FFMPEG_THREADS", "1")
    monkeypatch.setenv("HACKME_MEDIA_FFMPEG_TIMEOUT_SECONDS", "120")
    monkeypatch.setattr(media_streaming.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(media_streaming.select, "select", fake_select)

    media_streaming._run_ffmpeg_hls(source, derivative_dir=derivative_dir, media_type="video", ffmpeg_bin="ffmpeg")

    cmd, kwargs = calls[0]
    assert "-nostdin" in cmd
    assert "-hide_banner" in cmd
    assert "-loglevel" in cmd
    assert "-threads" in cmd
    assert cmd[cmd.index("-threads") + 1] == "1"
    assert kwargs["stdin"] == media_streaming.subprocess.DEVNULL
    assert kwargs["text"] is True


def test_ffmpeg_hls_applies_target_bitrate_for_quality_variants(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake-video")
    derivative_dir = tmp_path / "derivatives"
    calls = []

    class FakeStdout:
        def __init__(self):
            self.lines = iter(["progress=end\n"])
            self.done = False

        def readline(self):
            try:
                return next(self.lines)
            except StopIteration:
                self.done = True
                return ""

    class FakeStderr:
        def read(self):
            return ""

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = 0
            calls.append((cmd, kwargs))

        def poll(self):
            return 0 if self.stdout.done else None

        def wait(self):
            return 0

        def kill(self):
            self.stdout.done = True

    def fake_select(readers, _writers, _errors, _timeout):
        return readers, [], []

    monkeypatch.setenv("HACKME_MEDIA_FFMPEG_THREADS", "1")
    monkeypatch.setattr(media_streaming.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(media_streaming.select, "select", fake_select)

    media_streaming._run_ffmpeg_hls(
        source,
        derivative_dir=derivative_dir,
        media_type="video",
        target_height=720,
        target_bitrate=2_800_000,
        source_height=2160,
        ffmpeg_bin="ffmpeg",
    )

    cmd, _kwargs = calls[0]
    assert "-crf" not in cmd
    assert cmd[cmd.index("-b:v") + 1] == "2800000"
    assert cmd[cmd.index("-maxrate") + 1] == "3220000"
    assert cmd[cmd.index("-bufsize") + 1] == "5600000"
    assert cmd[cmd.index("-vf") + 1] == "scale=-2:720"
    assert cmd[cmd.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*4)"
    assert cmd[cmd.index("-sc_threshold") + 1] == "0"


def test_ffmpeg_hls_uses_configured_audio_bitrate(tmp_path, monkeypatch):
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"fake-video")
    derivative_dir = tmp_path / "derivatives"
    calls = []

    class FakeStdout:
        def __init__(self):
            self.lines = iter(["progress=end\n"])
            self.done = False

        def readline(self):
            try:
                return next(self.lines)
            except StopIteration:
                self.done = True
                return ""

    class FakeStderr:
        def read(self):
            return ""

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = 0
            calls.append((cmd, kwargs))

        def poll(self):
            return 0 if self.stdout.done else None

        def wait(self):
            return 0

        def kill(self):
            self.stdout.done = True

    def fake_select(readers, _writers, _errors, _timeout):
        return readers, [], []

    monkeypatch.setenv("HACKME_MEDIA_HLS_AUDIO_BITRATE", "96k")
    monkeypatch.setattr(media_streaming.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(media_streaming.select, "select", fake_select)

    media_streaming._run_ffmpeg_hls(
        source,
        derivative_dir=derivative_dir,
        media_type="audio",
        audio_stream_index=2,
        ffmpeg_bin="ffmpeg",
    )

    cmd, _kwargs = calls[0]
    assert cmd[cmd.index("-map") + 1] == "0:2"
    assert cmd[cmd.index("-b:a") + 1] == "96k"
    assert cmd[cmd.index("-ac") + 1] == "2"


def test_realtime_proxy_command_selects_audio_and_strips_non_media_tracks(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"media")

    def probe_payload(*_args, **_kwargs):
        return {
            "format": {"duration": "300", "bit_rate": "6200000"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
                {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "jpn"}, "disposition": {"default": 1}},
                {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "eng"}, "disposition": {"default": 0}},
                {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
                {"index": 4, "codec_type": "data", "codec_name": "bin_data"},
            ],
        }

    monkeypatch.setattr(media_streaming, "_run_probe", probe_payload)

    info = media_streaming.build_realtime_proxy_command(
        source,
        audio_track="audio_02_eng",
        start_seconds=12.5,
        ffmpeg_bin="ffmpeg-test",
        ffprobe_bin="ffprobe-test",
    )

    cmd = info["command"]
    assert cmd[0] == "ffmpeg-test"
    assert cmd[cmd.index("-ss") + 1] == "12.5"
    assert cmd[cmd.index("-map") + 1] == "0:v:0?"
    assert "0:2" in cmd
    assert cmd[cmd.index("-map_metadata") + 1] == "-1"
    assert cmd[cmd.index("-map_chapters") + 1] == "-1"
    assert "-sn" in cmd
    assert "-dn" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-profile:v") + 1] == "baseline"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[cmd.index("-b:a") + 1] == "160k"
    assert cmd[cmd.index("-ac") + 1] == "2"
    assert cmd[-2:] == ["mp4", "pipe:1"]
    assert info["audio_track"]["language"] == "eng"
    assert [track["name"] for track in info["audio_tracks"]] == ["audio_01_jpn", "audio_02_eng"]


def test_realtime_proxy_stream_real_ffmpeg_outputs_single_aac_audio_track(tmp_path, monkeypatch):
    ffmpeg_bin, ffprobe_bin = _require_ffmpeg_tools()
    source = _make_multiaudio_mkv_fixture(tmp_path, ffmpeg_bin=ffmpeg_bin)
    source_probe = media_streaming.realtime_proxy_audio_tracks_for_source(source, ffprobe_bin=ffprobe_bin)
    assert [track["name"] for track in source_probe] == ["audio_01_jpn", "audio_02_eng"]

    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE", "process")
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT", "2")
    monkeypatch.setattr(media_streaming, "_REALTIME_PROXY_ACTIVE", 0)
    stream_info = media_streaming.open_realtime_proxy_stream(
        source,
        audio_track="audio_02_eng",
        start_seconds=0,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        chunk_size=32 * 1024,
    )
    output = tmp_path / "standard-proxy.mp4"
    output.write_bytes(b"".join(stream_info["chunks"]))

    assert output.stat().st_size > 1024
    assert media_streaming.realtime_proxy_runtime_status()["active"] == 0
    assert stream_info["audio_track"]["name"] == "audio_02_eng"
    metrics = stream_info["metrics"]
    assert metrics["finished"] is True
    assert metrics["closed_by_client"] is False
    assert metrics["timed_out"] is False
    assert metrics["bytes_sent"] == output.stat().st_size
    assert metrics["chunks_sent"] >= 1
    assert metrics["first_chunk_latency_ms"] is not None
    assert metrics["duration_ms"] is not None
    assert "rss_peak_bytes" in metrics
    assert "cpu_time_seconds" in metrics
    probe = _ffprobe_json(output, ffprobe_bin=ffprobe_bin)
    streams = probe.get("streams") or []
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    data_streams = [stream for stream in streams if stream.get("codec_type") == "data"]

    assert str((probe.get("format") or {}).get("format_name") or "").startswith("mov,mp4")
    assert len(video_streams) == 1
    assert video_streams[0]["codec_name"] == "h264"
    assert len(audio_streams) == 1
    assert audio_streams[0]["codec_name"] == "aac"
    assert int(audio_streams[0].get("channels") or 0) == 2
    assert subtitle_streams == []
    assert data_streams == []


def test_realtime_proxy_real_ffmpeg_busy_disconnect_metrics_and_reopen(tmp_path, monkeypatch):
    ffmpeg_bin, ffprobe_bin = _require_ffmpeg_tools()
    source = _make_multiaudio_mkv_fixture(tmp_path, ffmpeg_bin=ffmpeg_bin)
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE", "process")
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT", "1")
    monkeypatch.setattr(media_streaming, "_REALTIME_PROXY_ACTIVE", 0)

    first = media_streaming.open_realtime_proxy_stream(
        source,
        audio_track="audio_01_jpn",
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        chunk_size=1024,
    )
    assert media_streaming.realtime_proxy_runtime_status()["active"] == 1
    with pytest.raises(RuntimeError, match="realtime_proxy_busy:1/1"):
        media_streaming.open_realtime_proxy_stream(
            source,
            audio_track="audio_02_eng",
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin,
            chunk_size=1024,
        )

    chunks = first["chunks"]
    first_chunk = next(chunks)
    assert len(first_chunk) > 0
    chunks.close()
    assert media_streaming.realtime_proxy_runtime_status()["active"] == 0
    first_metrics = first["metrics"]
    assert first_metrics["finished"] is True
    assert first_metrics["closed_by_client"] is True
    assert first_metrics["timed_out"] is False
    assert first_metrics["chunks_sent"] == 1
    assert first_metrics["bytes_sent"] == len(first_chunk)
    assert first_metrics["first_chunk_latency_ms"] is not None
    assert first_metrics["duration_ms"] is not None
    assert "rss_peak_bytes" in first_metrics
    assert "cpu_time_seconds" in first_metrics

    second = media_streaming.open_realtime_proxy_stream(
        source,
        audio_track="audio_02_eng",
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        chunk_size=32 * 1024,
    )
    reopened_bytes = b"".join(second["chunks"])
    assert len(reopened_bytes) > 1024
    assert media_streaming.realtime_proxy_runtime_status()["active"] == 0
    assert second["audio_track"]["name"] == "audio_02_eng"
    assert second["metrics"]["closed_by_client"] is False


def test_stream_playback_payload_exposes_realtime_proxy_when_enabled(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="plain-video", owner_user_id=1, filename="movie.mkv", mime="video/x-matroska")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='plain-video'").fetchone()
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_ENABLED", "1")
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT", "3")

    playback = media_streaming.stream_playback_payload(conn, file_row=row, video_id=77)

    assert playback["realtime_proxy_url"] == "/api/videos/77/realtime-proxy"
    assert playback["realtime_proxy"]["available"] is True
    assert playback["realtime_proxy"]["url"] == "/api/videos/77/realtime-proxy"
    assert playback["service_policy"]["realtime_proxy_enabled"] is True
    assert playback["service_policy"]["realtime_proxy_max_concurrent"] == 3
    standard = next(item for item in playback["streaming_options"] if item["mode"] == "realtime_proxy")
    assert standard["available"] is True
    assert standard["implementation_status"] == "ready"
    assert standard["media_support"]["share_ready"] is True


def test_stream_playback_payload_discovers_realtime_proxy_audio_before_hls_ready(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="plain-mkv", owner_user_id=1, filename="movie.mkv", mime="video/x-matroska")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='plain-mkv'").fetchone()
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_ENABLED", "1")

    def proxy_probe_payload(*_args, **_kwargs):
        return {
            "format": {"duration": "120", "bit_rate": "6000000"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
                {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "jpn"}, "disposition": {"default": 1}},
                {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "eng"}, "disposition": {"default": 0}},
            ],
        }

    monkeypatch.setattr(media_streaming, "_run_probe", proxy_probe_payload)

    playback = media_streaming.stream_playback_payload(conn, file_row=row, video_id=77, storage_root=storage_root)

    assert playback["mode"] == "realtime_proxy"
    assert playback["realtime_proxy"]["available"] is True
    assert [track["name"] for track in playback["audio_tracks"]] == ["audio_01_jpn", "audio_02_eng"]
    assert all(track["source"] == "realtime_proxy_probe" for track in playback["audio_tracks"])
    assert all(not track["playlist_url"] for track in playback["audio_tracks"])


def test_ffmpeg_hls_falls_back_to_transcode_when_copy_fails(tmp_path, monkeypatch):
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"fake-video")
    derivative_dir = tmp_path / "derivatives"
    calls = []

    class FakeStdout:
        def __init__(self):
            self.lines = iter(["progress=end\n"])
            self.done = False

        def readline(self):
            try:
                return next(self.lines)
            except StopIteration:
                self.done = True
                return ""

    class FakeStderr:
        def __init__(self, text=""):
            self.text = text

        def read(self):
            return self.text

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.stdout = FakeStdout()
            self.stderr = FakeStderr("copy failed" if len(calls) == 0 else "")
            self.returncode = 1 if len(calls) == 0 else 0
            calls.append((cmd, kwargs, self.returncode))

        def poll(self):
            return self.returncode if self.stdout.done else None

        def wait(self):
            return self.returncode

        def kill(self):
            self.stdout.done = True

    def fake_select(readers, _writers, _errors, _timeout):
        return readers, [], []

    monkeypatch.setattr(media_streaming.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(media_streaming.select, "select", fake_select)

    variant_name, playlist_path, init_path = media_streaming._run_ffmpeg_hls(
        source,
        derivative_dir=derivative_dir,
        media_type="video",
        variant_name="original",
        ffmpeg_bin="ffmpeg",
        copy_codecs=True,
    )

    assert variant_name == "original"
    assert playlist_path.name == "playlist.m3u8"
    assert init_path.name == "init.mp4"
    assert len(calls) == 2
    assert calls[0][0][calls[0][0].index("-c") + 1] == "copy"
    assert "libx264" in calls[1][0]
    assert "-sn" in calls[0][0]
    assert "-dn" in calls[0][0]


def test_get_stream_status_marks_e2ee_media_unavailable(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="e2ee-video", owner_user_id=1, filename="secret.mp4", mime="video/mp4", privacy_mode="e2ee")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='e2ee-video'").fetchone()

    status = media_streaming.get_stream_status(conn, file_row=row)

    assert status["status"] == "unavailable"
    assert "E2EE" in status["error_message"]


def test_non_media_file_never_gets_synthetic_stream_state(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(
        conn,
        storage_root,
        file_id="plain-text",
        owner_user_id=1,
        filename="notes.txt",
        mime="text/plain",
        privacy_mode="server_encrypted",
    )
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='plain-text'").fetchone()

    assert media_streaming.get_stream_status(conn, file_row=row) is None
    assert media_streaming.should_auto_prepare_stream(row, visibility="public") == {
        "enabled": False,
        "reason": "not_media",
        "media_type": "",
    }
    with pytest.raises(ValueError, match="not an audio or video"):
        media_streaming.mark_stream_asset_processing(conn, file_row=row)
    with pytest.raises(ValueError, match="not an audio or video"):
        media_streaming.prepare_stream_asset(conn, file_row=row, storage_root=storage_root)


def test_video_playback_and_hls_routes_use_ready_stream_asset(tmp_path, monkeypatch):
    db_path = tmp_path / "video-stream.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user={"id": 2, "username": "viewer", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="video-1", owner_user_id=1, filename="movie.mp4", mime="video/mp4")
        video = publish_video(conn, actor={"id": 1, "username": "alice", "role": "user"}, cloud_file_id="video-1", title="Movie")
        row = conn.execute("SELECT * FROM uploaded_files WHERE id='video-1'").fetchone()
        monkeypatch.setattr(media_streaming, "_run_probe", lambda *args, **kwargs: _fake_probe_payload("video"))
        monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", _fake_hls_package)
        media_streaming.prepare_stream_asset(conn, file_row=row, storage_root=storage_root, server_file_fernet=fernet)
        conn.commit()
        video_id = video["id"]
    finally:
        conn.close()

    playback = client.get(f"/api/videos/{video_id}/playback")
    payload = playback.get_json()
    assert playback.status_code == 200
    assert payload["ok"] is True
    assert payload["mode"] == "hls"
    assert payload["streaming_ready"] is True
    assert payload["can_prepare_stream"] is False
    assert payload["player_strategy"] == "native_hls_or_hlsjs"
    assert payload["stream_warning"] == ""
    assert payload["hls_js_url"] == "/js/hls.light.min.js?v=20260505-hlsjs"
    assert payload["master_url"].endswith(f"/api/videos/{video_id}/hls/master.m3u8")
    assert payload["fallback_url"] == ""
    assert payload["variants"]
    assert all(int(item["size_bytes"]) > 0 for item in payload["variants"])
    assert all(int(item["segments_total_bytes"]) == int(item["size_bytes"]) for item in payload["variants"])
    assert all(int(item["source_size_bytes"]) > 0 for item in payload["variants"])

    master_path = storage_root / "media_derivatives" / "video-1" / "master.m3u8"
    master_path.write_text(
        "#EXTM3U\n"
        "#EXT-X-VERSION:7\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1024000,RESOLUTION=1280x720,CODECS=\"h264\"\n"
        "source/playlist.m3u8\n",
        encoding="utf-8",
    )

    master = client.get(f"/api/videos/{video_id}/hls/master.m3u8")
    assert master.status_code == 200
    assert master.mimetype == "application/vnd.apple.mpegurl"
    assert 'CODECS="avc1.64001f,mp4a.40.2"' in master.get_data(as_text=True)
    assert 'CODECS="h264"' not in master.get_data(as_text=True)

    playlist = client.get(f"/api/videos/{video_id}/hls/original/playlist.m3u8")
    assert playlist.status_code == 200
    assert playlist.mimetype == "application/vnd.apple.mpegurl"

    init_seg = client.get(f"/api/videos/{video_id}/hls/original/init.mp4")
    assert init_seg.status_code == 200
    assert init_seg.mimetype == "video/mp4"

    segment = client.get(f"/api/videos/{video_id}/hls/original/seg_00001.m4s")
    assert segment.status_code == 200
    assert segment.mimetype == "video/mp4"


def test_video_realtime_proxy_route_streams_when_feature_enabled(tmp_path, monkeypatch):
    db_path = tmp_path / "video-realtime-proxy.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user={"id": 2, "username": "viewer", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="proxy-video-1", owner_user_id=1, filename="movie.mkv", mime="video/x-matroska")
        video = publish_video(conn, actor={"id": 1, "username": "alice", "role": "user"}, cloud_file_id="proxy-video-1", title="Proxy Movie")
        conn.commit()
        video_id = video["id"]
    finally:
        conn.close()

    captured = {}

    def fake_open_realtime_proxy_stream(path, **kwargs):
        captured["path"] = Path(path)
        captured["kwargs"] = kwargs
        return {
            "chunks": iter([b"ftyp", b"moof"]),
            "mimetype": "video/mp4",
            "audio_track": {"name": "audio_02_eng"},
        }

    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_ENABLED", "1")
    monkeypatch.setattr(video_routes, "open_realtime_proxy_stream", fake_open_realtime_proxy_stream)

    response = client.get(f"/api/videos/{video_id}/realtime-proxy?audio=audio_02_eng&start=12.5")

    assert response.status_code == 200
    assert response.mimetype == "video/mp4"
    assert response.get_data() == b"ftypmoof"
    assert response.headers["X-Hackme-Streaming-Mode"] == "realtime_proxy"
    assert response.headers["X-Hackme-Transfer-Mode"] == "python_realtime_proxy"
    assert response.headers["X-Hackme-Audio-Track"] == "audio_02_eng"
    assert captured["path"].name == "movie.mkv"
    assert captured["kwargs"]["audio_track"] == "audio_02_eng"
    assert captured["kwargs"]["start_seconds"] == "12.5"


def test_video_realtime_proxy_route_writes_server_metrics_jsonl(tmp_path, monkeypatch):
    db_path = tmp_path / "video-realtime-proxy-metrics.db"
    storage_root = tmp_path / "storage"
    reports_dir = tmp_path / "reports"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user={"id": 2, "username": "viewer", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
        extra_deps={"REPORTS_DIR": str(reports_dir)},
    ).test_client()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="proxy-metrics-1", owner_user_id=1, filename="movie.mkv", mime="video/x-matroska", payload=b"media-bytes")
        video = publish_video(conn, actor={"id": 1, "username": "alice", "role": "user"}, cloud_file_id="proxy-metrics-1", title="Proxy Metrics")
        conn.commit()
        video_id = video["id"]
    finally:
        conn.close()

    metrics = {
        "pid": 123,
        "runtime_active_at_start": 1,
        "runtime_limit": 2,
        "bytes_sent": 0,
        "chunks_sent": 0,
        "first_chunk_latency_ms": None,
        "duration_ms": None,
        "rss_peak_bytes": 0,
        "cpu_time_seconds": 0.0,
        "closed_by_client": False,
        "finished": False,
    }

    def metric_chunks():
        try:
            metrics["chunks_sent"] += 1
            metrics["bytes_sent"] += 4
            metrics["first_chunk_latency_ms"] = 12.5
            yield b"ftyp"
        finally:
            metrics.update({
                "duration_ms": 18.25,
                "rss_peak_bytes": 51_200_000,
                "cpu_time_seconds": 0.04,
                "closed_by_client": True,
                "finished": True,
                "returncode": 255,
            })

    def fake_open_realtime_proxy_stream(_path, **_kwargs):
        return {
            "chunks": metric_chunks(),
            "mimetype": "video/mp4",
            "audio_track": {"name": "audio_01_jpn", "language": "jpn", "stream_index": 1},
            "runtime": {"active": 1, "limit": 2},
            "metrics": metrics,
        }

    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_ENABLED", "1")
    monkeypatch.setattr(video_routes, "open_realtime_proxy_stream", fake_open_realtime_proxy_stream)

    response = client.get(f"/api/videos/{video_id}/realtime-proxy?audio=audio_01_jpn")

    assert response.status_code == 200
    assert response.get_data() == b"ftyp"
    assert response.headers["X-Hackme-Realtime-Proxy-Request-Id"]
    metric_path = reports_dir / "qa" / "realtime_proxy_stream_metrics.jsonl"
    rows = [json.loads(line) for line in metric_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "realtime_proxy_stream_final"
    assert row["path"] == f"/api/videos/{video_id}/realtime-proxy"
    assert row["route_kind"] == "video"
    assert row["audio_selector"] == "audio_01_jpn"
    assert row["selected_audio"]["name"] == "audio_01_jpn"
    assert row["runtime"]["active_at_start"] == 1
    assert row["runtime"]["local_active_at_start"] == 0
    assert row["runtime"]["limit"] == 2
    assert row["metrics"]["finished"] is True
    assert row["metrics"]["bytes_sent"] == 4
    assert row["metrics"]["rss_peak_bytes"] == 51_200_000
    assert row["metrics"]["closed_by_client"] is True


def test_realtime_proxy_global_slot_rejects_when_host_slot_locked(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"demo")
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    lock_handle = (lock_dir / "realtime_proxy_slot_00.lock").open("a+", encoding="utf-8")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR", str(lock_dir))
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE", "global")
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT", "1")
    monkeypatch.setattr(media_streaming, "_REALTIME_PROXY_ACTIVE", 0)
    monkeypatch.setattr(media_streaming, "_REALTIME_PROXY_HELD_SLOTS", set())
    build_calls = []

    def fake_build(*_args, **_kwargs):
        build_calls.append(True)
        raise AssertionError("global slot rejection should happen before ffprobe/ffmpeg setup")

    monkeypatch.setattr(media_streaming, "build_realtime_proxy_command", fake_build)
    try:
        with pytest.raises(RuntimeError, match="realtime_proxy_busy:1/1"):
            media_streaming.open_realtime_proxy_stream(source)
        status = media_streaming.realtime_proxy_runtime_status()
        assert status["scope"] == "global"
        assert status["active"] == 1
        assert status["global_active"] == 1
        assert status["limit"] == 1
        assert build_calls == []
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def test_realtime_proxy_stream_releases_slot_on_client_close_and_enforces_limit(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"demo")
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE", "process")
    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT", "1")
    monkeypatch.setattr(media_streaming, "_REALTIME_PROXY_ACTIVE", 0)
    build_calls = []

    def fake_build(*args, **kwargs):
        build_calls.append((args, kwargs))
        return {
            "command": ["ffmpeg", "-i", str(source), "pipe:1"],
            "mimetype": "video/mp4",
            "audio_track": {"name": "audio_01_jpn"},
        }

    monkeypatch.setattr(media_streaming, "build_realtime_proxy_command", fake_build)

    class FakeStdout:
        def __init__(self):
            self.closed = False
            self.reads = 0

        def read(self, _size):
            self.reads += 1
            return b"ftyp" if self.reads == 1 else b""

        def close(self):
            self.closed = True

    class FakeProc:
        def __init__(self, *_args, **_kwargs):
            self.stdout = FakeStdout()
            self.terminated = False
            self.killed = False

        def poll(self):
            return None if not self.terminated else 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.terminated = True

    proc_box = {}

    def fake_popen(*args, **kwargs):
        proc = FakeProc(*args, **kwargs)
        proc_box["proc"] = proc
        return proc

    monkeypatch.setattr(media_streaming.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(media_streaming.select, "select", lambda readable, *_args, **_kwargs: (readable, [], []))

    stream_info = media_streaming.open_realtime_proxy_stream(source)
    assert len(build_calls) == 1
    assert media_streaming.realtime_proxy_runtime_status()["active"] == 1
    with pytest.raises(RuntimeError, match="realtime_proxy_busy:1/1"):
        media_streaming.open_realtime_proxy_stream(source)
    assert len(build_calls) == 1

    chunks = stream_info["chunks"]
    assert next(chunks) == b"ftyp"
    chunks.close()

    assert media_streaming.realtime_proxy_runtime_status()["active"] == 0
    assert proc_box["proc"].stdout.closed is True
    assert proc_box["proc"].terminated is True


def test_video_realtime_proxy_route_returns_429_when_limit_is_busy(tmp_path, monkeypatch):
    db_path = tmp_path / "video-realtime-proxy-busy.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user={"id": 2, "username": "viewer", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="proxy-busy-1", owner_user_id=1, filename="movie.mkv", mime="video/x-matroska")
        video = publish_video(conn, actor={"id": 1, "username": "alice", "role": "user"}, cloud_file_id="proxy-busy-1", title="Proxy Busy")
        conn.commit()
        video_id = video["id"]
    finally:
        conn.close()

    def fake_busy(*_args, **_kwargs):
        raise RuntimeError("realtime_proxy_busy:1/1")

    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_ENABLED", "1")
    monkeypatch.setattr(video_routes, "open_realtime_proxy_stream", fake_busy)

    response = client.get(f"/api/videos/{video_id}/realtime-proxy")

    assert response.status_code == 429
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "realtime_proxy_busy"
    assert payload["realtime_proxy"]["enabled"] is True


def test_video_comment_and_danmaku_reject_sensitive_content_before_insert(tmp_path):
    db_path = tmp_path / "video-sensitive.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    actor = {"id": 2, "username": "viewer", "role": "user", "member_level": "trusted", "effective_level": "trusted"}
    violations = []

    def detect_chat_violation(text):
        return ("badword" in str(text or "").lower(), "測試敏感詞")

    def add_violation(*args, **kwargs):
        violations.append({"args": args, "kwargs": kwargs})
        return "counted", "違規計點 +1", len(violations)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="sensitive-video", owner_user_id=1, filename="movie.mp4", mime="video/mp4")
        video = publish_video(conn, actor={"id": 1, "username": "alice", "role": "user"}, cloud_file_id="sensitive-video", title="Movie")
        video_id = video["id"]
        conn.commit()
    finally:
        conn.close()

    client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user=actor,
        extra_deps={
            "detect_chat_violation": detect_chat_violation,
            "add_violation": add_violation,
        },
    ).test_client()

    comment_response = client.post(f"/api/videos/{video_id}/comments", json={"content": "badword comment"})
    assert comment_response.status_code == 403
    comment_payload = comment_response.get_json()
    assert comment_payload["ok"] is False
    assert comment_payload["error"] == "sensitive_content"
    assert comment_payload["reason"] == "測試敏感詞"

    danmaku_response = client.post(
        f"/api/videos/{video_id}/danmaku",
        json={"content": "badword danmaku", "time_ms": 100, "effect": "rainbow"},
    )
    assert danmaku_response.status_code == 403
    danmaku_payload = danmaku_response.get_json()
    assert danmaku_payload["ok"] is False
    assert danmaku_payload["error"] == "sensitive_content"
    assert danmaku_payload["reason"] == "測試敏感詞"

    assert [item["kwargs"]["triggered_by"] for item in violations] == ["video_comment", "video_danmaku"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM video_comments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM video_danmaku").fetchone()[0] == 0
    finally:
        conn.close()


def test_shared_standard_video_playback_uses_shared_hls_and_stream_urls(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-video-stream.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="shared-video-1", owner_user_id=1, filename="movie.mp4", mime="video/mp4")
        video = publish_video(
            conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="shared-video-1",
            title="Shared Movie",
            visibility="unlisted",
            streaming_modes=["prepared_hls"],
            share_password="SharePass123",
            share_max_views=1,
        )
        row = conn.execute("SELECT * FROM uploaded_files WHERE id='shared-video-1'").fetchone()
        monkeypatch.setattr(media_streaming, "_run_probe", lambda *args, **kwargs: _fake_probe_payload("video"))
        monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", _fake_hls_package)
        media_streaming.prepare_stream_asset(conn, file_row=row, storage_root=storage_root, server_file_fernet=fernet)
        subtitle = tmp_path / "shared-zh.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n共享字幕\n", encoding="utf-8")
        media_streaming.add_stream_subtitle(
            conn,
            file_row=row,
            storage_root=storage_root,
            subtitle_file_path=subtitle,
            original_filename="shared-zh.srt",
            label="繁中",
            language="zh-Hant",
        )
        conn.commit()
        token = video["share_url"].rsplit("/", 1)[-1]
    finally:
        conn.close()

    monkeypatch.setenv("HACKME_MEDIA_REALTIME_PROXY_ENABLED", "1")
    viewer = _build_app(db_path, storage_root, fernet, current_user=None).test_client()
    unlocked = viewer.post(f"/api/videos/shared/{token}/unlock", json={"password": "SharePass123"})
    assert unlocked.status_code == 200
    share_session = _share_session_query(unlocked)

    playback = viewer.get(f"/api/videos/shared/{token}/playback{share_session}")
    assert playback.status_code == 200
    payload = playback.get_json()
    assert payload["mode"] == "hls"
    assert payload["published_streaming_modes"] == ["prepared_hls"]
    assert payload["service_policy"]["customer_selectable_modes"] == ["prepared_hls", "realtime_proxy"]
    assert payload["service_policy"]["fallback_modes"] == ["realtime_proxy"]
    assert payload["master_url"].startswith(f"/api/videos/shared/{token}/hls/master.m3u8")
    assert payload["stream_url"] == ""
    assert payload["fallback_url"] == ""
    assert payload["subtitles"][0]["url"].startswith(f"/api/videos/shared/{token}/hls/subtitles/")
    assert "share_session=" in payload["subtitles"][0]["url"]
    assert payload["realtime_proxy_url"].startswith(f"/api/videos/shared/{token}/realtime-proxy")
    assert "share_session=" in payload["realtime_proxy_url"]
    assert payload["realtime_proxy"]["url"].startswith(f"/api/videos/shared/{token}/realtime-proxy")
    assert "share_session=" in payload["realtime_proxy"]["url"]
    assert f"/api/videos/{video['id']}/" not in payload["master_url"]
    assert f"/api/videos/{video['id']}/" not in payload["stream_url"]
    assert f"/api/videos/{video['id']}/" not in payload["realtime_proxy_url"]

    proxy_calls = []

    def fake_open_realtime_proxy_stream(path, **kwargs):
        proxy_calls.append({"path": Path(path), "kwargs": kwargs})
        return {
            "chunks": iter([b"ftyp", b"moof"]),
            "mimetype": "video/mp4",
            "audio_track": {"name": str(kwargs.get("audio_track") or "")},
        }

    monkeypatch.setattr(video_routes, "open_realtime_proxy_stream", fake_open_realtime_proxy_stream)

    proxy = viewer.get(f"/api/videos/shared/{token}/realtime-proxy{share_session}&audio=audio_02_eng&start=6.25")
    assert proxy.status_code == 200
    assert proxy.get_data() == b"ftypmoof"
    assert proxy.headers["X-Hackme-Streaming-Mode"] == "realtime_proxy"
    assert proxy.headers["X-Hackme-Transfer-Mode"] == "python_realtime_proxy"
    assert proxy.headers["X-Hackme-Audio-Track"] == "audio_02_eng"
    assert proxy_calls[-1]["path"].name == "movie.mp4"
    assert proxy_calls[-1]["kwargs"]["audio_track"] == "audio_02_eng"
    assert proxy_calls[-1]["kwargs"]["start_seconds"] == "6.25"

    master = viewer.get(payload["master_url"])
    assert master.status_code == 200
    assert master.mimetype == "application/vnd.apple.mpegurl"
    assert f"original/playlist.m3u8{share_session}" in master.get_data(as_text=True)

    playlist = viewer.get(f"/api/videos/shared/{token}/hls/original/playlist.m3u8{share_session}")
    assert playlist.status_code == 200
    playlist_text = playlist.get_data(as_text=True)
    assert f'URI="init.mp4{share_session}"' in playlist_text
    assert f"seg_00001.m4s{share_session}" in playlist_text

    init_segment = viewer.get(f"/api/videos/shared/{token}/hls/original/init.mp4{share_session}")
    assert init_segment.status_code == 200
    assert init_segment.mimetype == "video/mp4"

    stream = viewer.get(f"/api/videos/shared/{token}/stream{share_session}")
    assert stream.status_code == 403
    assert stream.get_json()["error"] == "direct_stream_disabled"

    subtitle_res = viewer.get(payload["subtitles"][0]["url"])
    assert subtitle_res.status_code == 200
    assert subtitle_res.mimetype == "text/vtt"
    assert "WEBVTT" in subtitle_res.get_data(as_text=True)
    shifted_subtitle_res = viewer.get(payload["subtitles"][0]["url"] + "&shift_ms=500")
    assert shifted_subtitle_res.status_code == 200
    assert "00:00:01.500 --> 00:00:02.500" in shifted_subtitle_res.get_data(as_text=True)

    stateless_viewer = _build_app(db_path, storage_root, fernet, current_user=None).test_client()
    stateless_master = stateless_viewer.get(payload["master_url"])
    assert stateless_master.status_code == 200
    stateless_playlist = stateless_viewer.get(f"/api/videos/shared/{token}/hls/original/playlist.m3u8{share_session}")
    assert stateless_playlist.status_code == 200
    stateless_segment = stateless_viewer.get(f"/api/videos/shared/{token}/hls/original/init.mp4{share_session}")
    assert stateless_segment.status_code == 200

    verify_conn = sqlite3.connect(db_path)
    verify_conn.row_factory = sqlite3.Row
    try:
        share_row = verify_conn.execute("SELECT access_count FROM video_share_links WHERE video_id=?", (video["id"],)).fetchone()
        assert share_row["access_count"] == 1
        event_count = verify_conn.execute(
            "SELECT COUNT(*) AS total FROM share_access_events WHERE share_type='video'",
        ).fetchone()["total"]
        assert event_count == 1
    finally:
        verify_conn.close()

    second_viewer = _build_app(db_path, storage_root, fernet, current_user=None).test_client()
    second_unlock = second_viewer.post(f"/api/videos/shared/{token}/unlock", json={"password": "SharePass123"})
    assert second_unlock.status_code == 410
    assert second_unlock.get_json()["reason"] == "view_limit_reached"


def test_media_prepare_stream_route_requires_owner(tmp_path, monkeypatch):
    db_path = tmp_path / "prepare-route.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    viewer_client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user={"id": 2, "username": "viewer", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="private-video", owner_user_id=1, filename="private.mp4", mime="video/mp4")
        conn.commit()
    finally:
        conn.close()

    response = viewer_client.post("/api/media/private-video/prepare-stream")

    assert response.status_code == 403

    manager_client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user={"id": 3, "username": "manager", "role": "manager", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    manager_response = manager_client.post("/api/media/private-video/prepare-stream")
    assert manager_response.status_code == 403


def test_media_prepare_and_status_require_login_without_500(tmp_path):
    db_path = tmp_path / "media-auth-required.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    anonymous_client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user=None,
        extra_deps={"DB_PATH": str(db_path), "LOG_DIR": str(tmp_path / "logs")},
    ).test_client()

    prepare = anonymous_client.post("/api/media/private-video/prepare-stream")
    status = anonymous_client.get("/api/media/private-video/stream-status")

    assert prepare.status_code == 401
    assert prepare.get_json()["error"] == "login_required"
    assert status.status_code == 401
    assert status.get_json()["error"] == "login_required"


def test_video_publish_prepares_requested_hls_without_blocking_publish(tmp_path, monkeypatch):
    db_path = tmp_path / "publish-route.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    launched = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched["cmd"] = list(cmd)
            launched["kwargs"] = dict(kwargs)

    monkeypatch.setattr(video_routes.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        video_routes,
        "prepare_stream_asset",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not prepare HLS inside Flask request")),
        raising=False,
    )
    owner_client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
        extra_deps={"DB_PATH": str(db_path), "LOG_DIR": str(tmp_path / "logs")},
    ).test_client()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="video-1", owner_user_id=1, filename="movie.mp4", mime="video/mp4")
        conn.commit()
    finally:
        conn.close()

    response = owner_client.post(
        "/api/videos/publish",
        json={
            "cloud_file_id": "video-1",
            "title": "Movie",
            "visibility": "public",
            "streaming_modes": ["direct", "prepared_hls"],
        },
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body["video"]["title"] == "Movie"
    assert body["video"]["status"] == "processing"
    assert body["stream_asset"]["status"] == "processing"
    assert body["stream_warning"] == ""
    assert "scripts/media/hls_prepare_worker.py" in " ".join(launched["cmd"])
    assert "--file-id" in launched["cmd"]
    assert "video-1" in launched["cmd"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = get_job_by_source(conn, "media_hls_prepare", "media_stream:video-1")
        assert job is not None
        assert job["owner_user_id"] == 1
        assert job["status"] == "running"
        assert job["progress_percent"] == 10
        assert job["stage"] == "worker_started"
        assert "Movie" in job["title"]
    finally:
        conn.close()

    browse = owner_client.get("/api/videos")
    assert browse.status_code == 200
    assert browse.get_json()["videos"] == []


def test_hls_prepare_worker_updates_job_center_until_ready(tmp_path, monkeypatch):
    db_path = tmp_path / "worker-job-center.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="worker-video", owner_user_id=1, filename="worker.mp4", mime="video/mp4")
        stale_job = create_job(
            conn,
            owner_user_id=1,
            job_type="media.hls.prepare",
            title="Worker Movie",
            source_module="media_hls_prepare",
            source_ref="media_stream:worker-video",
            status="running",
            progress_percent=12,
            stage="waiting_worker_slot",
        )
        update_job(
            conn,
            stale_job["job_uuid"],
            error_message="previous worker failed",
            error_stage="waiting_worker_slot",
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(media_streaming, "_run_probe", lambda *args, **kwargs: _fake_probe_payload("video"))
    monkeypatch.setattr(media_streaming, "_run_ffmpeg_hls", _fake_hls_package)
    monkeypatch.setenv("HACKME_MEDIA_HLS_SERIALIZE_ALL", "1")
    monkeypatch.setenv("HACKME_MEDIA_HLS_LOCK_DIR", str(tmp_path / "hls-locks"))
    monkeypatch.setenv("HACKME_MEDIA_HLS_MAX_CONCURRENT", "1")
    monkeypatch.setattr(hls_prepare_worker, "_utc_now_iso", lambda: "2026-07-20T05:31:59")

    rc = hls_prepare_worker.main([
        "--db-path",
        str(db_path),
        "--storage-root",
        str(storage_root),
        "--file-id",
        "worker-video",
        "--owner-user-id",
        "1",
        "--title",
        "Worker Movie",
        "--ffmpeg-bin",
        "fake-ffmpeg",
        "--ffprobe-bin",
        "fake-ffprobe",
    ])

    assert rc == 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT * FROM job_center_jobs
            WHERE source_module='media_hls_prepare' AND source_ref='media_stream:worker-video'
            """,
        ).fetchone()
        assert job is not None
        assert job["status"] == "succeeded"
        assert job["progress_percent"] == 100
        assert job["stage"] == "ready"
        assert job["finished_at"] == "2026-07-20T05:31:59"
        assert not job["error_message"]
        assert not job["error_stage"]
        assert "Worker Movie" in job["title"]
        metadata = json.loads(job["metadata_json"] or "{}")
        result = json.loads(job["result_json"] or "{}")
        assert metadata["service_tier"] == "premium_hls"
        assert metadata["worker_slot"]["scope"] == "global"
        assert metadata["worker_slot"]["limit"] == 1
        assert result["worker_slot"]["scope"] == "global"
        assert result["worker_slot"]["slot_index"] == 0
        assert result["worker_metrics"]["pid"] > 0
        assert result["worker_metrics"]["wall_ms"] >= 0
        events = conn.execute(
            "SELECT event_type, stage FROM job_center_events WHERE job_uuid=? ORDER BY id",
            (job["job_uuid"],),
        ).fetchall()
        assert ("progress", "transcoding") in [(row["event_type"], row["stage"]) for row in events]
        assert ("progress", "ready") in [(row["event_type"], row["stage"]) for row in events]
    finally:
        conn.close()


def test_hls_prepare_worker_uses_configurable_global_slot_pool(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    args = type("Args", (), {"storage_root": str(storage_root)})()
    monkeypatch.setenv("HACKME_MEDIA_HLS_LOCK_DIR", str(tmp_path / "hls-locks"))
    monkeypatch.setenv("HACKME_MEDIA_HLS_MAX_CONCURRENT", "2")

    first = second = fourth = None
    try:
        first, first_state = hls_prepare_worker._try_acquire_hls_worker_slot(args)
        second, second_state = hls_prepare_worker._try_acquire_hls_worker_slot(args)
        blocked, blocked_state = hls_prepare_worker._try_acquire_hls_worker_slot(args)

        assert first is not None
        assert second is not None
        assert blocked is None
        assert first_state["scope"] == "global"
        assert first_state["limit"] == 2
        assert first_state["slot_index"] == 0
        assert second_state["slot_index"] == 1
        assert blocked_state["active"] == 2
        assert blocked_state["free"] == 0

        hls_prepare_worker._release_hls_worker_slot(first)
        first = None
        fourth, fourth_state = hls_prepare_worker._try_acquire_hls_worker_slot(args)
        assert fourth is not None
        assert fourth_state["slot_index"] == 0
    finally:
        for handle in (first, second, fourth):
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    handle.close()
                except Exception:
                    pass


def test_hls_prepare_worker_serializes_only_large_jobs_by_default(tmp_path, monkeypatch):
    db_path = tmp_path / "worker-slot-policy.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="small-video", owner_user_id=1, filename="small.mp4", mime="video/mp4")
        small = conn.execute("SELECT * FROM uploaded_files WHERE id='small-video'").fetchone()
        assert hls_prepare_worker._hls_worker_slot_required(small) is False

        conn.execute(
            "UPDATE uploaded_files SET size_bytes=? WHERE id='small-video'",
            (hls_prepare_worker.DEFAULT_HLS_SERIALIZE_MIN_BYTES,),
        )
        large = conn.execute("SELECT * FROM uploaded_files WHERE id='small-video'").fetchone()
        assert hls_prepare_worker._hls_worker_slot_required(large) is True

        monkeypatch.setenv("HACKME_MEDIA_HLS_SERIALIZE_MIN_BYTES", "0")
        assert hls_prepare_worker._hls_worker_slot_required(large) is False

        monkeypatch.setenv("HACKME_MEDIA_HLS_SERIALIZE_ALL", "1")
        assert hls_prepare_worker._hls_worker_slot_required(small) is True
    finally:
        conn.close()


def test_video_publish_keeps_success_when_background_worker_launch_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "publish-route-fail.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    fernet = Fernet(Fernet.generate_key())
    monkeypatch.setattr(
        video_routes.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("worker spawn failed")),
    )
    owner_client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
        extra_deps={"DB_PATH": str(db_path), "LOG_DIR": str(tmp_path / "logs")},
    ).test_client()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="video-2", owner_user_id=1, filename="movie.mp4", mime="video/mp4")
        conn.commit()
    finally:
        conn.close()

    response = owner_client.post(
        "/api/videos/publish",
        json={
            "cloud_file_id": "video-2",
            "title": "Movie 2",
            "visibility": "public",
            "streaming_modes": ["direct", "prepared_hls"],
        },
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body["video"]["title"] == "Movie 2"
    assert body["video"]["status"] == "processing"
    assert body["stream_asset"]["status"] == "processing"
    assert "HLS 背景處理程序啟動失敗" in body["stream_warning"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT * FROM job_center_jobs
            WHERE source_module='media_hls_prepare' AND source_ref='media_stream:video-2'
            """,
        ).fetchone()
        assert job is not None
        assert job["status"] == "failed"
        assert job["stage"] == "launch_failed"
        assert "HLS 背景處理程序啟動失敗" in job["error_message"]
    finally:
        conn.close()


def test_video_upload_server_encrypted_auto_prepares_stream_asset(tmp_path, monkeypatch):
    db_path = tmp_path / "upload-route.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    server_key = Fernet.generate_key()
    key_path = tmp_path / "server-file.key"
    key_path.write_text(server_key.decode("utf-8"), encoding="utf-8")
    fernet = Fernet(server_key)
    launched = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched["cmd"] = list(cmd)
            launched["kwargs"] = dict(kwargs)

    monkeypatch.setattr(video_routes.subprocess, "Popen", FakePopen)
    owner_client = _build_app(
        db_path,
        storage_root,
        fernet,
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
        extra_deps={"DB_PATH": str(db_path), "LOG_DIR": str(tmp_path / "logs"), "SERVER_FILE_KEY_PATH": str(key_path)},
    ).test_client()

    response = owner_client.post(
        "/api/videos/upload",
        data={
            "video": (io.BytesIO(b"server-encrypted-video"), "clip.mp4", "video/mp4"),
            "title": "Encrypted upload",
            "visibility": "public",
            "privacy_mode": "server_encrypted",
        },
        content_type="multipart/form-data",
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body["stream_asset"]["status"] == "processing"
    assert body["stream_warning"] == ""
    assert body["video"]["status"] == "processing"
    assert body["file"]["file_id"] in launched["cmd"]
    assert "--server-file-key-path" in launched["cmd"]
    assert str(key_path) in launched["cmd"]


def test_server_encrypted_video_stream_requires_prepared_hls_not_main_process_decrypt(tmp_path):
    db_path = tmp_path / "server-encrypted-stream.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner = {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            conn,
            storage_root,
            file_id="server-encrypted-video",
            owner_user_id=1,
            filename="private.mp4",
            mime="video/mp4",
            privacy_mode="server_encrypted",
            payload=b"encrypted-placeholder",
        )
        video = publish_video(
            conn,
            actor=owner,
            cloud_file_id="server-encrypted-video",
            title="Private HLS only",
            visibility="unlisted",
            streaming_modes=["prepared_hls"],
        )
        conn.commit()
    finally:
        conn.close()

    owner_client = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=owner).test_client()
    direct = owner_client.get(f"/api/videos/{video['id']}/stream")
    assert direct.status_code == 403
    assert direct.get_json()["error"] == "direct_stream_disabled"

    token = video["share_url"].rsplit("/", 1)[-1]
    shared_client = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    shared = shared_client.get(f"/api/videos/shared/{token}/stream")
    assert shared.status_code == 403
    assert shared.get_json()["error"] == "direct_stream_disabled"


def test_e2ee_stream_v2_bundle_upload_has_inline_size_guard(tmp_path, monkeypatch):
    db_path = tmp_path / "e2ee-bundle-limit.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    monkeypatch.setenv("HACKME_E2EE_STREAM_BUNDLE_MAX_BYTES", "4")
    owner = {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            conn,
            storage_root,
            file_id="e2ee-video-limit",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            privacy_mode="e2ee",
            payload=b"ciphertext",
        )
        conn.commit()
    finally:
        conn.close()

    client = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=owner).test_client()
    response = client.post(
        "/api/media/e2ee-video-limit/e2ee-stream-v2",
        data={
            "bundle": (io.BytesIO(b"12345"), "bundle.bin", "application/octet-stream"),
            "manifest_json": "{}",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert response.get_json()["error"] == "e2ee_stream_bundle_too_large"


def test_prepare_stream_auto_policy_distinguishes_plain_server_encrypted_and_e2ee(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="plain-video", owner_user_id=1, filename="plain.mp4", mime="video/mp4", privacy_mode="standard_plain")
    _seed_uploaded_file(conn, storage_root, file_id="encrypted-video", owner_user_id=1, filename="enc.mp4", mime="video/mp4", privacy_mode="server_encrypted")
    _seed_uploaded_file(conn, storage_root, file_id="e2ee-video", owner_user_id=1, filename="secret.mp4", mime="video/mp4", privacy_mode="e2ee")

    plain = conn.execute("SELECT * FROM uploaded_files WHERE id='plain-video'").fetchone()
    encrypted = conn.execute("SELECT * FROM uploaded_files WHERE id='encrypted-video'").fetchone()
    e2ee = conn.execute("SELECT * FROM uploaded_files WHERE id='e2ee-video'").fetchone()

    assert media_streaming.should_auto_prepare_stream(plain, visibility="public")["enabled"] is True
    assert media_streaming.should_auto_prepare_stream(plain, visibility="private")["enabled"] is False
    assert media_streaming.should_auto_prepare_stream(encrypted, visibility="private")["enabled"] is True
    assert media_streaming.should_auto_prepare_stream(e2ee, visibility="public")["enabled"] is False


def test_strict_e2ee_prepare_stream_service_marks_unavailable_without_ffmpeg(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE uploaded_files (
            id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            storage_path TEXT NOT NULL,
            privacy_mode TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            original_filename_plain_for_public TEXT,
            mime_type_plain_for_public TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            deleted_at TEXT
        )
        """
    )
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _seed_uploaded_file(conn, storage_root, file_id="e2ee-video", owner_user_id=1, filename="secret.mp4", mime="video/mp4", privacy_mode="e2ee")
    row = conn.execute("SELECT * FROM uploaded_files WHERE id='e2ee-video'").fetchone()

    monkeypatch.setattr(media_streaming, "_run_probe", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("strict E2EE must not probe plaintext")))
    asset = media_streaming.prepare_stream_asset(conn, file_row=row, storage_root=storage_root)

    assert asset["status"] == "unavailable"
    assert "server-side HLS or server-side transcode" in asset["error_message"]
    assert asset["variants"] == []


def test_strict_e2ee_prepare_stream_route_rejects_server_transcode_without_worker(tmp_path, monkeypatch):
    db_path = tmp_path / "e2ee-prepare-route.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner = {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(conn, storage_root, file_id="e2ee-video", owner_user_id=1, filename="secret.mp4", mime="video/mp4", privacy_mode="e2ee")
        conn.commit()
    finally:
        conn.close()

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("strict E2EE must not launch HLS worker")

    monkeypatch.setattr(video_routes.subprocess, "Popen", fail_popen)
    client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user=owner,
        extra_deps={"DB_PATH": str(db_path), "LOG_DIR": str(tmp_path / "logs")},
    ).test_client()

    response = client.post("/api/media/e2ee-video/prepare-stream")

    assert response.status_code == 409
    body = response.get_json()
    assert body["error"] == "strict_e2ee_server_transcode_disabled"
    assert body["allowed_mode"] == "client_side_transcode_then_encrypt"


def test_legacy_owner_e2ee_video_exposes_video_scoped_ciphertext_and_owner_key(tmp_path):
    db_path = tmp_path / "public-e2ee.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(owner_conn, storage_root, file_id="e2ee-public", owner_user_id=1, filename="public-secret.mp4", mime="video/mp4", privacy_mode="e2ee", payload=b"ciphertext-video")
        _mark_file_as_e2ee(owner_conn, "e2ee-public")
        ensure_video_schema(owner_conn)
        owner_conn.execute(
            """
            INSERT INTO encrypted_file_keys (
                id, file_id, recipient_user_id, encrypted_file_key, wrapped_by, key_version, created_at
            ) VALUES ('owner-key-public', 'e2ee-public', 1, 'owner-passphrase-wrapped-key', 'owner_passphrase', 1, '2026-01-01T00:00:00')
            """
        )
        owner_conn.execute(
            """
            INSERT INTO videos (
                video_uuid, owner_user_id, cloud_file_id, title, description,
                visibility, status, created_at, updated_at
            ) VALUES ('legacy-owner-e2ee', 1, 'e2ee-public', 'Owner E2EE', '', 'public', 'ready', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
            """
        )
        owner_conn.commit()
        video_id = owner_conn.execute("SELECT id FROM videos WHERE cloud_file_id='e2ee-public'").fetchone()["id"]
    finally:
        owner_conn.close()

    client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    playback = client.get(f"/api/videos/{video_id}/playback")
    assert playback.status_code == 200
    playback_json = playback.get_json()
    assert playback_json["mode"] == "e2ee_direct"
    assert playback_json["requires_fragment_key"] is False
    assert playback_json["quality_policy"]["server_transcode_allowed"] is False
    assert playback_json["quality_policy"]["strict_e2ee_derivatives_mode"] == "client_side_transcode_then_encrypt"
    assert playback_json["e2ee_key_url"] == f"/api/videos/{video_id}/e2ee-key"
    assert playback_json["ciphertext_url"] == f"/api/videos/{video_id}/ciphertext"

    e2ee_key = client.get(playback_json["e2ee_key_url"])
    assert e2ee_key.status_code == 200
    key_payload = e2ee_key.get_json()["e2ee"]
    assert key_payload["encrypted_file_key"] == "owner-passphrase-wrapped-key"
    assert key_payload["ciphertext_sha256"] == "a" * 64

    ciphertext = client.get(playback_json["ciphertext_url"])
    assert ciphertext.status_code == 200
    assert ciphertext.data == b"ciphertext-video"


def test_shared_e2ee_video_requires_password_fragment_and_exposes_browser_side_payload(tmp_path):
    db_path = tmp_path / "shared-e2ee.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(owner_conn, storage_root, file_id="e2ee-video", owner_user_id=1, filename="secret.mp4", mime="video/mp4", privacy_mode="e2ee", payload=b"ciphertext-video")
        _mark_file_as_e2ee(owner_conn, "e2ee-video")
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="e2ee-video",
            title="Secret",
            visibility="unlisted",
            share_password="SharePass123",
            share_wrapped_file_key_envelope='{"alg":"AES-GCM","v":1,"nonce":"AAAAAAAAAAAAAAAA","ciphertext":"AQIDBA=="}',
            share_max_views=5,
        )
        owner_conn.commit()
        token = video["share_url"].rsplit("/", 1)[-1]
    finally:
        owner_conn.close()

    client = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()

    locked = client.get(f"/api/videos/shared/{token}")
    assert locked.status_code == 401
    assert locked.get_json()["reason"] == "password_required"

    forbidden = client.get(f"/api/videos/shared/{token}?vk=should-not-arrive")
    assert forbidden.status_code == 400
    assert forbidden.get_json()["reason"] == "forbidden_fragment_transport"

    secret_field = client.post(
        f"/api/videos/shared/{token}/unlock",
        json={"password": "SharePass123", "raw_file_key": "forbidden"},
    )
    assert secret_field.status_code == 400
    assert secret_field.get_json()["error"] == "forbidden_share_secret_field"

    bad_password = client.post(
        f"/api/videos/shared/{token}/unlock",
        json={"password": "wrong-pass"},
    )
    assert bad_password.status_code == 403
    assert bad_password.get_json()["reason"] == "password_invalid"

    unlocked = client.post(
        f"/api/videos/shared/{token}/unlock",
        json={"password": "SharePass123"},
    )
    assert unlocked.status_code == 200
    assert unlocked.get_json()["ok"] is True
    share_session = _share_session_query(unlocked)

    detail = client.get(f"/api/videos/shared/{token}{share_session}")
    assert detail.status_code == 200
    video_payload = detail.get_json()["video"]
    assert video_payload["share_password_required"] is True
    assert video_payload["share_requires_fragment_key"] is True
    assert video_payload["share_max_views"] == 5

    playback = client.get(f"/api/videos/shared/{token}/playback{share_session}")
    assert playback.status_code == 200
    playback_json = playback.get_json()
    assert playback_json["mode"] == "e2ee_direct"
    assert playback_json["requires_fragment_key"] is True
    assert playback_json["player_strategy"] == "browser_e2ee_full_fallback"
    assert playback_json["stream_warning"]
    assert playback_json["hls_js_url"] == ""
    assert playback_json["stream_v2_available"] is False

    e2ee_key = client.get(f"/api/videos/shared/{token}/e2ee-key{share_session}")
    assert e2ee_key.status_code == 200
    key_payload = e2ee_key.get_json()["e2ee_share"]
    assert key_payload["wrapped_file_key_envelope"]
    assert "encrypted_file_key" not in key_payload
    assert key_payload["ciphertext_sha256"] == "a" * 64

    ciphertext = client.get(f"/api/videos/shared/{token}/ciphertext{share_session}")
    assert ciphertext.status_code == 200
    assert ciphertext.data == b"ciphertext-video"


def test_shared_e2ee_video_simulation_can_decrypt_original_plaintext(tmp_path):
    db_path = tmp_path / "shared-e2ee-sim.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        sealed = _seed_real_e2ee_file(
            owner_conn,
            storage_root,
            file_id="e2ee-video",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            plaintext=b"simulated-shared-e2ee-video-payload",
        )
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="e2ee-video",
            title="Secret",
            visibility="unlisted",
            share_password="SharePass123",
            share_wrapped_file_key_envelope=sealed["share_wrapped_file_key_envelope"],
            share_max_views=5,
        )
        owner_conn.commit()
        token = video["share_url"].rsplit("/", 1)[-1]
    finally:
        owner_conn.close()

    viewer = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    unlocked = viewer.post(f"/api/videos/shared/{token}/unlock", json={"password": "SharePass123"})
    assert unlocked.status_code == 200
    share_session = _share_session_query(unlocked)

    playback = viewer.get(f"/api/videos/shared/{token}/playback{share_session}")
    assert playback.status_code == 200
    playback_json = playback.get_json()
    assert playback_json["player_strategy"] == "browser_e2ee_full_fallback"
    assert playback_json["mode"] == "e2ee_direct"

    key_payload = viewer.get(playback_json["e2ee_key_url"]).get_json()["e2ee_share"]
    unwrapped_file_key = _unwrap_shared_file_key_from_fragment(
        key_payload["wrapped_file_key_envelope"],
        sealed["share_fragment_key"],
    )
    assert unwrapped_file_key == sealed["file_key"]

    decrypted_metadata = _decrypt_shared_metadata(unwrapped_file_key, key_payload["encrypted_metadata"])
    assert decrypted_metadata["filename"] == "secret.mp4"
    assert decrypted_metadata["mime_type"] == "video/mp4"

    ciphertext = viewer.get(playback_json["ciphertext_url"])
    assert ciphertext.status_code == 200
    plaintext = AESGCM(unwrapped_file_key).decrypt(
        base64.b64decode(key_payload["nonce"]),
        ciphertext.data,
        None,
    )
    assert plaintext == sealed["plaintext"]


def test_shared_video_password_lock_max_views_and_revoke_routes(tmp_path):
    db_path = tmp_path / "shared-guardrails.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(owner_conn, storage_root, file_id="plain-video", owner_user_id=1, filename="movie.mp4", mime="video/mp4", privacy_mode="standard_plain", payload=b"plain-video")
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="plain-video",
            title="Plain",
            visibility="unlisted",
            share_password="SharePass123",
            share_max_views=1,
        )
        owner_conn.commit()
        video_id = video["id"]
        token = video["share_url"].rsplit("/", 1)[-1]
    finally:
        owner_conn.close()

    anonymous = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    for _ in range(5):
        denied = anonymous.post(f"/api/videos/shared/{token}/unlock", json={"password": "wrong-pass"})
        assert denied.status_code == 403
    locked = anonymous.post(f"/api/videos/shared/{token}/unlock", json={"password": "wrong-pass"})
    assert locked.status_code == 429
    assert locked.get_json()["reason"] == "password_locked"

    owner_client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    regenerated = owner_client.put(f"/api/videos/{video_id}/share-link", json={"regenerate": True})
    assert regenerated.status_code == 200
    new_share_url = regenerated.get_json()["share_link"]["url"]
    new_token = new_share_url.rsplit("/", 1)[-1]
    assert new_token != token
    assert regenerated.get_json()["share_link"]["password_required"] is True

    first_viewer = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    unlock = first_viewer.post(f"/api/videos/shared/{new_token}/unlock", json={"password": "SharePass123"})
    assert unlock.status_code == 200
    share_session = _share_session_query(unlock)
    ok = first_viewer.get(f"/api/videos/shared/{new_token}/playback{share_session}")
    assert ok.status_code == 200
    same_session_detail = first_viewer.get(f"/api/videos/shared/{new_token}{share_session}")
    assert same_session_detail.status_code == 200
    exhausted_page_same_viewer = first_viewer.get(f"/shared/videos/{new_token}")
    assert exhausted_page_same_viewer.status_code == 200
    assert "share-password-form" in exhausted_page_same_viewer.get_data(as_text=True)

    second_viewer = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    second_unlock = second_viewer.post(f"/api/videos/shared/{new_token}/unlock", json={"password": "SharePass123"})
    assert second_unlock.status_code == 410
    exhausted = second_unlock
    assert exhausted.status_code == 410
    assert exhausted.get_json()["reason"] == "view_limit_reached"

    revoked = owner_client.delete(f"/api/videos/{video_id}/share-link")
    assert revoked.status_code == 200
    revoked_view = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client().get(f"/api/videos/shared/{new_token}")
    assert revoked_view.status_code == 404


def test_manager_cannot_update_another_users_unlisted_share_link(tmp_path):
    db_path = tmp_path / "shared-manager.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(owner_conn, storage_root, file_id="plain-video", owner_user_id=1, filename="movie.mp4", mime="video/mp4", privacy_mode="standard_plain", payload=b"plain-video")
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="plain-video",
            title="Plain",
            visibility="unlisted",
        )
        owner_conn.commit()
        video_id = video["id"]
    finally:
        owner_conn.close()

    manager_client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user={"id": 3, "username": "manager", "role": "manager", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    updated = manager_client.put(
        f"/api/videos/{video_id}/share-link",
        json={"share_password": "ManagerShare123", "share_max_views": 7},
    )
    assert updated.status_code == 404

    revoked = manager_client.delete(f"/api/videos/{video_id}/share-link")
    assert revoked.status_code == 404


def test_shared_video_page_mentions_hls_js_fallback_and_fragment_loss(tmp_path):
    db_path = tmp_path / "shared-page.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(owner_conn, storage_root, file_id="plain-video", owner_user_id=1, filename="movie.mp4", mime="video/mp4", privacy_mode="standard_plain", payload=b"plain-video")
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="plain-video",
            title="Plain",
            visibility="unlisted",
        )
        owner_conn.commit()
        token = video["share_url"].rsplit("/", 1)[-1]
    finally:
        owner_conn.close()

    page = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client().get(f"/shared/videos/{token}")
    html = page.get_data(as_text=True)
    # Issue #182 fix: the runtime JS lives in public/js/shared-video.js so
    # CSP `script-src: 'self'` doesn't block it. The HTML now only carries
    # the JSON token island + external script tag; the assertions below
    # scan the standalone JS file.
    from pathlib import Path as _P
    js = (_P(__file__).resolve().parents[3] / "public" / "js" / "shared-video.js").read_text(encoding="utf-8")

    assert page.status_code == 200
    assert '<script src="/js/shared-video.js' in html
    assert '<script id="share-token" type="application/json">' in html
    assert 'id="player-action"' in html
    assert "loadSharedHlsLibrary" in js
    assert "/js/hls.light.min.js?v=20260505-hlsjs" in js
    assert "Chrome / Firefox / Edge" in js
    assert "完整連結" in js
    assert "無法復原" in js
    assert "分享授權無效或已被竄改" in js
    assert "正在讀取 E2EE 分享授權" in js
    assert "正在下載加密影音檔" in js
    assert "正在瀏覽器端解密影音" in js
    assert "不會把密碼或金鑰送到伺服器" in js
    assert "AbortController" in js
    assert "setTimeout(() => controller.abort(), 10000);" in js
    assert "分享影音載入失敗" in js
    assert "isSharePasswordResponse" in js
    assert "showSharePasswordPrompt" in js
    assert "此分享影音需要先解鎖" in js
    assert "showSharedPlaybackAction" in js
    assert "開始 E2EE 播放" in js
    assert "未按下播放前，不會主動要求密碼或開始解密。" in js
    assert ".wrap { width:min(100%, 1120px); min-height:100dvh;" in html
    assert "#player-host { width:100%; min-height:0; margin-top:.8rem; display:grid; place-items:center; }" in html
    assert "#player-host video { inline-size:min(100%, calc((100dvh - 240px) * 16 / 9)); height:auto; max-height:min(64dvh, 560px); aspect-ratio:16 / 9; object-fit:contain; }" in html
    assert "@media (max-width: 640px)" in html
    assert "#player-host video { inline-size:100%; max-height:min(48dvh, calc(100dvh - 210px)); }" in html
    assert "@media (max-height: 520px) and (orientation: landscape)" in html


def test_shared_e2ee_stream_v2_manifest_and_chunk_routes_work_and_stay_off_hls(tmp_path):
    db_path = tmp_path / "shared-e2ee-stream-v2.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            owner_conn,
            storage_root,
            file_id="e2ee-video",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            privacy_mode="e2ee",
            payload=b"ciphertext-video",
        )
        _mark_file_as_e2ee(owner_conn, "e2ee-video")
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="e2ee-video",
            title="Secret",
            visibility="unlisted",
            share_password="SharePass123",
            share_wrapped_file_key_envelope='{"alg":"AES-GCM","v":1,"nonce":"AAAAAAAAAAAAAAAA","ciphertext":"AQIDBA=="}',
            share_max_views=8,
        )
        owner_conn.commit()
        video_id = video["id"]
        token = video["share_url"].rsplit("/", 1)[-1]
    finally:
        owner_conn.close()

    owner_client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()

    manifest = {
        "e2ee_stream_version": 2,
        "chunk_size": 8,
        "chunk_count": 2,
        "content_type": "video/mp4",
        "duration_hint": 3.5,
        "byte_range_hint": {"total_plaintext_bytes": 9},
        "chunks": [
            {
                "chunk_index": 0,
                "nonce": "AAAAAAAAAAAAAAAA",
                "ciphertext_offset": 0,
                "ciphertext_size": 5,
                "plaintext_offset": 0,
                "plaintext_size": 4,
                "ciphertext_sha256": "36bbe50ed96841d10443bcb670d6554f0a34b761be67ec9c4a8ad2c0c44ca42c",
            },
            {
                "chunk_index": 1,
                "nonce": "BBBBBBBBBBBBBBBB",
                "ciphertext_offset": 5,
                "ciphertext_size": 4,
                "plaintext_offset": 4,
                "plaintext_size": 5,
                "ciphertext_sha256": "21e32f5321cad49ab4cf78ba5ed231e0f36d0c78d34108fda1be939f33fba149",
            },
        ],
    }
    prepared = owner_client.post(
        "/api/media/e2ee-video/e2ee-stream-v2",
        data={
            "manifest_json": json.dumps(manifest),
            "bundle": (io.BytesIO(b"abcdeWXYZ"), "bundle.bin"),
        },
        content_type="multipart/form-data",
    )
    assert prepared.status_code == 200
    assert prepared.get_json()["asset"]["available"] is True
    variant_prepared = owner_client.post(
        "/api/media/e2ee-video/e2ee-stream-v2/variants/q480",
        data={
            "manifest_json": json.dumps({**manifest, "content_type": "video/webm"}),
            "bundle": (io.BytesIO(b"abcdeWXYZ"), "q480.bundle"),
            "label": "480p",
            "width": "854",
            "height": "480",
            "bitrate": "800000",
            "derived_from_original_sha256": "d" * 64,
        },
        content_type="multipart/form-data",
    )
    assert variant_prepared.status_code == 200
    assert variant_prepared.get_json()["variant"]["name"] == "q480"
    assert variant_prepared.get_json()["variant"]["height"] == 480
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT * FROM job_center_jobs
            WHERE source_module='media_e2ee_stream_v2' AND source_ref='e2ee_stream_v2:e2ee-video'
            """,
        ).fetchone()
        assert job is not None
        assert job["owner_user_id"] == 1
        assert job["status"] == "succeeded"
        assert job["progress_percent"] == 100
        assert job["stage"] == "ready"
    finally:
        conn.close()

    viewer = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    unlocked = viewer.post(f"/api/videos/shared/{token}/unlock", json={"password": "SharePass123"})
    assert unlocked.status_code == 200
    share_session = _share_session_query(unlocked)

    playback = viewer.get(f"/api/videos/shared/{token}/playback{share_session}")
    assert playback.status_code == 200
    playback_json = playback.get_json()
    assert playback_json["mode"] == "e2ee_stream_v2"
    assert playback_json["player_strategy"] == "browser_e2ee_stream_v2"
    assert playback_json["stream_v2_available"] is True
    assert playback_json["high_performance_streaming"] is False
    assert playback_json["master_url"] == ""
    assert playback_json["hls_js_url"] == ""
    assert "manifest_url" in playback_json
    assert "chunk_url_template" in playback_json
    assert playback_json["quality_policy"]["strict_e2ee_derivatives_mode"] == "client_side_transcode_then_encrypt"
    assert playback_json["e2ee_derivatives_available"] is True
    assert playback_json["default_quality"] == "q480"
    assert [variant["name"] for variant in playback_json["variants"]] == ["original", "q480"]
    assert "/variants/q480/manifest" in playback_json["variants"][1]["manifest_url"]
    assert "share_session=" in playback_json["variants"][1]["manifest_url"]

    shared_manifest = viewer.get(f"/api/videos/shared/{token}/e2ee-stream-v2/manifest{share_session}")
    assert shared_manifest.status_code == 200
    shared_manifest_json = shared_manifest.get_json()
    assert shared_manifest_json["available"] is True
    assert shared_manifest_json["player_strategy"] == "browser_e2ee_stream_v2"
    assert shared_manifest_json["chunk_count"] == 2
    assert shared_manifest_json["chunks"][0]["ciphertext_size"] == 5
    assert shared_manifest_json["capabilities"]["segment_integrity_sha256"] is True
    assert shared_manifest_json["capabilities"]["seek_recovery"] == "sequential_segment_resume"
    assert "ciphertext_offset" not in shared_manifest_json["chunks"][0]

    chunk0 = viewer.get(f"/api/videos/shared/{token}/e2ee-stream-v2/chunks/0{share_session}")
    assert chunk0.status_code == 200
    assert chunk0.data == b"abcde"
    assert chunk0.headers["Cache-Control"] == "private, max-age=0, no-store"

    missing_chunk = viewer.get(f"/api/videos/shared/{token}/e2ee-stream-v2/chunks/9{share_session}")
    assert missing_chunk.status_code == 404
    assert missing_chunk.get_json()["error"] == "chunk_not_found"

    shared_variant_manifest = viewer.get(f"/api/videos/shared/{token}/e2ee-stream-v2/variants/q480/manifest{share_session}")
    assert shared_variant_manifest.status_code == 200
    shared_variant_manifest_json = shared_variant_manifest.get_json()
    assert shared_variant_manifest_json["available"] is True
    assert shared_variant_manifest_json["variant_name"] == "q480"
    assert shared_variant_manifest_json["height"] == 480
    assert shared_variant_manifest_json["content_type"] == "video/webm"
    assert shared_variant_manifest_json["encrypted_size_bytes"] == 9
    assert "ciphertext_offset" not in shared_variant_manifest_json["chunks"][0]

    shared_variant_chunk = viewer.get(f"/api/videos/shared/{token}/e2ee-stream-v2/variants/q480/chunks/1{share_session}")
    assert shared_variant_chunk.status_code == 200
    assert shared_variant_chunk.data == b"WXYZ"

    direct_manifest = owner_client.get(f"/api/videos/{video_id}/e2ee-stream-v2/manifest")
    assert direct_manifest.status_code == 200
    assert direct_manifest.get_json()["available"] is True

    direct_variant_manifest = owner_client.get(f"/api/videos/{video_id}/e2ee-stream-v2/variants/q480/manifest")
    assert direct_variant_manifest.status_code == 200
    assert direct_variant_manifest.get_json()["variant_name"] == "q480"


def test_e2ee_stream_v2_manifest_rejects_bundle_size_mismatch():
    manifest = {
        "e2ee_stream_version": 2,
        "chunk_size": 8,
        "chunk_count": 2,
        "content_type": "video/mp4",
        "duration_hint": 1.0,
        "chunks": [
            {
                "chunk_index": 0,
                "nonce": "AAAAAAAAAAAAAAAA",
                "ciphertext_offset": 0,
                "ciphertext_size": 5,
                "plaintext_offset": 0,
                "plaintext_size": 4,
                "ciphertext_sha256": "11" * 32,
            },
            {
                "chunk_index": 1,
                "nonce": "BBBBBBBBBBBBBBBB",
                "ciphertext_offset": 5,
                "ciphertext_size": 6,
                "plaintext_offset": 4,
                "plaintext_size": 5,
                "ciphertext_sha256": "22" * 32,
            },
        ],
    }

    with pytest.raises(ValueError, match="bundle 大小與 chunk metadata 不一致"):
        _normalize_manifest(manifest, bundle_size=10)


def test_e2ee_stream_v2_variant_rejects_oversized_derivative(tmp_path):
    db_path = tmp_path / "e2ee-stream-v2-variant-oversized.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            conn,
            storage_root,
            file_id="e2ee-video",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            privacy_mode="e2ee",
            payload=b"0123456789",
        )
        _mark_file_as_e2ee(conn, "e2ee-video")
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id='e2ee-video'").fetchone()
        manifest = {
            "e2ee_stream_version": 2,
            "chunk_size": 16,
            "chunk_count": 1,
            "content_type": "video/webm",
            "duration_hint": 1.0,
            "chunks": [
                {
                    "chunk_index": 0,
                    "nonce": "AAAAAAAAAAAAAAAA",
                    "ciphertext_offset": 0,
                    "ciphertext_size": 10,
                    "plaintext_offset": 0,
                    "plaintext_size": 8,
                    "ciphertext_sha256": hashlib.sha256(b"0123456789").hexdigest(),
                },
            ],
        }
        with pytest.raises(ValueError, match="不應大於或等於原檔"):
            upsert_e2ee_stream_v2_variant(
                conn,
                file_row=file_row,
                storage_root=storage_root,
                variant_name="q480",
                manifest_payload=manifest,
                bundle_bytes=b"0123456789",
                height=480,
            )
    finally:
        conn.close()


def test_e2ee_stream_v2_variant_route_respects_root_policy(tmp_path):
    db_path = tmp_path / "e2ee-stream-v2-variant-policy.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            conn,
            storage_root,
            file_id="e2ee-video",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            privacy_mode="e2ee",
            payload=b"0123456789abcdef",
        )
        _mark_file_as_e2ee(conn, "e2ee-video")
        conn.commit()
    finally:
        conn.close()

    owner = {"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"}
    manifest = {
        "e2ee_stream_version": 2,
        "chunk_size": 16,
        "chunk_count": 1,
        "content_type": "video/webm",
        "duration_hint": 1.0,
        "chunks": [
            {
                "chunk_index": 0,
                "nonce": "AAAAAAAAAAAAAAAA",
                "ciphertext_offset": 0,
                "ciphertext_size": 8,
                "plaintext_offset": 0,
                "plaintext_size": 6,
                "ciphertext_sha256": hashlib.sha256(b"abcdefgh").hexdigest(),
            },
        ],
    }

    disabled_client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user=owner,
        extra_deps={"get_system_settings": lambda: {"video_e2ee_derivatives_enabled": False}},
    ).test_client()
    disabled = disabled_client.post(
        "/api/media/e2ee-video/e2ee-stream-v2/variants/q480",
        data={
            "manifest_json": json.dumps(manifest),
            "bundle": (io.BytesIO(b"abcdefgh"), "q480.bundle"),
            "height": "480",
        },
        content_type="multipart/form-data",
    )
    assert disabled.status_code == 403
    assert disabled.get_json()["error"] == "e2ee_derivatives_disabled"

    whitelist_client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user=owner,
        extra_deps={"get_system_settings": lambda: {"video_e2ee_derivative_heights": "720"}},
    ).test_client()
    rejected_height = whitelist_client.post(
        "/api/media/e2ee-video/e2ee-stream-v2/variants/q480",
        data={
            "manifest_json": json.dumps(manifest),
            "bundle": (io.BytesIO(b"abcdefgh"), "q480.bundle"),
            "height": "480",
        },
        content_type="multipart/form-data",
    )
    assert rejected_height.status_code == 400
    assert rejected_height.get_json()["error"] == "e2ee_derivative_height_disabled"
    assert rejected_height.get_json()["allowed_heights"] == [720]

    accepted_height = whitelist_client.post(
        "/api/media/e2ee-video/e2ee-stream-v2/variants/q720",
        data={
            "manifest_json": json.dumps(manifest),
            "bundle": (io.BytesIO(b"abcdefgh"), "q720.bundle"),
            "label": "720p",
            "width": "1280",
            "height": "720",
            "bitrate": "1200000",
            "derived_from_original_sha256": "b" * 64,
        },
        content_type="multipart/form-data",
    )
    assert accepted_height.status_code == 200
    assert accepted_height.get_json()["variant"]["name"] == "q720"


def test_e2ee_stream_v2_cleanup_removes_original_and_derivative_cache(tmp_path):
    db_path = tmp_path / "e2ee-stream-v2-cleanup.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            conn,
            storage_root,
            file_id="e2ee-video",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            privacy_mode="e2ee",
            payload=b"ciphertext-video-payload",
        )
        _mark_file_as_e2ee(conn, "e2ee-video")
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id='e2ee-video'").fetchone()
        manifest = {
            "e2ee_stream_version": 2,
            "chunk_size": 16,
            "chunk_count": 1,
            "content_type": "video/webm",
            "duration_hint": 1.0,
            "chunks": [
                {
                    "chunk_index": 0,
                    "nonce": "AAAAAAAAAAAAAAAA",
                    "ciphertext_offset": 0,
                    "ciphertext_size": 9,
                    "plaintext_offset": 0,
                    "plaintext_size": 7,
                    "ciphertext_sha256": hashlib.sha256(b"abcdefghi").hexdigest(),
                },
            ],
        }
        video_routes.upsert_e2ee_stream_v2_asset(
            conn,
            file_row=file_row,
            storage_root=storage_root,
            manifest_payload=manifest,
            bundle_bytes=b"abcdefghi",
        )
        upsert_e2ee_stream_v2_variant(
            conn,
            file_row=file_row,
            storage_root=storage_root,
            variant_name="q480",
            manifest_payload=manifest,
            bundle_bytes=b"abcdefghi",
            height=480,
        )
        conn.commit()
        assert (storage_root / "e2ee_stream_v2" / "e2ee-video" / "bundle.bin").exists()
        assert (storage_root / "e2ee_stream_v2" / "e2ee-video" / "derivatives" / "q480" / "bundle.bin").exists()

        cleanup = cleanup_e2ee_stream_v2_assets(conn, uploaded_file_id="e2ee-video", storage_root=storage_root)
        conn.commit()

        assert cleanup["assets_removed"] == 1
        assert cleanup["variants_removed"] == 1
        assert not (storage_root / "e2ee_stream_v2" / "e2ee-video").exists()
        assert conn.execute("SELECT COUNT(*) FROM media_e2ee_stream_v2_assets").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM media_e2ee_stream_v2_variants").fetchone()[0] == 0
    finally:
        conn.close()


def test_e2ee_stream_v2_chunk_route_reports_bundle_truncated_when_bundle_shrinks(tmp_path):
    db_path = tmp_path / "shared-e2ee-stream-v2-truncated.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            conn,
            storage_root,
            file_id="e2ee-video",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            privacy_mode="e2ee",
            payload=b"ciphertext-video",
        )
        _mark_file_as_e2ee(conn, "e2ee-video")
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id='e2ee-video'").fetchone()
        manifest = {
            "e2ee_stream_version": 2,
            "chunk_size": 8,
            "chunk_count": 2,
            "content_type": "video/mp4",
            "duration_hint": 1.0,
            "chunks": [
                {
                    "chunk_index": 0,
                    "nonce": "AAAAAAAAAAAAAAAA",
                    "ciphertext_offset": 0,
                    "ciphertext_size": 4,
                    "plaintext_offset": 0,
                    "plaintext_size": 4,
                    "ciphertext_sha256": "11" * 32,
                },
                {
                    "chunk_index": 1,
                    "nonce": "BBBBBBBBBBBBBBBB",
                    "ciphertext_offset": 4,
                    "ciphertext_size": 4,
                    "plaintext_offset": 4,
                    "plaintext_size": 4,
                    "ciphertext_sha256": "22" * 32,
                },
            ],
        }
        video_routes.upsert_e2ee_stream_v2_asset(
            conn,
            file_row=file_row,
            storage_root=storage_root,
            manifest_payload=manifest,
            bundle_bytes=b"ABCDEFGH",
        )
        conn.commit()
        (storage_root / "e2ee_stream_v2" / "e2ee-video" / "bundle.bin").write_bytes(b"ABCDEF")
        payload, error = resolve_e2ee_chunk_response(
            conn,
            file_row=file_row,
            storage_root=storage_root,
            chunk_index=1,
        )
        assert payload is None
        assert error["error"] == "bundle_truncated"
    finally:
        conn.close()


def test_e2ee_stream_v2_chunk_route_rejects_corrupt_ciphertext_hash(tmp_path):
    db_path = tmp_path / "shared-e2ee-stream-v2-corrupt.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            conn,
            storage_root,
            file_id="e2ee-video",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            privacy_mode="e2ee",
            payload=b"ciphertext-video",
        )
        _mark_file_as_e2ee(conn, "e2ee-video")
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id='e2ee-video'").fetchone()
        video_routes.upsert_e2ee_stream_v2_asset(
            conn,
            file_row=file_row,
            storage_root=storage_root,
            manifest_payload={
                "e2ee_stream_version": 2,
                "chunk_size": 8,
                "chunk_count": 1,
                "content_type": "video/mp4",
                "duration_hint": 1.0,
                "chunks": [
                    {
                        "chunk_index": 0,
                        "nonce": "AAAAAAAAAAAAAAAA",
                        "ciphertext_offset": 0,
                        "ciphertext_size": 4,
                        "plaintext_offset": 0,
                        "plaintext_size": 4,
                        "ciphertext_sha256": "00" * 32,
                    },
                ],
            },
            bundle_bytes=b"ABCD",
        )
        conn.commit()
        payload, error = resolve_e2ee_chunk_response(
            conn,
            file_row=file_row,
            storage_root=storage_root,
            chunk_index=0,
        )
        assert payload is None
        assert error["error"] == "bundle_corrupt"
    finally:
        conn.close()


def test_shared_e2ee_stream_v2_simulation_can_decrypt_chunked_plaintext(tmp_path):
    db_path = tmp_path / "shared-e2ee-stream-v2-sim.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        sealed = _seed_real_e2ee_file(
            owner_conn,
            storage_root,
            file_id="e2ee-video",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            plaintext=b"simulated-shared-e2ee-stream-v2-payload",
        )
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="e2ee-video",
            title="Secret",
            visibility="unlisted",
            share_password="SharePass123",
            share_wrapped_file_key_envelope=sealed["share_wrapped_file_key_envelope"],
            share_max_views=6,
        )
        owner_conn.commit()
        token = video["share_url"].rsplit("/", 1)[-1]
    finally:
        owner_conn.close()

    owner_client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    manifest, bundle = _build_real_stream_v2_manifest_and_bundle(
        sealed["file_key"],
        sealed["plaintext"],
        content_type="video/mp4",
    )
    prepared = owner_client.post(
        "/api/media/e2ee-video/e2ee-stream-v2",
        data={
            "manifest_json": json.dumps(manifest),
            "bundle": (io.BytesIO(bundle), "bundle.bin"),
        },
        content_type="multipart/form-data",
    )
    assert prepared.status_code == 200
    assert prepared.get_json()["asset"]["available"] is True

    viewer = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    unlocked = viewer.post(f"/api/videos/shared/{token}/unlock", json={"password": "SharePass123"})
    assert unlocked.status_code == 200
    share_session = _share_session_query(unlocked)

    playback = viewer.get(f"/api/videos/shared/{token}/playback{share_session}")
    assert playback.status_code == 200
    playback_json = playback.get_json()
    assert playback_json["player_strategy"] == "browser_e2ee_stream_v2"
    assert playback_json["mode"] == "e2ee_stream_v2"

    key_payload = viewer.get(playback_json["e2ee_key_url"]).get_json()["e2ee_share"]
    unwrapped_file_key = _unwrap_shared_file_key_from_fragment(
        key_payload["wrapped_file_key_envelope"],
        sealed["share_fragment_key"],
    )
    assert unwrapped_file_key == sealed["file_key"]

    shared_manifest = viewer.get(playback_json["manifest_url"])
    assert shared_manifest.status_code == 200
    manifest_json = shared_manifest.get_json()
    plaintext_parts = []
    for chunk_meta in manifest_json["chunks"]:
        chunk_res = viewer.get(
            playback_json["chunk_url_template"].replace("__INDEX__", str(chunk_meta["chunk_index"]))
        )
        assert chunk_res.status_code == 200
        chunk_plain = AESGCM(unwrapped_file_key).decrypt(
            base64.b64decode(chunk_meta["nonce"]),
            chunk_res.data,
            None,
        )
        plaintext_parts.append(chunk_plain)
    assert b"".join(plaintext_parts) == sealed["plaintext"]


def test_shared_e2ee_stream_v2_endpoints_respect_revoked_expired_and_view_limits(tmp_path):
    db_path = tmp_path / "shared-e2ee-stream-v2-guards.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            owner_conn,
            storage_root,
            file_id="e2ee-video",
            owner_user_id=1,
            filename="secret.mp4",
            mime="video/mp4",
            privacy_mode="e2ee",
            payload=b"ciphertext-video",
        )
        _mark_file_as_e2ee(owner_conn, "e2ee-video")
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="e2ee-video",
            title="Secret",
            visibility="unlisted",
            share_password="SharePass123",
            share_wrapped_file_key_envelope='{"alg":"AES-GCM","v":1,"nonce":"AAAAAAAAAAAAAAAA","ciphertext":"AQIDBA=="}',
            share_max_views=1,
        )
        owner_conn.commit()
        video_id = video["id"]
        token = video["share_url"].rsplit("/", 1)[-1]
    finally:
        owner_conn.close()

    owner_client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()
    manifest = {
        "e2ee_stream_version": 2,
        "chunk_size": 8,
        "chunk_count": 1,
        "content_type": "video/mp4",
        "duration_hint": 1.0,
        "byte_range_hint": {"total_plaintext_bytes": 4},
        "chunks": [
            {
                "chunk_index": 0,
                "nonce": "AAAAAAAAAAAAAAAA",
                "ciphertext_offset": 0,
                "ciphertext_size": 4,
                "plaintext_offset": 0,
                "plaintext_size": 4,
                "ciphertext_sha256": "11" * 32,
            }
        ],
    }
    prepared = owner_client.post(
        "/api/media/e2ee-video/e2ee-stream-v2",
        data={
            "manifest_json": json.dumps(manifest),
            "bundle": (io.BytesIO(b"ABCD"), "bundle.bin"),
        },
        content_type="multipart/form-data",
    )
    assert prepared.status_code == 200

    expired_conn = sqlite3.connect(db_path)
    expired_conn.row_factory = sqlite3.Row
    expired_conn.execute(
        "UPDATE video_share_links SET expires_at='2000-01-01T00:00:00' WHERE video_id=?",
        (video_id,),
    )
    expired_conn.commit()
    expired_conn.close()
    expired_client = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    expired_page = expired_client.get(f"/shared/videos/{token}")
    assert expired_page.status_code == 410
    assert "分享已結束" in expired_page.get_data(as_text=True)
    assert "已到期" in expired_page.get_data(as_text=True)
    expired_manifest = expired_client.get(f"/api/videos/shared/{token}/e2ee-stream-v2/manifest")
    assert expired_manifest.status_code == 410
    assert expired_manifest.get_json()["reason"] == "expired"

    reopen_conn = sqlite3.connect(db_path)
    reopen_conn.row_factory = sqlite3.Row
    reopen_conn.execute(
        "UPDATE video_share_links SET expires_at=NULL, access_count=0 WHERE video_id=?",
        (video_id,),
    )
    reopen_conn.commit()
    reopen_conn.close()
    first_viewer = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    first_unlock = first_viewer.post(f"/api/videos/shared/{token}/unlock", json={"password": "SharePass123"})
    assert first_unlock.status_code == 200
    share_session = _share_session_query(first_unlock)
    assert first_viewer.get(f"/api/videos/shared/{token}/e2ee-stream-v2/manifest{share_session}").status_code == 200

    second_viewer = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    exhausted = second_viewer.post(f"/api/videos/shared/{token}/unlock", json={"password": "SharePass123"})
    assert exhausted.status_code == 410
    assert exhausted.get_json()["reason"] == "view_limit_reached"

    regenerated = owner_client.put(
        f"/api/videos/{video_id}/share-link",
        json={
            "regenerate": True,
            "share_wrapped_file_key_envelope": '{"alg":"AES-GCM","v":1,"nonce":"BBBBBBBBBBBBBBBB","ciphertext":"BQYHCA=="}',
        },
    )
    assert regenerated.status_code == 200
    new_token = regenerated.get_json()["share_link"]["url"].rsplit("/", 1)[-1]
    revoked = owner_client.delete(f"/api/videos/{video_id}/share-link")
    assert revoked.status_code == 200
    revoked_manifest = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client().get(
        f"/api/videos/shared/{new_token}/e2ee-stream-v2/manifest"
    )
    assert revoked_manifest.status_code == 404


def test_shared_video_regeneration_for_e2ee_requires_new_browser_side_envelope(tmp_path):
    db_path = tmp_path / "shared-e2ee-regenerate.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(owner_conn, storage_root, file_id="e2ee-video", owner_user_id=1, filename="secret.mp4", mime="video/mp4", privacy_mode="e2ee", payload=b"ciphertext-video")
        _mark_file_as_e2ee(owner_conn, "e2ee-video")
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="e2ee-video",
            title="Secret",
            visibility="unlisted",
            share_wrapped_file_key_envelope='{"alg":"AES-GCM","v":1,"nonce":"AAAAAAAAAAAAAAAA","ciphertext":"AQIDBA=="}',
        )
        owner_conn.commit()
        video_id = video["id"]
    finally:
        owner_conn.close()

    owner_client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
    ).test_client()

    missing = owner_client.put(f"/api/videos/{video_id}/share-link", json={"regenerate": True})
    assert missing.status_code == 400
    assert "瀏覽器端分享授權" in missing.get_json()["msg"]

    forbidden = owner_client.put(f"/api/videos/{video_id}/share-link", json={"regenerate": True, "e2ee_password": "nope"})
    assert forbidden.status_code == 400
    assert forbidden.get_json()["error"] == "forbidden_share_secret_field"

    regenerated = owner_client.put(
        f"/api/videos/{video_id}/share-link",
        json={
            "regenerate": True,
            "share_wrapped_file_key_envelope": '{"alg":"AES-GCM","v":1,"nonce":"BBBBBBBBBBBBBBBB","ciphertext":"BQYHCA=="}',
        },
    )
    assert regenerated.status_code == 200
    body = regenerated.get_json()
    assert body["share_link"]["requires_fragment_key"] is True
    assert body["video"]["share_requires_fragment_key"] is True


def test_share_link_update_and_revoke_succeed_with_real_audit_service(tmp_path):
    db_path = tmp_path / "shared-real-audit.db"
    storage_root = tmp_path / "storage-real-audit"
    storage_root.mkdir()
    _init_db(db_path)
    audit_func = _configure_real_audit_for_test(db_path, tmp_path / "runtime-real-audit")
    owner_conn = sqlite3.connect(db_path)
    owner_conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            owner_conn,
            storage_root,
            file_id="plain-video",
            owner_user_id=1,
            filename="movie.mp4",
            mime="video/mp4",
            privacy_mode="standard_plain",
            payload=b"plain-video",
        )
        video = publish_video(
            owner_conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="plain-video",
            title="Plain",
            visibility="unlisted",
        )
        owner_conn.commit()
        video_id = video["id"]
    finally:
        owner_conn.close()

    owner_client = _build_app(
        db_path,
        storage_root,
        Fernet(Fernet.generate_key()),
        current_user={"id": 1, "username": "alice", "role": "user", "member_level": "trusted", "effective_level": "trusted"},
        audit_func=audit_func,
    ).test_client()

    regenerated = owner_client.put(f"/api/videos/{video_id}/share-link", json={"regenerate": True})
    assert regenerated.status_code == 200
    token = regenerated.get_json()["share_link"]["url"].rsplit("/", 1)[-1]

    revoked = owner_client.delete(f"/api/videos/{video_id}/share-link")
    assert revoked.status_code == 200

    anonymous = _build_app(db_path, storage_root, Fernet(Fernet.generate_key()), current_user=None).test_client()
    revoked_view = anonymous.get(f"/api/videos/shared/{token}")
    assert revoked_view.status_code == 404


def test_shared_video_three_privacy_modes_complete_unlock_flow(tmp_path):
    """User-reported regression: share page stuck after password input, no
    response to submit. Reproduces full unlock flow for all 3 privacy modes
    so any future regression in this user-facing flow fails CI:

      1) standard_plain   — server stores plaintext on disk
      2) server_encrypted — Fernet-encrypted on disk, decrypted server-side
      3) e2ee             — browser-side decryption only

    For each mode the test exercises:
      - GET /shared/videos/<token>            → returns the inline HTML (200)
      - GET /api/videos/shared/<token>        → 401 password_required (locked)
      - POST /api/videos/shared/<token>/unlock with wrong password → 403
      - POST .../unlock with right password → 200 ok
      - GET /api/videos/shared/<token>        → 200 with video metadata
      - GET /api/videos/shared/<token>/playback → 200 playback descriptor
    """
    import sqlite3
    from cryptography.fernet import Fernet

    cases = [
        ("standard_plain", False),   # plaintext on disk
        ("server_encrypted", False), # Fernet on disk
        ("e2ee", True),              # browser-side only
    ]

    for privacy_mode, is_e2ee in cases:
        db_path = tmp_path / f"share-{privacy_mode}.db"
        storage_root = tmp_path / f"storage-{privacy_mode}"
        storage_root.mkdir()
        _init_db(db_path)
        owner_conn = sqlite3.connect(db_path)
        owner_conn.row_factory = sqlite3.Row
        try:
            file_id = f"video-{privacy_mode}"
            if is_e2ee:
                _seed_uploaded_file(
                    owner_conn, storage_root,
                    file_id=file_id, owner_user_id=1,
                    filename="secret.mp4", mime="video/mp4",
                )
                _mark_file_as_e2ee(owner_conn, file_id)
            else:
                _seed_uploaded_file(
                    owner_conn, storage_root,
                    file_id=file_id, owner_user_id=1,
                    filename="clip.mp4", mime="video/mp4",
                    privacy_mode=privacy_mode,
                )

            kwargs = {
                "actor": {"id": 1, "username": "alice", "role": "user"},
                "cloud_file_id": file_id,
                "title": f"Test {privacy_mode}",
                "visibility": "unlisted",
                "share_password": "P@ssw0rd!",
                "share_max_views": 10,
            }
            if is_e2ee:
                kwargs["share_wrapped_file_key_envelope"] = (
                    '{"alg":"AES-GCM","v":1,"nonce":"AAAAAAAAAAAAAAAA","ciphertext":"AQIDBA=="}'
                )
            video = publish_video(owner_conn, **kwargs)
            owner_conn.commit()
            token = video["share_url"].rsplit("/", 1)[-1]
        finally:
            owner_conn.close()

        client = _build_app(
            db_path, storage_root,
            Fernet(Fernet.generate_key()),
            current_user=None,
        ).test_client()

        # 1) Inline HTML page renders (no 5xx, no f-string crash). Per
        # issue #182 fix, the runtime JS now lives in
        # public/js/shared-video.js (loaded via <script src>), and the
        # token is delivered through a JSON island so CSP allows it.
        page = client.get(f"/shared/videos/{token}")
        assert page.status_code == 200, f"{privacy_mode}: share page HTML status {page.status_code}"
        page_html = page.data.decode("utf-8")
        assert "share-password-form" in page_html, f"{privacy_mode}: form missing from rendered HTML"
        assert '<script src="/js/shared-video.js' in page_html, (
            f"{privacy_mode}: external shared-video.js not loaded — fix #182 may have regressed"
        )
        assert '<script id="share-token" type="application/json">' in page_html, (
            f"{privacy_mode}: token JSON island missing"
        )

        # 2) Metadata while locked → 401 password_required
        locked = client.get(f"/api/videos/shared/{token}")
        assert locked.status_code == 401, (
            f"{privacy_mode}: locked metadata expected 401, got {locked.status_code} "
            f"body={locked.get_json()}"
        )
        body = locked.get_json()
        assert body.get("password_required") is True, f"{privacy_mode}: missing password_required flag"

        # 3) Wrong password → 403
        bad = client.post(
            f"/api/videos/shared/{token}/unlock",
            json={"password": "wrong-attempt-1"},
        )
        assert bad.status_code == 403, (
            f"{privacy_mode}: wrong password expected 403, got {bad.status_code}"
        )

        # 4) Correct password → 200 OK + session granted
        unlock = client.post(
            f"/api/videos/shared/{token}/unlock",
            json={"password": "P@ssw0rd!"},
        )
        assert unlock.status_code == 200, (
            f"{privacy_mode}: correct password expected 200, got {unlock.status_code} "
            f"body={unlock.get_json()}"
        )
        assert unlock.get_json()["ok"] is True
        share_session = _share_session_query(unlock)

        # 5) Metadata after unlock → 200 with video payload
        detail = client.get(f"/api/videos/shared/{token}{share_session}")
        assert detail.status_code == 200, (
            f"{privacy_mode}: post-unlock detail expected 200, got {detail.status_code} "
            f"body={detail.get_json()}"
        )
        assert detail.get_json()["video"]["share_password_required"] is True

        # 6) Playback descriptor available
        playback = client.get(f"/api/videos/shared/{token}/playback{share_session}")
        assert playback.status_code == 200, (
            f"{privacy_mode}: playback expected 200, got {playback.status_code} "
            f"body={playback.get_json()}"
        )
        playback_json = playback.get_json()
        if is_e2ee:
            assert playback_json["mode"].startswith("e2ee"), (
                f"e2ee: expected e2ee_* playback mode, got {playback_json['mode']}"
            )
        else:
            assert playback_json["mode"] in {"hls", "realtime_proxy", "high_performance", "server_encrypted"}, (
                f"{privacy_mode}: expected non-e2ee playback mode, got {playback_json['mode']}"
            )


def test_shared_video_page_csp_does_not_block_inline_script(tmp_path):
    """Issue #182 regression guard.

    Talisman is configured with strict CSP `script-src: 'self'` (no
    'unsafe-inline', no nonce). Inline `<script>` blocks are blocked by
    every modern browser. The shared video page must not rely on inline
    JS to function.

    This test fires when:
      A) the response has an inline <script>...</script> body AND
      B) the CSP forbids both 'unsafe-inline' and nonce-X for script-src

    The fix is either (A) move the JS to /static or (B) emit a nonce
    matching the script tag.
    """
    import sqlite3, re
    from cryptography.fernet import Fernet

    db_path = tmp_path / "csp-share.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_uploaded_file(
            conn, storage_root,
            file_id="video-csp", owner_user_id=1,
            filename="x.mp4", mime="video/mp4",
        )
        video = publish_video(
            conn,
            actor={"id": 1, "username": "alice", "role": "user"},
            cloud_file_id="video-csp",
            title="csp",
            visibility="unlisted",
            share_password="P",
        )
        conn.commit()
        token = video["share_url"].rsplit("/", 1)[-1]
    finally:
        conn.close()

    client = _build_app(
        db_path, storage_root,
        Fernet(Fernet.generate_key()),
        current_user=None,
    ).test_client()

    resp = client.get(f"/shared/videos/{token}")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")

    # The test fixture doesn't go through Talisman, so we can't rely on
    # the test response's CSP header. Read the *production* CSP config
    # directly from server.py to know what real browsers will see.
    from pathlib import Path as _Path
    server_py = (_Path(__file__).resolve().parents[3] / "server.py").read_text(encoding="utf-8")
    script_src_match = re.search(r'"script-src":\s*"([^"]+)"', server_py)
    production_script_src = script_src_match.group(1) if script_src_match else ""
    production_allows_inline = (
        "'unsafe-inline'" in production_script_src
        or "'nonce-" in production_script_src
    )

    inline_script = re.search(r"<script>(.{50,})</script>", body, re.DOTALL)
    has_external_script = (
        '<script src="' in body or '<script type="application/json"' in body
    )

    if inline_script and not has_external_script and not production_allows_inline:
        raise AssertionError(
            f"Shared video page emits a non-trivial inline <script> body "
            f"({len(inline_script.group(1))} chars) but the production CSP "
            f"in server.py is `script-src: {production_script_src}` which "
            "forbids inline scripts. Browsers will silently block the JS, "
            "leaving the password form inert and the page stuck at "
            "'讀取中...'.\n"
            "\n"
            "Fix options:\n"
            "  A) Move the inline JS to public/js/shared-video.js and load "
            "via <script src='/js/shared-video.js'>. Pass TOKEN via a JSON "
            "island (<script type='application/json'>...</script>).\n"
            "  B) Configure Talisman with content_security_policy_nonce_in="
            "['script-src'] and emit <script nonce='...'> on this page.\n"
            "\n"
            "See issue #182."
        )


def test_shared_video_page_browser_realistic_full_flow_for_three_modes(tmp_path):
    """Issue #182 deeper guard — go beyond "the file exists" string-grep:

      A) /js/shared-video.js is reachable via the same Flask static handler
         that real browsers will hit (so a misnamed/misplaced file fails CI).
      B) The token JSON island parses with json.loads (catches future
         regressions where someone uses repr() / format() and emits
         single-quoted Python literals — invalid JSON the browser drops).
      C) The external JS file references the exact API URL templates that
         the server actually serves, with a `${TOKEN}` interpolation slot
         (catches drift between route paths and JS fetch calls).
      D) The full 3-mode unlock + metadata + playback flow still works
         when invoked using the parsed-from-island token.
    """
    import re
    import sqlite3
    from cryptography.fernet import Fernet

    cases = [
        ("standard_plain", False),
        ("server_encrypted", False),
        ("e2ee", True),
    ]

    for privacy_mode, is_e2ee in cases:
        db_path = tmp_path / f"realbr-{privacy_mode}.db"
        storage_root = tmp_path / f"realbr-{privacy_mode}-st"
        storage_root.mkdir()
        _init_db(db_path)
        owner_conn = sqlite3.connect(db_path)
        owner_conn.row_factory = sqlite3.Row
        try:
            file_id = f"realbr-{privacy_mode}"
            if is_e2ee:
                _seed_uploaded_file(
                    owner_conn, storage_root,
                    file_id=file_id, owner_user_id=1,
                    filename="x.mp4", mime="video/mp4",
                )
                _mark_file_as_e2ee(owner_conn, file_id)
            else:
                _seed_uploaded_file(
                    owner_conn, storage_root,
                    file_id=file_id, owner_user_id=1,
                    filename="x.mp4", mime="video/mp4",
                    privacy_mode=privacy_mode,
                )
            kwargs = {
                "actor": {"id": 1, "username": "alice", "role": "user"},
                "cloud_file_id": file_id,
                "title": f"realbr-{privacy_mode}",
                "visibility": "unlisted",
                "share_password": "P@ssw0rd!",
                "share_max_views": 10,
            }
            if is_e2ee:
                kwargs["share_wrapped_file_key_envelope"] = (
                    '{"alg":"AES-GCM","v":1,"nonce":"AAAAAAAAAAAAAAAA","ciphertext":"AQIDBA=="}'
                )
            video = publish_video(owner_conn, **kwargs)
            owner_conn.commit()
            html_token = video["share_url"].rsplit("/", 1)[-1]
        finally:
            owner_conn.close()

        client = _build_app(
            db_path, storage_root,
            Fernet(Fernet.generate_key()),
            current_user=None,
        ).test_client()

        # A) external JS reachable via Flask static handler
        js_resp = client.get("/js/shared-video.js")
        assert js_resp.status_code == 200, (
            f"{privacy_mode}: /js/shared-video.js not reachable via static "
            f"handler (status {js_resp.status_code}); check public/ static "
            "mount in server.py"
        )
        js_body = js_resp.data.decode("utf-8")

        # B) token JSON island must parse as strict JSON
        page = client.get(f"/shared/videos/{html_token}").data.decode("utf-8")
        m = re.search(
            r'<script id="share-token" type="application/json">([^<]*)</script>',
            page,
        )
        assert m, f"{privacy_mode}: JSON island missing"
        try:
            parsed_token = json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"{privacy_mode}: JSON island body {m.group(1)!r} is not "
                f"valid JSON ({exc}). If this trips, someone likely changed "
                "the serializer to repr() — use json.dumps."
            )
        assert parsed_token == html_token, (
            f"{privacy_mode}: token round-trip mismatch "
            f"(island={parsed_token!r}, url={html_token!r})"
        )

        # C) JS fetches the URLs the route actually exposes
        for needle in (
            "/api/videos/shared/${encodeURIComponent(TOKEN)}",
            "/api/videos/shared/${encodeURIComponent(TOKEN)}/unlock",
            "/api/videos/shared/${encodeURIComponent(TOKEN)}/playback",
        ):
            assert needle in js_body, (
                f"{privacy_mode}: shared-video.js missing fetch URL `{needle}` "
                "— route path and JS fetch path are out of sync"
            )

        # D) full unlock flow with the parsed token
        unlock = client.post(
            f"/api/videos/shared/{parsed_token}/unlock",
            json={"password": "P@ssw0rd!"},
        )
        assert unlock.status_code == 200, (
            f"{privacy_mode}: unlock with island-parsed token failed "
            f"(status={unlock.status_code}, body={unlock.get_json()})"
        )
        share_session = _share_session_query(unlock)
        detail = client.get(f"/api/videos/shared/{parsed_token}{share_session}")
        assert detail.status_code == 200, (
            f"{privacy_mode}: post-unlock metadata expected 200, "
            f"got {detail.status_code}"
        )
        playback = client.get(f"/api/videos/shared/{parsed_token}/playback{share_session}")
        assert playback.status_code == 200, (
            f"{privacy_mode}: playback descriptor expected 200, "
            f"got {playback.status_code}"
        )


def test_shared_video_token_json_island_resists_script_tag_close_injection():
    """Issue #182 hardening guard.

    The route writes the share token into a JSON island via
    json.dumps + .replace("</", "<\\/"). That defense exists so a token
    that *looked* like </script> can never close the host <script> tag
    and inject markup. This test simulates the exact serialization the
    route does for a malicious-looking token and confirms a) the JSON
    island never closes prematurely, b) it round-trips back to the
    same string under json.loads.

    The serializer lives at routes/videos.py:_shared_video_html (a
    closure inside register_video_routes, so we mirror the two-line
    transformation here).
    """
    import json as _json

    malicious = 'abc</script><script>alert(1)</script>def'
    # Mirror routes/videos.py: share_token_json = json.dumps(...).replace("</", "<\\/")
    serialized = _json.dumps(malicious).replace("</", "<\\/")
    html = (
        '<script id="share-token" type="application/json">'
        + serialized
        + '</script>'
        + '<script src="/js/shared-video.js"></script>'
    )
    opener = '<script id="share-token" type="application/json">'
    start = html.find(opener)
    body_start = start + len(opener)
    body_end = html.find("</script>", body_start)
    assert body_end > body_start, (
        "JSON island never closes — token serializer let </script> through"
    )
    island_body = html[body_start:body_end]
    # In the JSON-string form, "<\/" is the legal way to embed a slash;
    # json.loads turns it back into "</" so the parsed token equals the
    # input.
    parsed = _json.loads(island_body)
    assert parsed == malicious, (
        f"JSON island body did not round-trip the token. "
        f"island_body={island_body!r}, parsed={parsed!r}"
    )
