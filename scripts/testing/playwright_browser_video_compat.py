#!/usr/bin/env python3
"""Cross-browser video/share preview compatibility probe.

Starts an isolated hackme_web runtime, publishes a tiny MP4 once, then verifies
the shared video page and playback descriptor in Chromium, Firefox, and WebKit.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.playwright_deep_site_check import (  # noqa: E402
    Recorder,
    enable_required_features,
    fetch_json,
    fetch_text,
    free_port,
    generate_tiny_mp4,
    login,
    mkdirs,
    start_server,
    switch_module,
    utc_stamp,
    wait_for_server,
)


SHARE_PASSWORD = "BrowserShare123!"


def browser_dependency_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return (
        "error while loading shared libraries" in lowered
        or "host system is missing dependencies" in lowered
        or "browser executable doesn't exist" in lowered
    )


def attach_error_handlers(page, bucket: list[dict[str, str]], browser_name: str) -> None:
    seen: set[str] = set()

    def add(kind: str, text: str) -> None:
        compact = str(text or "").replace("\n", " ")[:500]
        if not compact:
            return
        # MediaSource/HLS engines can emit non-fatal warnings; keep them in the
        # artifact, but de-duplicate so one browser does not drown the report.
        key = f"{browser_name}:{kind}:{compact}"
        if key in seen or len(bucket) >= 200:
            return
        seen.add(key)
        bucket.append({"browser": browser_name, "type": kind, "text": compact})

    page.on("console", lambda msg: add(f"console.{msg.type}", msg.text))
    page.on("pageerror", lambda exc: add("pageerror", str(exc)))


def generate_multiaudio_mkv(target: Path) -> dict[str, Any]:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        target.write_bytes(generate_tiny_mp4())
        return {"kind": "tiny_mp4_fallback", "multi_audio": False, "reason": "ffmpeg_missing"}
    subtitle = target.with_suffix(".srt")
    subtitle.write_text("1\n00:00:00,200 --> 00:00:02,800\nbrowser smoke subtitle\n", encoding="utf-8")
    cmd = [
        ffmpeg_bin,
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
        str(target),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    return {"kind": "multiaudio_mkv", "multi_audio": True, "path": str(target)}


def publish_video_fixture(page, base_url: str, runtime_root: Path) -> dict[str, Any]:
    video_path = runtime_root / "reports" / "qa" / f"browser_compat_{int(time.time() * 1000)}.mkv"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_meta = generate_multiaudio_mkv(video_path)
    try:
        login(page, base_url, load_app=False)
        enable_required_features(page, base_url)
        switch_module(page, "videos")
        page.evaluate(
            """() => {
                if (typeof switchModuleTab === 'function') switchModuleTab('videos');
                const module = document.querySelector('#module-videos');
                if (module) {
                    module.hidden = false;
                    module.removeAttribute('hidden');
                    module.style.display = 'block';
                }
                const panel = document.querySelector('#video-publish-panel');
                if (panel) {
                    panel.hidden = false;
                    panel.removeAttribute('hidden');
                    panel.style.display = 'block';
                }
                const toggle = document.querySelector('#video-publish-open-btn');
                if (toggle) toggle.setAttribute('aria-expanded', 'true');
            }"""
        )
        page.wait_for_selector("#video-upload-file", state="attached", timeout=5000)
        page.set_input_files("#video-upload-file", str(video_path))
        page.fill("#video-publish-title", "Browser Standard Proxy QA 影音")
        page.fill("#video-publish-description", "跨瀏覽器 Standard 即時轉封裝測試 fixture。")
        page.select_option("#video-publish-visibility", "unlisted")
        page.fill("#video-share-password", SHARE_PASSWORD)
        page.fill("#video-share-max-views", "0")
        with page.expect_response(
            lambda response: "/api/videos/upload" in response.url and response.request.method == "POST",
            timeout=120000,
        ) as upload_info:
            page.click("#video-publish-btn")
        upload_response = upload_info.value
        upload_json = upload_response.json()
        if upload_response.status < 200 or upload_response.status >= 300 or not upload_json.get("ok"):
            raise RuntimeError(f"video upload failed: status={upload_response.status} body={upload_json}")
        page.wait_for_selector("#video-upload-progress:not([hidden])", timeout=5000)
        page.wait_for_function(
            """() => {
                const msg = document.querySelector('#video-msg')?.textContent || '';
                const status = document.querySelector('#video-upload-progress-status')?.textContent || '';
                const percent = document.querySelector('#video-upload-progress-percent')?.textContent || '';
                return /影音已發布/.test(msg) || /處理完成/.test(status) || percent.trim() === '100%';
            }""",
            timeout=60000,
        )
        latest = upload_json.get("video") or {}
        video_id = int(latest.get("id") or 0)
        if video_id <= 0:
            raise RuntimeError(f"video upload response missing id: {upload_json}")
        detail = fetch_json(page, "GET", f"/api/videos/{video_id}")
        video = (detail.get("body") or {}).get("video") or {}
        share_url = str(video.get("share_url") or latest.get("share_url") or "").strip()
        if not share_url:
            raise RuntimeError(f"video share_url missing: detail={detail} upload={upload_json}")
        playback = fetch_json(page, "GET", f"/api/videos/{video_id}/playback")
        master = {"status": 0, "text": ""}
        master_url = str((playback.get("body") or {}).get("master_url") or "")
        if master_url:
            master = fetch_text(page, master_url)
        return {
            "video_id": video_id,
            "share_url": share_url,
            "playback": playback,
            "master": master,
            "latest": latest,
            "fixture": fixture_meta,
        }
    finally:
        try:
            video_path.unlink()
        except FileNotFoundError:
            pass
        try:
            video_path.with_suffix(".srt").unlink()
        except FileNotFoundError:
            pass


def browser_kind(playwright, name: str):
    if name == "chromium":
        return playwright.chromium
    if name == "firefox":
        return playwright.firefox
    if name == "webkit":
        return playwright.webkit
    raise ValueError(f"unsupported browser: {name}")


def evaluate_browser_coverage(
    checks: list[dict[str, Any]],
    *,
    requested_browsers: list[str],
    include_mobile: bool,
    require_all_browsers: bool,
) -> dict[str, Any]:
    """Apply legacy-tolerant or formal browser-completeness semantics."""

    requested_viewports = ["desktop", "mobile"] if include_mobile else ["desktop"]
    expected = [
        {"browser": browser, "viewport": viewport}
        for browser in requested_browsers
        for viewport in requested_viewports
    ]
    observed = {
        (str(item.get("browser") or ""), str(item.get("viewport") or ""))
        for item in checks
    }
    missing = [
        item
        for item in expected
        if (item["browser"], item["viewport"]) not in observed
    ]
    skipped = [
        {
            "browser": str(item.get("browser") or ""),
            "viewport": str(item.get("viewport") or ""),
            "reason": str(item.get("skip_reason") or ""),
        }
        for item in checks
        if item.get("skipped")
    ]
    failed = [
        {
            "browser": str(item.get("browser") or ""),
            "viewport": str(item.get("viewport") or ""),
            "skipped": bool(item.get("skipped")),
            "exception": str(item.get("exception") or ""),
        }
        for item in checks
        if not item.get("ok")
    ]
    runnable = [item for item in checks if not item.get("skipped")]
    legacy_ok = bool(runnable) and all(item.get("ok") for item in runnable)
    formal_ok = (
        bool(expected)
        and not missing
        and not skipped
        and len(checks) >= len(expected)
        and all(item.get("ok") for item in checks)
    )
    return {
        "ok": bool(formal_ok if require_all_browsers else legacy_ok),
        "mode": "require_all_browsers" if require_all_browsers else "allow_dependency_skips",
        "requested": expected,
        "expected_check_count": len(expected),
        "observed_check_count": len(checks),
        "runnable_check_count": len(runnable),
        "missing": missing,
        "skipped": skipped,
        "failed": failed,
    }


def inspect_media_page(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const player = document.querySelector('#shared-player');
            const host = document.querySelector('#player-host');
            const msg = document.querySelector('#msg')?.textContent || '';
            const title = document.querySelector('#title')?.textContent || '';
            const formHidden = document.querySelector('#share-password-form')?.classList.contains('hidden');
            const rect = player ? player.getBoundingClientRect() : null;
            return {
                title,
                msg,
                formHidden,
                hostHidden: host ? host.classList.contains('hidden') : true,
                playerTag: player ? player.tagName.toLowerCase() : '',
                playerSrc: player ? (player.currentSrc || player.src || '') : '',
                networkState: player ? player.networkState : null,
                readyState: player ? player.readyState : null,
                width: rect ? Math.round(rect.width) : 0,
                height: rect ? Math.round(rect.height) : 0,
                hlsScriptLoaded: !!document.querySelector('script[data-shared-hls-js="1"]'),
                hlsGlobal: typeof window.Hls === 'function',
            };
        }"""
    )


