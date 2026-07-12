#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.probe_credentials import add_root_password_argument, add_user_password_argument  # noqa: E402


TOOL_CANDIDATES = [
    "write_remote_download_direct",
    "write_remote_download_bt",
    "write_remote_download_pause",
    "write_remote_download_resume",
    "write_remote_download_cancel",
    "write_album_create",
    "write_album_add_file",
    "write_album_update",
    "write_video_upload",
    "write_video_publish",
    "write_transcode_hls",
    "write_hls_rebuild",
    "write_subtitle_upload",
]


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def add(
        self,
        case_id: str,
        item: str,
        ok: bool,
        result: str,
        evidence: dict[str, Any] | None = None,
        *,
        expected_gap: bool = False,
    ) -> None:
        row = {
            "case_id": case_id,
            "item": item,
            "ok": bool(ok),
            "expected_gap": bool(expected_gap),
            "result": result,
            "evidence": evidence or {},
        }
        self.rows.append(row)
        label = "GAP" if expected_gap and ok else "PASS" if ok else "FAIL"
        print(f"[{label}] {case_id} {item}: {result}", flush=True)
        if not ok and not expected_gap:
            self.failures.append(row)


def api_fetch(page, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return page.evaluate(
        """async ({method, path, body}) => {
            const cookie = document.cookie || "";
            const csrf = (cookie.match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || "";
            const headers = {"X-CSRF-Token": decodeURIComponent(csrf)};
            const opts = {method, credentials: "same-origin", headers};
            if (body !== null && body !== undefined) {
              headers["Content-Type"] = "application/json";
              opts.body = JSON.stringify(body);
            }
            const res = await fetch(path, opts);
            const text = await res.text();
            let json = {};
            try { json = text ? JSON.parse(text) : {}; } catch (e) { json = {raw: text}; }
            return {status: res.status, ok: res.ok, body: json, text};
        }""",
        {"method": method, "path": path, "body": body},
    )


def api_multipart_upload_video(page, fixture_path: str, title: str) -> dict[str, Any]:
    return page.evaluate(
        """async ({fixturePath, title}) => {
            const cookie = document.cookie || "";
            const csrf = (cookie.match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || "";
            const payload = await window.__readFixtureAsBase64(fixturePath);
            const bytes = Uint8Array.from(atob(payload), c => c.charCodeAt(0));
            const file = new File([bytes], "ai-agent-media-probe.mp4", {type: "video/mp4"});
            const form = new FormData();
            form.append("video", file);
            form.append("title", title);
            form.append("description", "AI Agent media/downloader probe");
            form.append("visibility", "unlisted");
            form.append("privacy_mode", "standard_plain");
            form.append("streaming_modes", "prepared_hls,realtime_proxy");
            const res = await fetch("/api/videos/upload", {
              method: "POST",
              credentials: "same-origin",
              headers: {"X-CSRF-Token": decodeURIComponent(csrf)},
              body: form,
            });
            const text = await res.text();
            let json = {};
            try { json = text ? JSON.parse(text) : {}; } catch (e) { json = {raw: text}; }
            return {status: res.status, ok: res.ok, body: json, text};
        }""",
        {"fixturePath": fixture_path, "title": title},
    )


def login(page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    result = api_fetch(page, "POST", "/api/login", {"username": username, "password": password})
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"login failed for {username}: {result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")


def execute_ai_tool(page, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return api_fetch(
        page,
        "POST",
        "/api/ai-agent/write-tools/execute",
        {"tool": tool, "arguments": arguments or {}, "confirm": "EXECUTE"},
    )


def install_media_planner_mock(page) -> dict[str, Any]:
    state = {"planner_calls": 0, "chat_calls": 0, "plans": []}

    def handler(route, request):
        try:
            payload = request.post_data_json or {}
        except Exception:
            payload = {}
        messages = payload.get("messages") if isinstance(payload, dict) else []
        content = messages[0].get("content") if messages and isinstance(messages[0], dict) else ""
        text = str(content or "")
        is_planner = "工具路由器" in text and "context=" in text
        if is_planner:
            user_text = text.split("\nuser=", 1)[-1].strip()
            plan = {
                "action": "media_download_album_transcode",
                "confidence": 0.97,
                "execute_write": True,
                "reason": "downloader, album and HLS transcode requested",
            }
            state["planner_calls"] += 1
            state["plans"].append({"user": user_text[:240], "plan": plan})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}}),
            )
            return
        state["chat_calls"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": True,
                    "message": {
                        "role": "assistant",
                        "content": "目前 AI Agent 尚未接上 BT/Direct downloader、相簿 CRUD、影片上傳或 HLS 轉檔工具；不能假裝已建立下載任務、相簿或轉檔工作。",
                    },
                },
                ensure_ascii=False,
            ),
        )

    page.route("**/api/ai-agent/chat", handler)
    return state


