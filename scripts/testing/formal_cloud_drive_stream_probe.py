#!/usr/bin/env python3
"""Formal cloud-drive upload/HLS/share/realtime/UI lifecycle probe.

This probe is intentionally terminal-state based.  It creates a real MKV with
two audio tracks and a subtitle, uploads it through the live cloud-drive API,
runs the production HLS worker, reads the resulting playlists and media bytes,
unlocks a password share in desktop/mobile browsers, revokes the share, and
removes every product fixture it created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "hackme.formal-cloud-drive-stream-probe/v1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def response_record(response: requests.Response, *, include_body: bool = True) -> dict[str, Any]:
    body: Any = {}
    if include_body:
        try:
            body = response.json()
        except Exception:
            body = {"text_sample": response.text[:500]}
    return {
        "status": response.status_code,
        "body": body,
        "content_type": response.headers.get("Content-Type", ""),
        "streaming_mode": response.headers.get("X-Hackme-Streaming-Mode", ""),
        "transfer_mode": response.headers.get("X-Hackme-Transfer-Mode", ""),
    }


class Api:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""

    def refresh_csrf(self) -> None:
        response = self.session.get(f"{self.base_url}/api/csrf-token", timeout=30)
        response.raise_for_status()
        self.csrf = str((response.json() or {}).get("csrf_token") or self.session.cookies.get("csrf_token") or "")
        if not self.csrf:
            raise RuntimeError("csrf token missing")

    def login(self) -> dict[str, Any]:
        self.refresh_csrf()
        response = self.session.post(
            f"{self.base_url}/api/login",
            json={"username": self.username, "password": self.password},
            headers={"X-CSRF-Token": self.csrf},
            timeout=30,
        )
        record = response_record(response)
        if response.status_code == 200 and isinstance(record["body"], dict) and record["body"].get("ok") is True:
            self.refresh_csrf()
        return record

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers["X-CSRF-Token"] = self.csrf
        return self.session.request(
            method.upper(),
            f"{self.base_url}{path}",
            headers=headers,
            timeout=kwargs.pop("timeout", 120),
            **kwargs,
        )


def make_fixture(out_dir: Path) -> tuple[Path, dict[str, Any]]:
    subtitle = out_dir / "fixture.zh.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:04,000\n正式雲端分享串流測試\n",
        encoding="utf-8",
    )
    media = out_dir / "formal_cloud_dual_audio_subtitle.mkv"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=12",
        "-i", str(subtitle),
        "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0", "-map", "3:s:0",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-c:s", "srt",
        "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=eng",
        "-metadata:s:s:0", "language=zho", "-shortest", str(media),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    if completed.returncode != 0 or not media.is_file() or media.stat().st_size <= 0:
        raise RuntimeError(f"fixture ffmpeg failed: {completed.stderr[-1000:]}")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    payload = json.loads(probe.stdout or "{}") if probe.returncode == 0 else {}
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    summary = {
        "path": str(media.resolve()),
        "size_bytes": media.stat().st_size,
        "sha256": sha256(media),
        "ffprobe_returncode": probe.returncode,
        "duration_seconds": float((payload.get("format") or {}).get("duration") or 0),
        "video_streams": sum(1 for row in streams if row.get("codec_type") == "video"),
        "audio_streams": sum(1 for row in streams if row.get("codec_type") == "audio"),
        "subtitle_streams": sum(1 for row in streams if row.get("codec_type") == "subtitle"),
    }
    return media, summary


def parse_playlist_uris(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def browser_checks(base_url: str, token: str, password: str, screenshot_dir: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for label, viewport in (
            ("desktop", {"width": 1366, "height": 900}),
            ("mobile", {"width": 390, "height": 844}),
        ):
            context = browser.new_context(ignore_https_errors=True, viewport=viewport)
            page = context.new_page()
            console_errors: list[str] = []
            page_errors: list[str] = []
            failed_responses: list[dict[str, Any]] = []
            page.on("console", lambda message, target=console_errors: target.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error, target=page_errors: target.append(str(error)))
            page.on(
                "response",
                lambda response, target=failed_responses: target.append({"url": response.url, "status": response.status})
                if response.status >= 400 and "/api/storage/shared/" in response.url else None,
            )
            page.goto(f"{base_url}/shared/files/{token}", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("#shared-file-password-input", timeout=30_000)
            page.fill("#shared-file-password-input", password)
            page.click("#shared-file-password-form button[type=submit]")
            page.wait_for_function(
                "() => !document.querySelector('#shared-file-preview-btn')?.disabled",
                timeout=30_000,
            )
            page.click("#shared-file-preview-btn")
            page.wait_for_selector("#shared-file-hls-player", state="attached", timeout=60_000)
            state = page.evaluate(
                """() => {
                  const player = document.querySelector('#shared-file-hls-player');
                  const panel = document.querySelector('.panel');
                  return {
                    player_present: !!player,
                    player_tag: player?.tagName || '',
                    preview_hidden: !!document.querySelector('#shared-file-preview')?.hidden,
                    root_overflow_px: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
                    panel_right: panel?.getBoundingClientRect()?.right || 0,
                    viewport_width: window.innerWidth,
                  };
                }"""
            )
            screenshot = screenshot_dir / f"cloud_share_{label}.png"
            page.screenshot(path=str(screenshot), full_page=True)
            context.close()
            rows.append({
                "viewport": label,
                "dimensions": viewport,
                "state": state,
                "console_errors": console_errors,
                "page_errors": page_errors,
                "failed_responses": failed_responses,
                "screenshot": str(screenshot.resolve()),
                "screenshot_size_bytes": screenshot.stat().st_size,
                "context_closed": True,
            })
        browser.close()
    return {"rows": rows, "browser_closed": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--screenshot-dir", required=True)
    args = parser.parse_args()

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    password = os.environ.get("HACKME_PROBE_USER_PASSWORD") or os.environ.get("HACKME_TEST_PASSWORD") or ""
    share_password = os.environ.get("HACKME_CLOUD_PROBE_SHARE_PASSWORD") or ""
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "fixture": {},
        "upload": {},
        "hls_worker": {},
        "stream": {},
        "share": {},
        "browser": {},
        "cleanup": {},
        "errors": [],
    }
    api = Api(args.base_url, "test", password)
    storage_file_id = ""
    file_id = ""
    share_id = ""
    token = ""
    try:
        if not password or not share_password:
            raise RuntimeError("probe credentials missing")
        login = api.login()
        if login.get("status") != 200 or (login.get("body") or {}).get("ok") is not True:
            raise RuntimeError("test login failed")
        media, fixture = make_fixture(out_path.parent)
        result["fixture"] = fixture
        upload_started = time.monotonic()
        with media.open("rb") as handle:
            response = api.request(
                "POST",
                "/api/storage/files",
                files={"file": (media.name, handle, "video/x-matroska")},
                data={
                    "virtual_path": f"formal/{uuid.uuid4().hex}/{media.name}",
                    "display_name": media.name,
                    "privacy_mode": "standard_plain",
                },
                timeout=600,
            )
        upload = response_record(response)
        result["upload"] = {
            **upload,
            "elapsed_seconds": round(time.monotonic() - upload_started, 3),
        }
        upload_body = upload.get("body") if isinstance(upload.get("body"), dict) else {}
        storage_file = upload_body.get("storage_file") if isinstance(upload_body.get("storage_file"), dict) else {}
        file_payload = upload_body.get("file") if isinstance(upload_body.get("file"), dict) else {}
        storage_file_id = str(storage_file.get("id") or "")
        file_id = str(storage_file.get("file_id") or file_payload.get("file_id") or "")
        if response.status_code != 200 or not storage_file_id or not file_id:
            raise RuntimeError("cloud upload did not return file identities")

        worker_env = os.environ.copy()
        worker_env.update({
            "PYTHONPATH": str(ROOT),
            "HACKME_MEDIA_HLS_COPY_FIRST": "1",
            "HACKME_MEDIA_HLS_FORCE_COPY": "1",
            "HACKME_MEDIA_HLS_COPY_FALLBACK_TRANSCODE": "0",
            "HACKME_MEDIA_HLS_ORIGINAL_VARIANT_MODE": "always",
            "HACKME_MEDIA_HLS_INCLUDE_ORIGINAL": "1",
            "HACKME_MEDIA_HLS_QUALITY_HEIGHTS": "none",
        })
        worker_command = [
            sys.executable,
            str(ROOT / "scripts" / "media" / "hls_prepare_worker.py"),
            "--db-path", str(runtime_root / "database" / "database.db"),
            "--storage-root", str(runtime_root / "storage"),
            "--file-id", file_id,
            "--video-id", "0",
            "--owner-user-id", "0",
            "--title", media.name,
            "--ffmpeg-bin", "ffmpeg",
            "--ffprobe-bin", "ffprobe",
        ]
        worker_started = time.monotonic()
        worker = subprocess.run(
            worker_command,
            env=worker_env,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        worker_lines = [line for line in worker.stdout.splitlines() if line.strip().startswith("{")]
        worker_payload = json.loads(worker_lines[-1]) if worker_lines else {}
        result["hls_worker"] = {
            "returncode": worker.returncode,
            "elapsed_seconds": round(time.monotonic() - worker_started, 3),
            "payload": worker_payload,
            "stderr_sample": worker.stderr[-1000:],
        }
        if worker.returncode != 0 or worker_payload.get("ok") is not True:
            raise RuntimeError("HLS worker did not reach ready")

        preview_response = api.request("GET", f"/api/cloud-drive/files/{file_id}/preview")
        preview = response_record(preview_response)
        preview_body = preview.get("body") if isinstance(preview.get("body"), dict) else {}
        preview_meta = preview_body.get("preview") if isinstance(preview_body.get("preview"), dict) else {}
        stream_asset = preview_meta.get("stream_asset") if isinstance(preview_meta.get("stream_asset"), dict) else {}
        master_response = api.request("GET", f"/api/cloud-drive/files/{file_id}/hls/master.m3u8")
        master_text = master_response.text
        master_uris = parse_playlist_uris(master_text)
        if not master_uris:
            raise RuntimeError("owner HLS master contains no variants")
        variant_uri = master_uris[0]
        variant_response = api.request("GET", urljoin(f"/api/cloud-drive/files/{file_id}/hls/", variant_uri))
        variant_uris = parse_playlist_uris(variant_response.text)
        segment_uri = next((uri for uri in variant_uris if not uri.endswith(".mp4") or "init" not in uri), "")
        if not segment_uri:
            raise RuntimeError("owner HLS variant contains no media segment")
        segment_response = api.request(
            "GET",
            urljoin(f"/api/cloud-drive/files/{file_id}/hls/{variant_uri}", segment_uri),
        )
        subtitles = stream_asset.get("subtitles") if isinstance(stream_asset.get("subtitles"), list) else []
        subtitle_url = str((subtitles[0] if subtitles else {}).get("url") or "")
        subtitle_response = api.request("GET", subtitle_url) if subtitle_url else None
        result["stream"] = {
            "preview": preview,
            "status": stream_asset.get("status"),
            "master_manifest_ready": stream_asset.get("master_manifest_ready"),
            "audio_track_count": len(stream_asset.get("audio_tracks") or []),
            "subtitle_count": len(subtitles),
            "master_status": master_response.status_code,
            "master_extm3u": master_text.startswith("#EXTM3U"),
            "variant_uri": variant_uri,
            "variant_status": variant_response.status_code,
            "variant_extm3u": variant_response.text.startswith("#EXTM3U"),
            "segment_uri": segment_uri,
            "segment_status": segment_response.status_code,
            "segment_bytes": len(segment_response.content),
            "subtitle_status": subtitle_response.status_code if subtitle_response is not None else 0,
            "subtitle_webvtt": bool(subtitle_response is not None and subtitle_response.text.startswith("WEBVTT")),
        }

        share_response = api.request(
            "POST",
            "/api/storage/share-links",
            json={"storage_file_id": storage_file_id, "share_password": share_password, "can_preview": True},
        )
        share_record = response_record(share_response)
        share_link = ((share_record.get("body") or {}).get("share_link") or {}) if isinstance(share_record.get("body"), dict) else {}
        share_id = str(share_link.get("id") or "")
        token = str(share_link.get("token") or "")
        if share_response.status_code != 200 or not share_id or not token:
            raise RuntimeError("share link creation failed")
        unauth = requests.get(f"{args.base_url}/api/storage/shared/{token}", timeout=60, verify=False)
        wrong = requests.get(
            f"{args.base_url}/api/storage/shared/{token}",
            headers={"X-Share-Password": "definitely-wrong"},
            timeout=60,
            verify=False,
        )
        share_headers = {"X-Share-Password": share_password}
        unlocked = requests.get(
            f"{args.base_url}/api/storage/shared/{token}",
            headers=share_headers,
            timeout=60,
            verify=False,
        )
        unlocked_record = response_record(unlocked)
        shared_file = ((unlocked_record.get("body") or {}).get("file") or {}) if isinstance(unlocked_record.get("body"), dict) else {}
        shared_stream = shared_file.get("stream_asset") if isinstance(shared_file.get("stream_asset"), dict) else {}
        shared_master = requests.get(
            f"{args.base_url}/api/storage/shared/{token}/hls/master.m3u8",
            headers=share_headers,
            timeout=60,
            verify=False,
        )
        shared_master_uris = parse_playlist_uris(shared_master.text)
        shared_variant_uri = shared_master_uris[0] if shared_master_uris else ""
        shared_variant_url = urljoin(
            f"{args.base_url}/api/storage/shared/{token}/hls/",
            shared_variant_uri,
        )
        shared_variant = requests.get(shared_variant_url, headers=share_headers, timeout=60, verify=False)
        shared_segment_uris = parse_playlist_uris(shared_variant.text)
        shared_segment_uri = next((uri for uri in shared_segment_uris if not uri.endswith(".mp4") or "init" not in uri), "")
        shared_segment_url = urljoin(shared_variant_url, shared_segment_uri)
        shared_segment = requests.get(shared_segment_url, headers=share_headers, timeout=60, verify=False)
        shared_subtitles = shared_stream.get("subtitles") if isinstance(shared_stream.get("subtitles"), list) else []
        shared_subtitle_url = str((shared_subtitles[0] if shared_subtitles else {}).get("url") or "")
        shared_subtitle = requests.get(
            f"{args.base_url}{shared_subtitle_url}", headers=share_headers, timeout=60, verify=False,
        ) if shared_subtitle_url else None
        proxy = requests.get(
            f"{args.base_url}/api/storage/shared/{token}/realtime-proxy?start=1",
            headers=share_headers,
            timeout=(30, 180),
            verify=False,
            stream=True,
        )
        proxy_first_chunk = next(proxy.iter_content(chunk_size=64 * 1024), b"")
        proxy_record = response_record(proxy, include_body=False)
        proxy.close()
        result["share"] = {
            "create": share_record,
            "share_id": share_id,
            "token": token,
            "password_required_status": unauth.status_code,
            "wrong_password_status": wrong.status_code,
            "unlocked": unlocked_record,
            "stream_status": shared_stream.get("status"),
            "audio_track_count": len(shared_stream.get("audio_tracks") or []),
            "subtitle_count": len(shared_subtitles),
            "master_status": shared_master.status_code,
            "master_extm3u": shared_master.text.startswith("#EXTM3U"),
            "variant_status": shared_variant.status_code,
            "variant_extm3u": shared_variant.text.startswith("#EXTM3U"),
            "segment_status": shared_segment.status_code,
            "segment_bytes": len(shared_segment.content),
            "subtitle_status": shared_subtitle.status_code if shared_subtitle is not None else 0,
            "subtitle_webvtt": bool(shared_subtitle is not None and shared_subtitle.text.startswith("WEBVTT")),
            "realtime_proxy": {
                **proxy_record,
                "first_chunk_bytes": len(proxy_first_chunk),
            },
        }

        result["browser"] = browser_checks(
            args.base_url.rstrip("/"), token, share_password, Path(args.screenshot_dir).expanduser().resolve(),
        )

        revoke = api.request("POST", f"/api/storage/share-links/{share_id}/revoke", json={})
        denied = requests.get(
            f"{args.base_url}/api/storage/shared/{token}",
            headers=share_headers,
            timeout=60,
            verify=False,
        )
        trashed = api.request("DELETE", f"/api/storage/files/{storage_file_id}")
        purged = api.request("DELETE", f"/api/storage/files/{storage_file_id}/purge")
        owner_missing = api.request("GET", f"/api/cloud-drive/files/{file_id}/preview")
        result["cleanup"] = {
            "revoke": response_record(revoke),
            "revoked_access_status": denied.status_code,
            "trash": response_record(trashed),
            "purge": response_record(purged),
            "owner_preview_after_purge_status": owner_missing.status_code,
        }

        browser_rows = result["browser"].get("rows") or []
        result["ok"] = bool(
            fixture.get("duration_seconds", 0) >= 10
            and fixture.get("audio_streams") == 2
            and fixture.get("subtitle_streams") == 1
            and result["upload"].get("status") == 200
            and result["hls_worker"].get("returncode") == 0
            and result["stream"].get("status") == "ready"
            and result["stream"].get("master_manifest_ready") is True
            and result["stream"].get("audio_track_count", 0) >= 2
            and result["stream"].get("subtitle_count", 0) >= 1
            and result["stream"].get("master_status") == 200
            and result["stream"].get("variant_status") == 200
            and result["stream"].get("segment_status") == 200
            and result["stream"].get("segment_bytes", 0) > 0
            and result["stream"].get("subtitle_webvtt") is True
            and result["share"].get("password_required_status") == 401
            and result["share"].get("wrong_password_status") == 403
            and (result["share"].get("unlocked") or {}).get("status") == 200
            and result["share"].get("master_status") == 200
            and result["share"].get("variant_status") == 200
            and result["share"].get("segment_status") == 200
            and result["share"].get("segment_bytes", 0) > 0
            and result["share"].get("subtitle_webvtt") is True
            and (result["share"].get("realtime_proxy") or {}).get("status") == 200
            and (result["share"].get("realtime_proxy") or {}).get("first_chunk_bytes", 0) > 0
            and {row.get("viewport") for row in browser_rows} == {"desktop", "mobile"}
            and all(
                (row.get("state") or {}).get("player_present") is True
                and int((row.get("state") or {}).get("root_overflow_px") or 0) == 0
                and not row.get("page_errors")
                and int(row.get("screenshot_size_bytes") or 0) > 0
                and row.get("context_closed") is True
                for row in browser_rows
            )
            and (result["cleanup"].get("revoke") or {}).get("status") == 200
            and result["cleanup"].get("revoked_access_status") in {404, 410}
            and (result["cleanup"].get("trash") or {}).get("status") == 200
            and (result["cleanup"].get("purge") or {}).get("status") == 200
            and result["cleanup"].get("owner_preview_after_purge_status") == 404
        )
    except Exception as exc:
        result["errors"].append(f"{exc.__class__.__name__}: {exc}")
        # Best-effort cleanup is recorded and never converted into a PASS.
        try:
            if share_id:
                api.request("POST", f"/api/storage/share-links/{share_id}/revoke", json={})
            if storage_file_id:
                api.request("DELETE", f"/api/storage/files/{storage_file_id}")
                api.request("DELETE", f"/api/storage/files/{storage_file_id}/purge")
        except Exception as cleanup_exc:
            result["errors"].append(f"cleanup:{cleanup_exc.__class__.__name__}: {cleanup_exc}")
    finally:
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "out": str(out_path), "errors": result["errors"]}, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
