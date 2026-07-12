#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def api_fetch(page, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return page.evaluate(
        """async ({method, path, body}) => {
          const csrf = (document.cookie.match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || "";
          const headers = {"X-CSRF-Token": decodeURIComponent(csrf)};
          const opts = {method, credentials: "same-origin", headers};
          if (body !== null && body !== undefined) {
            headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
          }
          const res = await fetch(path, opts);
          const text = await res.text();
          let parsed = {};
          try { parsed = text ? JSON.parse(text) : {}; } catch (e) { parsed = {raw: text}; }
          return {status: res.status, ok: res.ok, body: parsed, text};
        }""",
        {"method": method, "path": path, "body": body},
    )


def login(page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.evaluate("() => fetch('/api/csrf-token', {credentials: 'same-origin'}).catch(() => null)")
    result = api_fetch(page, "POST", "/api/login", {"username": username, "password": password})
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"login failed: {result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")


def open_ai_agent(page, base_url: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.locator("#tab-module-ai-agent").wait_for(state="visible", timeout=20_000)
    page.click("#tab-module-ai-agent")
    page.locator("#module-ai-agent.active").wait_for(state="visible", timeout=15_000)
    page.locator("#ai-agent-input").wait_for(state="visible", timeout=15_000)


def thread_text(page) -> str:
    try:
        return page.locator("#ai-agent-thread").inner_text(timeout=5_000)
    except Exception:
        return ""


def message_count(page) -> int:
    try:
        return int(page.locator("#ai-agent-thread .ai-agent-message").count())
    except Exception:
        return 0


def send_case(page, text: str, fake_writes: list[dict[str, Any]]) -> dict[str, Any]:
    before_messages = message_count(page)
    before_writes = len(fake_writes)
    started = time.perf_counter()
    page.fill("#ai-agent-input", text)
    page.click("#ai-agent-send-btn")
    try:
      page.wait_for_function(
          "count => window.__AI_AGENT_FAKE_WRITES__ && window.__AI_AGENT_FAKE_WRITES__.length > count",
          arg=before_writes,
          timeout=120_000,
      )
    except PlaywrightTimeoutError:
      pass
    return {
        "text": text,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "messages_delta": max(0, message_count(page) - before_messages),
        "writes_delta": max(0, len(fake_writes) - before_writes),
        "thread_tail": thread_text(page)[-1800:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/hackme_ai_agent_cloud_media_context_{stamp}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    fake_writes: list[dict[str, Any]] = []
    browser_errors: list[str] = []
    chat_responses: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        page.on("console", lambda msg: browser_errors.append(msg.text) if msg.type == "error" else None)

        def write_tool_handler(route, request):
            try:
                payload = request.post_data_json or {}
            except Exception:
                payload = {}
            fake_writes.append(payload)
            page.evaluate("payload => { window.__AI_AGENT_FAKE_WRITES__ = window.__AI_AGENT_FAKE_WRITES__ || []; window.__AI_AGENT_FAKE_WRITES__.push(payload); }", payload)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "ok": True,
                    "tool": payload.get("tool") or "",
                    "status": 200,
                    "result": {"ok": True, "id": f"fake-cloud-media-{len(fake_writes)}", "status": "queued"},
                }, ensure_ascii=False),
            )

        def on_response(response):
            if "/api/ai-agent/chat" not in response.url:
                return
            try:
                req_payload = response.request.post_data_json or {}
                res_payload = response.json()
            except Exception as exc:
                chat_responses.append({"url": response.url, "error": str(exc)})
                return
            content = str((res_payload.get("message") or {}).get("content") or res_payload.get("msg") or "")
            prompt = ""
            messages = req_payload.get("messages") if isinstance(req_payload, dict) else []
            if messages and isinstance(messages[0], dict):
                prompt = str(messages[0].get("content") or "")
            chat_responses.append({
                "kind": "planner" if "工具路由器" in prompt and "context=" in prompt else "chat",
                "status": response.status,
                "ok": bool(res_payload.get("ok")),
                "content_preview": content[:1600],
            })

        page.route("**/api/ai-agent/write-tools/execute", write_tool_handler)
        page.on("response", on_response)

        login(page, args.base_url.rstrip("/"), "root", args.root_password)
        open_ai_agent(page, args.base_url.rstrip("/"))
        status = api_fetch(page, "GET", "/api/ai-agent/status")

        cases = [
            (
                "cloud_text",
                "請在我的雲端硬碟建立文字檔 audit-agent-note.txt，內容是：AI agent cloud drive audit 2026-06-23",
            ),
            (
                "album_create",
                "請建立相簿，名稱 Agent Audit Album，描述是給 AI agent 前台測試用，visibility private",
            ),
            (
                "album_add_file",
                "請把雲端檔案 file_id=file-audit-1 加入相簿 album_id=album-audit-1，caption 寫 AI audit sample",
            ),
            (
                "transcode_hls",
                "請對雲端檔案 file_id=file-video-audit-1 排程 HLS 轉檔，不要用 video_id",
            ),
        ]
        for case_id, text in cases:
            before_chat = len(chat_responses)
            before_writes = len(fake_writes)
            result = send_case(page, text, fake_writes)
            result["case_id"] = case_id
            result["chat_responses"] = chat_responses[before_chat:]
            result["new_writes"] = fake_writes[before_writes:]
            records.append(result)

        screenshot_path = out_dir / "ai_agent_cloud_media_context.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        browser.close()

    by_case = {record["case_id"]: record for record in records}
    cloud_args = ((by_case.get("cloud_text", {}).get("new_writes") or [{}])[0]).get("arguments") or {}
    album_args = ((by_case.get("album_create", {}).get("new_writes") or [{}])[0]).get("arguments") or {}
    album_add_args = ((by_case.get("album_add_file", {}).get("new_writes") or [{}])[0]).get("arguments") or {}
    transcode_args = ((by_case.get("transcode_hls", {}).get("new_writes") or [{}])[0]).get("arguments") or {}
    checks = {
        "cloud_text_tool": any(w.get("tool") == "write_cloud_drive_create_text" for w in by_case.get("cloud_text", {}).get("new_writes") or []),
        "cloud_text_canonical_args": bool(cloud_args.get("filename") and cloud_args.get("content")),
        "album_create_tool": any(w.get("tool") == "write_album_create" for w in by_case.get("album_create", {}).get("new_writes") or []),
        "album_create_uses_title": bool(album_args.get("title")) and "name" not in album_args,
        "album_add_file_tool": any(w.get("tool") == "write_album_add_file" for w in by_case.get("album_add_file", {}).get("new_writes") or []),
        "album_add_file_has_file_ref": bool(album_add_args.get("file_id") or album_add_args.get("storage_file_id")),
        "transcode_tool": any(w.get("tool") == "write_transcode_hls" for w in by_case.get("transcode_hls", {}).get("new_writes") or []),
        "transcode_uses_file_id": bool(transcode_args.get("file_id")) and "video_id" not in transcode_args,
        "no_browser_errors": not browser_errors,
    }
    report = {
        "ok": all(checks.values()),
        "checks": checks,
        "base_url": args.base_url,
        "status": status,
        "records": records,
        "fake_writes": fake_writes,
        "browser_errors": browser_errors,
        "artifacts": {"screenshot": str(screenshot_path)},
    }
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / "report.md"
    md_path.write_text(
        "# AI Agent Cloud Media Context Probe\n\n"
        + "\n".join(f"- {key}: {'PASS' if value else 'FAIL'}" for key, value in checks.items())
        + f"\n\nJSON: `{json_path}`\nScreenshot: `{screenshot_path}`\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": report["ok"], "out_dir": str(out_dir), "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