def open_ai_agent(page, base_url: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.locator("#tab-module-ai-agent").wait_for(state="visible", timeout=20000)
    page.click("#tab-module-ai-agent")
    page.locator("#module-ai-agent.active").wait_for(state="visible", timeout=15000)
    page.locator("#ai-agent-input").wait_for(state="visible", timeout=15000)


def send_ai_text(page, text: str) -> None:
    page.fill("#ai-agent-input", text)
    page.click("#ai-agent-send-btn")


def wait_thread_any(page, needles: list[str], timeout: int = 20000) -> str:
    page.locator("#ai-agent-thread").wait_for(state="visible", timeout=timeout)
    page.wait_for_function(
        """needles => {
          const text = document.querySelector('#ai-agent-thread')?.innerText || '';
          return needles.some((needle) => text.includes(needle));
        }""",
        arg=needles,
        timeout=timeout,
    )
    return page.locator("#ai-agent-thread").inner_text(timeout=10000)


def thread_messages(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#ai-agent-thread .ai-agent-message'))
          .map((el) => (el.innerText || '').trim())
          .filter(Boolean)"""
    )


def anomaly_metrics(messages: list[str]) -> dict[str, int]:
    repeated_adjacent = 0
    seen: dict[str, int] = {}
    empty = 0
    repeated_total = 0
    for idx, message in enumerate(messages):
        normalized = re.sub(r"\s+", " ", message).strip()
        if not normalized:
            empty += 1
        if idx > 0 and normalized == re.sub(r"\s+", " ", messages[idx - 1]).strip():
            repeated_adjacent += 1
        seen[normalized] = seen.get(normalized, 0) + 1
        if seen[normalized] == 2:
            repeated_total += 1
    return {"empty": empty, "repeated_adjacent": repeated_adjacent, "repeated_total": repeated_total, "message_count": len(messages)}


def create_fixture_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x90:rate=12",
        "-t",
        "1",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-y",
        str(path),
    ]
    subprocess.run(cmd, check=True)


def install_fixture_reader(page) -> None:
    page.expose_function("__readFixtureAsBase64", lambda fixture_path: Path(fixture_path).read_bytes().hex())
    page.evaluate(
        """() => {
          const original = window.__readFixtureAsBase64;
          window.__readFixtureAsBase64 = async (fixturePath) => {
            const hex = await original(fixturePath);
            let binary = "";
            for (let i = 0; i < hex.length; i += 2) {
              binary += String.fromCharCode(parseInt(hex.slice(i, i + 2), 16));
            }
            return btoa(binary);
          };
        }"""
    )


def poll_jobs(page, seconds: float = 8.0) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        jobs = api_fetch(page, "GET", "/api/jobs")
        body = jobs.get("body") or {}
        items = body.get("jobs") or body.get("items") or body.get("tasks") or []
        snapshots.append({"status": jobs["status"], "count": len(items), "items": items[:5]})
        if any("hls" in json.dumps(item, ensure_ascii=False).lower() or "video" in json.dumps(item, ensure_ascii=False).lower() for item in items):
            break
        time.sleep(1)
    return snapshots


def record_request(request_log: list[dict[str, Any]], request) -> None:
    request_log.append({"url": request.url, "method": request.method})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    add_root_password_argument(parser)
    add_user_password_argument(parser)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fixture", default="/tmp/hackme_ai_agent_media_probe/ai_agent_media_probe.mp4")
    args = parser.parse_args()

    rec = Recorder()
    run_id = str(int(time.time()))[-8:]
    fixture = Path(args.fixture)
    create_fixture_mp4(fixture)
    request_log: list[dict[str, Any]] = []
    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        root_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        root_page = root_ctx.new_page()
        root_page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        root_page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        root_page.on("request", lambda request: record_request(request_log, request))
        planner_state = install_media_planner_mock(root_page)
        login(root_page, args.base_url, "root", args.root_password)
        install_fixture_reader(root_page)

        api_fetch(
            root_page,
            "PUT",
            "/api/admin/settings",
            {
                "feature_ai_agent_enabled": True,
                "module_ai_agent_min_role": "user",
                "ai_agent_provider": "openai_compatible",
                "ai_agent_api_base_url": "http://127.0.0.1:9",
                "ai_agent_api_key": "sk-media-probe",
                "ai_agent_model": "qa-router",
                "ai_agent_allowed_models": "qa-router",
                "ai_agent_allowed_tools": "",
                "ai_agent_operation_mode": "write",
            },
        )

        caps = api_fetch(root_page, "GET", "/api/cloud-drive/remote-download/capabilities")
        tasks = api_fetch(root_page, "GET", "/api/cloud-drive/remote-download/tasks")
        albums = api_fetch(root_page, "GET", "/api/storage/albums")
        videos = api_fetch(root_page, "GET", "/api/videos")
        video_manage = api_fetch(root_page, "GET", "/api/videos/manage")
        rec.add(
            "MDT-01",
            "下載器/相簿/影音 API 存在",
            all(item["status"] == 200 and item["body"].get("ok", True) for item in [caps, tasks, albums, videos, video_manage]),
            f"caps={caps['status']}, tasks={tasks['status']}, albums={albums['status']}, videos={videos['status']}, manage={video_manage['status']}",
            {
                "capabilities": caps["body"],
                "albums_count": len(albums["body"].get("albums") or []),
                "videos_keys": sorted(videos["body"].keys()),
                "manage_keys": sorted(video_manage["body"].keys()),
            },
        )

        local_direct = api_fetch(root_page, "POST", "/api/cloud-drive/remote-download/tasks", {"url": "file:///etc/passwd", "privacy_mode": "standard_plain"})
        magnet_as_direct = api_fetch(
            root_page,
            "POST",
            "/api/cloud-drive/remote-download/tasks",
            {"url": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=probe", "source_type": "direct", "privacy_mode": "standard_plain"},
        )
        http_as_bt = api_fetch(
            root_page,
            "POST",
            "/api/cloud-drive/remote-download/torrent-tasks",
            {"url": "https://example.com/not-a-torrent.bin", "privacy_mode": "standard_plain"},
        )
        rec.add(
            "MDT-02",
            "下載器安全邊界與模式驗證",
            local_direct["status"] == 400 and magnet_as_direct["status"] == 400 and http_as_bt["status"] == 400,
            f"local={local_direct['status']}, magnet_direct={magnet_as_direct['status']}, http_bt={http_as_bt['status']}",
            {"local": local_direct["body"], "magnet_as_direct": magnet_as_direct["body"], "http_as_bt": http_as_bt["body"]},
        )

        album = api_fetch(
            root_page,
            "POST",
            "/api/storage/albums",
            {"title": f"AI Agent Media Probe {run_id}", "description": "media/downloader probe", "visibility": "unlisted"},
        )
        album_id = ((album.get("body") or {}).get("album") or {}).get("id")
        album_detail = api_fetch(root_page, "GET", f"/api/storage/albums/{album_id}") if album_id else {"status": 0, "body": {}}
        album_update = (
            api_fetch(root_page, "PUT", f"/api/storage/albums/{album_id}", {"title": f"AI Agent Media Probe Updated {run_id}", "visibility": "public"})
            if album_id
            else {"status": 0, "body": {}}
        )
        smart = api_fetch(root_page, "POST", "/api/storage/albums/smart-organize", {"strategy": "folder", "visibility": "private"})
        rec.add(
            "MDT-03",
            "相簿 CRUD 與自動整理",
            album["status"] == 200 and album_id and album_detail["status"] == 200 and album_update["status"] == 200 and smart["status"] in {200, 400},
            f"create={album['status']}, detail={album_detail['status']}, update={album_update['status']}, smart={smart['status']}",
            {"album": album["body"], "detail_keys": sorted(album_detail["body"].keys()), "update": album_update["body"], "smart": smart["body"]},
        )

        upload = api_multipart_upload_video(root_page, str(fixture), f"AI Agent Media Probe {run_id}")
        video = (upload.get("body") or {}).get("video") or {}
        stream_asset = (upload.get("body") or {}).get("stream_asset") or {}
        video_id = video.get("id")
        job_snapshots = poll_jobs(root_page, seconds=10.0) if upload["status"] == 200 else []
        playback = api_fetch(root_page, "GET", f"/api/videos/{video_id}/playback") if video_id else {"status": 0, "body": {}}
        master = api_fetch(root_page, "GET", f"/api/videos/{video_id}/hls/master.m3u8") if video_id else {"status": 0, "body": {}}
        hls_seen = any(
            "hls" in json.dumps(snapshot, ensure_ascii=False).lower() or "video" in json.dumps(snapshot, ensure_ascii=False).lower()
            for snapshot in job_snapshots
        )
        rec.add(
            "MDT-04",
            "小型影片上傳與 HLS 背景排程",
            upload["status"] == 200 and video_id and hls_seen and playback["status"] in {200, 403, 404, 409} and master["status"] in {200, 403, 404, 409},
            f"upload={upload['status']}, video_id={video_id}, hls_seen={hls_seen}, playback={playback['status']}, master={master['status']}",
            {
                "fixture_bytes": fixture.stat().st_size,
                "video": video,
                "stream_asset": stream_asset,
                "job_snapshots": job_snapshots[-3:],
                "playback": playback["body"],
                "master_status": master["status"],
            },
        )

        tools = api_fetch(root_page, "GET", "/api/ai-agent/write-tools")
        unsupported = {tool: execute_ai_tool(root_page, tool, {"url": "https://example.com/file.bin", "album_id": album_id, "video_id": video_id}) for tool in TOOL_CANDIDATES}
        rec.add(
            "MDT-05",
            "AI Agent 未接下載/相簿/轉檔工具",
            tools["status"] == 200 and all(result["status"] == 400 and not result["body"].get("ok") for result in unsupported.values()),
            f"listed_tools={len(tools['body'].get('tools', []))}, rejected={sum(1 for result in unsupported.values() if result['status'] == 400)}/{len(unsupported)}",
            {"tools": tools["body"].get("tools"), "unsupported": {tool: {"status": result["status"], "body": result["body"]} for tool, result in unsupported.items()}},
            expected_gap=True,
        )

        open_ai_agent(root_page, args.base_url)
        watched_paths = (
            "/api/cloud-drive/remote-download",
            "/api/storage/albums",
            "/api/videos/upload",
            "/api/videos/",
            "/api/ai-agent/write-tools/execute",
        )
        before = len([r for r in request_log if any(path in r["url"] for path in watched_paths)])
        t0 = time.perf_counter()
        send_ai_text(root_page, "幫我用 Direct download 下載一個檔案，再用 BT 磁力下載，建立相簿，把影片轉成 HLS，完成後回報任務進度")
        thread = wait_thread_any(root_page, ["尚未接上 BT", "不能假裝", "HLS 轉檔"], timeout=25000)
        elapsed = round(time.perf_counter() - t0, 3)
        after = len([r for r in request_log if any(path in r["url"] for path in watched_paths)])
        rec.add(
            "MDT-06",
            "對話下載/相簿/轉檔要求不靜默成功",
            after == before and "不能假裝" in thread,
            f"response_s={elapsed}, media_requests={after - before}",
            {"thread_tail": thread[-900:], "planner": planner_state},
            expected_gap=True,
        )

        test_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        test_page = test_ctx.new_page()
        login(test_page, args.base_url, "test", args.test_password)
        member_tools = api_fetch(test_page, "GET", "/api/ai-agent/write-tools")
        member_ai = execute_ai_tool(test_page, "write_remote_download_direct", {"url": "https://example.com/file.bin"})
        member_caps = api_fetch(test_page, "GET", "/api/cloud-drive/remote-download/capabilities")
        member_albums = api_fetch(test_page, "GET", "/api/storage/albums")
        rec.add(
            "MDT-07",
            "會員權限與越權",
            member_tools["status"] == 403 and member_ai["status"] == 403 and member_caps["status"] == 200 and member_albums["status"] == 200,
            f"member_tools={member_tools['status']}, member_ai={member_ai['status']}, caps={member_caps['status']}, albums={member_albums['status']}",
            {"member_tools": member_tools["body"], "member_ai": member_ai["body"]},
        )
        test_ctx.close()

        anomaly = anomaly_metrics(thread_messages(root_page))
        rec.add(
            "MDT-08",
            "回應時間/跳針",
            elapsed < 25 and anomaly["empty"] == 0 and anomaly["repeated_adjacent"] == 0,
            f"response_s={elapsed}, empty={anomaly['empty']}, repeated_adjacent={anomaly['repeated_adjacent']}",
            {"anomaly": anomaly},
        )

        root_ctx.close()
        browser.close()

    filtered_console_errors = [
        item
        for item in console_errors
        if "favicon" not in item.lower()
        and "the server responded with a status of 400" not in item.lower()
        and "the server responded with a status of 403" not in item.lower()
        and "the server responded with a status of 404" not in item.lower()
        and "the server responded with a status of 409" not in item.lower()
    ]
    result = {
        "ok": not rec.failures,
        "rows": rec.rows,
        "failures": rec.failures,
        "expected_gaps": [row for row in rec.rows if row.get("expected_gap")],
        "console_errors": filtered_console_errors,
        "page_errors": page_errors,
        "fixture": str(fixture),
    }
    if filtered_console_errors or page_errors:
        result["ok"] = False
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