def inspect_standard_controls(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const service = document.querySelector('#shared-service-mode-select');
            const audio = document.querySelector('#audio-track-select');
            const player = document.querySelector('#shared-player');
            const serviceOptions = service ? Array.from(service.options).map(option => ({
                value: option.value,
                text: option.textContent || '',
                disabled: option.disabled,
                selected: option.selected,
            })) : [];
            const audioOptions = audio ? Array.from(audio.options).map(option => ({
                value: option.value,
                text: option.textContent || '',
                selected: option.selected,
            })) : [];
            return {
                hasServiceSelect: !!service,
                selectedService: service ? service.value : '',
                serviceOptions,
                hasRealtimeOption: serviceOptions.some(option => option.value === 'realtime_proxy' && !option.disabled),
                hasAudioSelect: !!audio,
                audioOptions,
                audioOptionCount: audioOptions.length,
                selectedAudio: audio ? audio.value : '',
                playerSrc: player ? (player.currentSrc || player.src || '') : '',
            };
        }"""
    )


def realtime_proxy_url_from_playback(page, explicit_share_session: str = "") -> dict[str, Any]:
    return page.evaluate(
        """async explicitShareSession => {
            const token = JSON.parse(document.querySelector('#share-token')?.textContent || '""');
            const audio = document.querySelector('#audio-track-select')?.value || '';
            const suffix = explicitShareSession ? `?share_session=${encodeURIComponent(explicitShareSession)}` : '';
            const res = await fetch(`/api/videos/shared/${encodeURIComponent(token)}/playback${suffix}`, {credentials: 'same-origin'});
            const text = await res.text();
            let body = null;
            try { body = JSON.parse(text); } catch (err) { body = {raw: text.slice(0, 300)}; }
            const raw = String(body?.realtime_proxy_url || body?.realtime_proxy?.url || '').trim();
            if (!raw) {
                return {ok: false, status: res.status, body, audio, reason: 'realtime_proxy_url_missing'};
            }
            const url = new URL(raw, window.location.origin);
            if (explicitShareSession && !url.searchParams.has('share_session')) {
                url.searchParams.set('share_session', explicitShareSession);
            }
            if (audio) url.searchParams.set('audio', audio);
            return {
                ok: res.ok,
                status: res.status,
                body,
                audio,
                url: `${url.pathname}${url.search}${url.hash}`,
            };
        }""",
        explicit_share_session,
    )


def fetch_realtime_probe(page, player_src: str) -> dict[str, Any]:
    if not player_src:
        return {"ok": False, "reason": "empty_player_src"}
    return page.evaluate(
        """async url => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 7000);
            try {
                const res = await fetch(url, {credentials: 'same-origin', signal: controller.signal});
                let firstChunkBytes = 0;
                if (res.body && typeof res.body.getReader === 'function') {
                    const reader = res.body.getReader();
                    const first = await reader.read();
                    firstChunkBytes = first && first.value ? first.value.byteLength : 0;
                    try { await reader.cancel(); } catch (err) {}
                } else {
                    const blob = await res.blob();
                    firstChunkBytes = blob.size || 0;
                }
                return {
                    ok: res.ok,
                    status: res.status,
                    contentType: res.headers.get('content-type') || '',
                    firstChunkBytes,
                };
            } catch (err) {
                return {ok: false, status: 0, error: String(err && err.message || err)};
            } finally {
                clearTimeout(timer);
                controller.abort();
            }
        }""",
        player_src,
    )


def switch_to_standard_proxy(page, share_session_id: str = "") -> dict[str, Any]:
    before = inspect_standard_controls(page)
    if not before.get("hasRealtimeOption"):
        return {"ok": False, "before": before, "reason": "realtime_proxy_option_missing"}
    if before.get("selectedService") != "realtime_proxy":
        try:
            with page.expect_response(lambda response: "/api/videos/shared/" in response.url and "/playback" in response.url, timeout=30000):
                page.select_option("#shared-service-mode-select", "realtime_proxy")
        except PlaywrightTimeoutError:
            page.select_option("#shared-service-mode-select", "realtime_proxy")
    page.wait_for_function(
        """() => {
            const service = document.querySelector('#shared-service-mode-select');
            const player = document.querySelector('#shared-player');
            const src = player ? (player.currentSrc || player.src || '') : '';
            return service?.value === 'realtime_proxy' && (src.includes('/realtime-proxy') || src.startsWith('blob:'));
        }""",
        timeout=30000,
    )
    page.wait_for_timeout(500)
    after = inspect_standard_controls(page)
    descriptor = realtime_proxy_url_from_playback(page, share_session_id)
    audio_switched = False
    if int(after.get("audioOptionCount") or 0) >= 2:
        page.select_option("#audio-track-select", str(int(after["audioOptionCount"]) - 1))
        page.wait_for_function(
            """() => {
                const service = document.querySelector('#shared-service-mode-select');
                const audio = document.querySelector('#audio-track-select');
                const player = document.querySelector('#shared-player');
                const src = player ? (player.currentSrc || player.src || '') : '';
                return service?.value === 'realtime_proxy' && audio?.value && (src.includes('/realtime-proxy') || src.startsWith('blob:'));
            }""",
            timeout=30000,
        )
        page.wait_for_timeout(500)
        descriptor = realtime_proxy_url_from_playback(page, share_session_id)
        audio_switched = True
    final_state = inspect_standard_controls(page)
    proxy_fetch = fetch_realtime_probe(page, descriptor.get("url") or final_state.get("playerSrc") or "")
    ok = (
        final_state.get("selectedService") == "realtime_proxy"
        and (
            "/realtime-proxy" in str(final_state.get("playerSrc") or "")
            or str(final_state.get("playerSrc") or "").startswith("blob:")
        )
        and descriptor.get("ok") is True
        and "/realtime-proxy" in str(descriptor.get("url") or "")
        and proxy_fetch.get("status") == 200
        and "video/mp4" in str(proxy_fetch.get("contentType") or "")
        and int(proxy_fetch.get("firstChunkBytes") or 0) > 0
        and (int(final_state.get("audioOptionCount") or 0) < 2 or audio_switched)
    )
    return {
        "ok": bool(ok),
        "before": before,
        "after": after,
        "final": final_state,
        "descriptor": descriptor,
        "audio_switched": audio_switched,
        "proxy_fetch": proxy_fetch,
    }


def exercise_subtitle_shift(page, playback_body: dict[str, Any]) -> dict[str, Any]:
    """Exercise the real shared-video subtitle controls and reset their state."""

    subtitles = playback_body.get("subtitles")
    if not isinstance(subtitles, list):
        status = playback_body.get("status")
        subtitles = status.get("subtitles") if isinstance(status, dict) else []
    expected = len(subtitles or [])
    before = page.evaluate(
        """() => ({
            inputPresent: !!document.querySelector('#subtitle-shift-seconds'),
            trackCount: document.querySelectorAll('track[data-shared-subtitle="1"]').length,
            trackSources: Array.from(document.querySelectorAll('track[data-shared-subtitle="1"]')).map(track => track.src || track.getAttribute('src') || ''),
        })"""
    )
    if expected <= 0:
        return {
            "ok": False,
            "expected_subtitle_count": expected,
            "before": before,
            "reason": "playback_subtitles_missing",
            "reset": False,
        }
    if not before.get("inputPresent") or int(before.get("trackCount") or 0) < expected:
        return {
            "ok": False,
            "expected_subtitle_count": expected,
            "before": before,
            "reason": "subtitle_controls_or_tracks_missing",
            "reset": False,
        }
    page.locator("#subtitle-shift-seconds").fill("0.5")
    page.locator("#subtitle-shift-seconds").dispatch_event("change")
    page.wait_for_function(
        """() => {
            const tracks = Array.from(document.querySelectorAll('track[data-shared-subtitle="1"]'));
            return tracks.length > 0 && tracks.every(track => {
                const src = track.src || track.getAttribute('src') || '';
                return new URL(src, window.location.origin).searchParams.get('shift_ms') === '500';
            });
        }""",
        timeout=15000,
    )
    shifted = page.evaluate(
        """() => ({
            inputValue: document.querySelector('#subtitle-shift-seconds')?.value || '',
            trackSources: Array.from(document.querySelectorAll('track[data-shared-subtitle="1"]')).map(track => track.src || track.getAttribute('src') || ''),
        })"""
    )
    page.locator("[data-subtitle-shift-reset]").click()
    page.wait_for_function(
        """() => {
            const input = document.querySelector('#subtitle-shift-seconds');
            const tracks = Array.from(document.querySelectorAll('track[data-shared-subtitle="1"]'));
            return input?.value === '0' && tracks.length > 0 && tracks.every(track => {
                const src = track.src || track.getAttribute('src') || '';
                return !new URL(src, window.location.origin).searchParams.has('shift_ms');
            });
        }""",
        timeout=15000,
    )
    reset = page.evaluate(
        """() => ({
            inputValue: document.querySelector('#subtitle-shift-seconds')?.value || '',
            trackSources: Array.from(document.querySelectorAll('track[data-shared-subtitle="1"]')).map(track => track.src || track.getAttribute('src') || ''),
        })"""
    )
    return {
        "ok": True,
        "expected_subtitle_count": expected,
        "before": before,
        "shifted": shifted,
        "reset_state": reset,
        "reset": True,
    }


def check_shared_video_browser(browser_type, *, browser_name: str, base_url: str, share_url: str, headed: bool, mobile: bool) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    viewport = {"width": 390, "height": 844} if mobile else {"width": 1366, "height": 768}
    browser = None
    context = None
    label = browser_name + ("_mobile" if mobile else "_desktop")
    result: dict[str, Any] = {"browser": browser_name, "viewport": "mobile" if mobile else "desktop", "ok": False, "errors": errors}
    try:
        browser = browser_type.launch(headless=not headed)
        context = browser.new_context(ignore_https_errors=True, viewport=viewport)
        page = context.new_page()
        attach_error_handlers(page, errors, label)
        target = base_url + share_url if share_url.startswith("/") else share_url
        page.goto(target, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("#share-password-form:not(.hidden), #player-host:not(.hidden)", timeout=15000)
        share_session_id = ""
        if page.locator("#share-password-form:not(.hidden)").count():
            page.fill("#share-password", SHARE_PASSWORD)
            with page.expect_response(
                lambda response: f"/api/videos/shared/" in response.url
                and response.url.endswith("/unlock")
                and response.request.method == "POST",
                timeout=30000,
            ) as unlock_info:
                page.locator("#share-password-form button[type=submit]").click()
            unlock_json = unlock_info.value.json()
            share_session_id = str(unlock_json.get("share_session_id") or "")
        page.wait_for_selector("#player-host:not(.hidden) #shared-player", timeout=30000)
        page.wait_for_timeout(1400)
        media = inspect_media_page(page)
        playback = page.evaluate(
            """async explicitShareSession => {
                const token = JSON.parse(document.querySelector('#share-token')?.textContent || '""');
                const player = document.querySelector('#shared-player');
                const playerSrc = player ? (player.currentSrc || player.src || '') : '';
                let shareSession = explicitShareSession || '';
                try {
                    shareSession = shareSession || new URL(playerSrc, window.location.origin).searchParams.get('share_session') || '';
                } catch (err) {}
                const suffix = shareSession ? `?share_session=${encodeURIComponent(shareSession)}` : '';
                const res = await fetch(`/api/videos/shared/${encodeURIComponent(token)}/playback${suffix}`, {credentials: 'same-origin'});
                const text = await res.text();
                let body = null;
                try { body = JSON.parse(text); } catch (err) { body = {raw: text.slice(0, 300)}; }
                return {status: res.status, ok: res.ok, body};
            }""",
            share_session_id,
        )
        master = {"status": 0, "text": ""}
        master_url = str((playback.get("body") or {}).get("master_url") or "")
        if master_url:
            master = page.evaluate(
                """async url => {
                    const res = await fetch(url, {credentials: 'same-origin'});
                    const text = await res.text();
                    return {status: res.status, ok: res.ok, text: text.slice(0, 1000), contentType: res.headers.get('content-type') || ''};
                }""",
                master_url,
            )
        subtitle_shift = exercise_subtitle_shift(page, playback.get("body") or {})
        standard = switch_to_standard_proxy(page, share_session_id)
        fatal_errors = [
            item
            for item in errors
            if item["type"] == "pageerror" or ("Failed to load resource: the server responded with a status of 5" in item["text"])
        ]
        ok = (
            playback["status"] == 200
            and (playback.get("body") or {}).get("mode") in {"hls", "direct", "e2ee_stream_v2", "e2ee_direct"}
            and media["playerTag"] in {"video", "audio"}
            and media["width"] > 0
            and media["height"] > 0
            and media["hostHidden"] is False
            and standard.get("ok") is True
            and subtitle_shift.get("ok") is True
            and subtitle_shift.get("reset") is True
            and not fatal_errors
            and (not master_url or (master.get("status") == 200 and "#EXTM3U" in master.get("text", "")))
        )
        result.update({"ok": bool(ok), "media": media, "playback": playback, "master": master, "standard": standard, "subtitle_shift": subtitle_shift, "fatal_errors": fatal_errors})
        return result
    except Exception as exc:
        exception_text = f"{type(exc).__name__}: {exc}"
        skipped = browser_dependency_error(exception_text)
        result.update(
            {
                "ok": False,
                "skipped": skipped,
                "skip_reason": "browser_dependency_missing" if skipped else "",
                "exception": exception_text,
                "traceback": traceback.format_exc(),
            }
        )
        return result
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def write_outputs(runtime_root: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    out_dir = runtime_root / "reports" / "qa"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "browser_video_compat.json"
    md_path = out_dir / "browser_video_compat.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Browser Video Compatibility",
        "",
        f"- Base URL: `{payload.get('base_url')}`",
        f"- Runtime root: `{payload.get('runtime_root')}`",
        f"- Video ID: `{(payload.get('fixture') or {}).get('video_id')}`",
        f"- Require all browsers: `{payload.get('require_all_browsers')}`",
        f"- OK: `{payload.get('ok')}`",
        "",
        "## Browsers",
        "",
    ]
    for item in payload.get("checks") or []:
        mark = "SKIP" if item.get("skipped") else ("PASS" if item.get("ok") else "FAIL")
        media = item.get("media") or {}
        playback = (item.get("playback") or {}).get("body") or {}
        standard = item.get("standard") or {}
        lines.append(
            f"- `{mark}` {item.get('browser')} {item.get('viewport')}: "
            f"mode={playback.get('mode')}, player={media.get('playerTag')}, "
            f"size={media.get('width')}x{media.get('height')}, standard={standard.get('ok')}, "
            f"msg={str(media.get('msg') or '')[:100]}"
        )
        if item.get("exception"):
            lines.append(f"  - exception: `{item.get('exception')}`")
        if item.get("skipped"):
            lines.append(f"  - skip_reason: `{item.get('skip_reason')}`")
        if item.get("fatal_errors"):
            lines.append(f"  - fatal errors: `{len(item.get('fatal_errors') or [])}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--keep-server", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--browsers", default="chromium,firefox,webkit")
    parser.add_argument("--skip-mobile", action="store_true")
    parser.add_argument(
        "--require-all-browsers",
        action="store_true",
        help="Fail when any requested browser/viewport check is skipped, missing, or unsuccessful.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    stamp = utc_stamp()
    runtime_root = Path(args.runtime_root).resolve() if args.runtime_root else Path("/tmp") / f"hackme_web_browser_video_{stamp}"
    mkdirs(runtime_root)
    port = free_port()
    browser_names = [item.strip().lower() for item in args.browsers.split(",") if item.strip()]
    os.environ.setdefault("HACKME_MEDIA_REALTIME_PROXY_ENABLED", "1")
    os.environ.setdefault("HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT", "2")
    server = start_server(runtime_root, port)
    base_url = ""
    payload: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runtime_root": str(runtime_root),
        "requested_browsers": browser_names,
        "include_mobile": not args.skip_mobile,
        "require_all_browsers": bool(args.require_all_browsers),
        "checks": [],
    }
    try:
        base_url = wait_for_server(port)
        payload["base_url"] = base_url
        with sync_playwright() as p:
            setup_browser = p.chromium.launch(headless=not args.headed)
            setup_context = setup_browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 768})
            setup_page = setup_context.new_page()
            fixture = publish_video_fixture(setup_page, base_url, runtime_root)
            setup_context.close()
            setup_browser.close()
            payload["fixture"] = fixture
            share_url = str(fixture.get("share_url") or "")
            if not share_url:
                raise RuntimeError(f"fixture did not produce share_url: {fixture}")
            for name in browser_names:
                btype = browser_kind(p, name)
                for mobile in ([False] if args.skip_mobile else [False, True]):
                    result = check_shared_video_browser(
                        btype,
                        browser_name=name,
                        base_url=base_url,
                        share_url=share_url,
                        headed=args.headed,
                        mobile=mobile,
                    )
                    payload["checks"].append(result)
                    mark = "SKIP" if result.get("skipped") else ("PASS" if result.get("ok") else "FAIL")
                    print(f"[{mark}] {name} {'mobile' if mobile else 'desktop'}", flush=True)
    finally:
        payload["finished_at"] = datetime.now(timezone.utc).isoformat()
        coverage = evaluate_browser_coverage(
            payload.get("checks", []),
            requested_browsers=browser_names,
            include_mobile=not args.skip_mobile,
            require_all_browsers=bool(args.require_all_browsers),
        )
        payload["browser_coverage"] = coverage
        was_running = server.poll() is None
        terminate_attempted = bool(was_running and not args.keep_server)
        if server.poll() is None and not args.keep_server:
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        payload["cleanup"] = {
            "server_was_running": was_running,
            "terminate_attempted": terminate_attempted,
            "server_stopped": server.poll() is not None,
            "keep_server_requested": bool(args.keep_server),
        }
        payload["ok"] = bool(coverage["ok"] and (args.keep_server or payload["cleanup"]["server_stopped"]))
        json_path, md_path = write_outputs(runtime_root, payload)
        print(json.dumps({"ok": payload["ok"], "json": str(json_path), "md": str(md_path), "base_url": base_url}, ensure_ascii=False), flush=True)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
