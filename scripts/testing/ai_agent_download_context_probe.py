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


def send_case(page, text: str, *, wait_for_write: bool, fake_writes: list[dict[str, Any]]) -> dict[str, Any]:
    before_messages = message_count(page)
    before_writes = len(fake_writes)
    started = time.perf_counter()
    page.fill("#ai-agent-input", text)
    page.click("#ai-agent-send-btn")
    try:
        if wait_for_write:
            page.wait_for_function(
                "count => window.__AI_AGENT_FAKE_WRITES__ && window.__AI_AGENT_FAKE_WRITES__.length > count",
                arg=before_writes,
                timeout=120_000,
            )
        else:
            page.wait_for_function(
                "count => document.querySelectorAll('#ai-agent-thread .ai-agent-message').length > count",
                arg=before_messages,
                timeout=120_000,
            )
            page.wait_for_timeout(2_000)
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
    parser.add_argument("--root-password", default="root")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/hackme_ai_agent_download_context_{stamp}").resolve()
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
                    "result": {
                        "ok": True,
                        "task_id": f"fake-download-context-{len(fake_writes)}",
                        "status": "queued",
                    },
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
                "direct_download",
                "請用 Direct download 下載 https://example.com/releases/audit-test-video.mp4 到我的雲端硬碟，檔名 audit-test-video.mp4",
                True,
            ),
            (
                "bt_download",
                "請建立 BT/magnet download 任務：magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=audit-test.iso",
                True,
            ),
            (
                "download_status",
                "查一下目前下載器和遠端下載任務進度，不要新增下載",
                False,
            ),
        ]
        for case_id, text, wait_for_write in cases:
            before_chat = len(chat_responses)
            before_writes = len(fake_writes)
            result = send_case(page, text, wait_for_write=wait_for_write, fake_writes=fake_writes)
            result["case_id"] = case_id
            result["chat_responses"] = chat_responses[before_chat:]
            result["new_writes"] = fake_writes[before_writes:]
            records.append(result)

        screenshot_path = out_dir / "ai_agent_download_context.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        browser.close()

    direct_writes = next((r.get("new_writes") or [] for r in records if r["case_id"] == "direct_download"), [])
    bt_writes = next((r.get("new_writes") or [] for r in records if r["case_id"] == "bt_download"), [])
    status_record = next((r for r in records if r["case_id"] == "download_status"), {})
    checks = {
        "direct_download_wrote_direct_tool": any(w.get("tool") == "write_remote_download_direct" for w in direct_writes),
        "bt_download_wrote_bt_tool": any(w.get("tool") == "write_remote_download_bt" for w in bt_writes),
        "status_did_not_write": status_record.get("writes_delta") == 0,
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
        "# AI Agent Download Context Probe\n\n"
        + "\n".join(f"- {key}: {'PASS' if value else 'FAIL'}" for key, value in checks.items())
        + f"\n\nJSON: `{json_path}`\nScreenshot: `{screenshot_path}`\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": report["ok"], "out_dir": str(out_dir), "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

