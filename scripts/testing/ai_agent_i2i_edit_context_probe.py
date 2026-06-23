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


def seed_recent_image(page, source_ref: dict[str, Any]) -> None:
    page.evaluate(
        """sourceRef => {
          AI_AGENT_STATE.messages = [];
          AI_AGENT_STATE.lastComfyuiJob = {
            job_id: "probe-comfyui-image-edit-source",
            status: "completed",
            progress: {percent: 100},
            result: {images: [{filename: sourceRef.filename, image_ref: sourceRef, mime_type: "image/png"}]},
          };
          AI_AGENT_STATE.lastComfyuiArgs = {
            prompt: "portrait source for image edit probe",
            width: 1024,
            height: 1024,
            steps: 20,
            batch_size: 1,
            confirm_billing: true,
          };
          AI_AGENT_STATE.messages.push({
            role: "assistant",
            content: "ComfyUI 產圖完成。這是剛剛可重用的站內圖片。",
            images: [{
              image_ref: sourceRef,
              filename: sourceRef.filename,
              prompt_id: "probe-prompt-id",
              mime_type: "image/png",
            }],
          });
          renderAiAgentThread({skipPersist: true});
        }""",
        source_ref,
    )


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


def send_case(page, text: str, fake_writes: list[dict[str, Any]], *, expect_write: bool) -> dict[str, Any]:
    before_messages = message_count(page)
    before_writes = len(fake_writes)
    started = time.perf_counter()
    page.fill("#ai-agent-input", text)
    page.click("#ai-agent-send-btn")
    try:
        if expect_write:
            page.wait_for_function(
                "count => window.__AI_AGENT_FAKE_WRITES__ && window.__AI_AGENT_FAKE_WRITES__.length > count",
                arg=before_writes,
                timeout=180_000,
            )
        else:
            page.wait_for_function(
                "count => document.querySelectorAll('#ai-agent-thread .ai-agent-message').length > count",
                arg=before_messages,
                timeout=180_000,
            )
            page.wait_for_timeout(2_000)
    except PlaywrightTimeoutError:
        pass
    return {
        "text": text,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "messages_delta": max(0, message_count(page) - before_messages),
        "writes_delta": max(0, len(fake_writes) - before_writes),
        "thread_tail": thread_text(page)[-2200:],
    }


def _last_write(records: dict[str, Any], case_id: str) -> dict[str, Any]:
    writes = (records.get(case_id) or {}).get("new_writes") or []
    return writes[-1] if writes else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    parser.add_argument("--root-password", default="root")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/hackme_ai_agent_i2i_edit_context_{stamp}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    source_ref = {"filename": "agent-i2i-source.png", "subfolder": "agent-audit", "type": "output"}
    fake_writes: list[dict[str, Any]] = []
    browser_errors: list[str] = []
    chat_responses: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        page.on("console", lambda msg: browser_errors.append(msg.text) if msg.type == "error" else None)

        def image_preview_handler(route, request):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "ok": True,
                    "image": {
                        "mime_type": "image/png",
                        "size_bytes": 68,
                        "data_url": (
                            "data:image/png;base64,"
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
                        ),
                    },
                }, ensure_ascii=False),
            )

        def fake_job_handler(route, request):
            job_id = request.url.rstrip("/").split("/")[-1]
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "ok": True,
                    "job": {
                        "job_id": job_id,
                        "status": "completed",
                        "progress": {"percent": 100, "phase": "completed"},
                        "result": {
                            "images": [{
                                "filename": f"{job_id}.png",
                                "image_ref": {"filename": f"{job_id}.png", "subfolder": "agent-audit", "type": "output"},
                                "mime_type": "image/png",
                            }],
                        },
                    },
                }, ensure_ascii=False),
            )

        def write_tool_handler(route, request):
            try:
                payload = request.post_data_json or {}
            except Exception:
                payload = {}
            fake_writes.append(payload)
            page.evaluate(
                "payload => { window.__AI_AGENT_FAKE_WRITES__ = window.__AI_AGENT_FAKE_WRITES__ || []; window.__AI_AGENT_FAKE_WRITES__.push(payload); }",
                payload,
            )
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "ok": True,
                    "tool": payload.get("tool") or "",
                    "status": 200,
                    "result": {
                        "ok": True,
                        "job": {"job_id": f"fake-i2i-edit-{len(fake_writes)}", "status": "queued"},
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
            messages = req_payload.get("messages") if isinstance(req_payload, dict) else []
            prompt = str(messages[0].get("content") or "") if messages and isinstance(messages[0], dict) else ""
            content = str((res_payload.get("message") or {}).get("content") or res_payload.get("msg") or "")
            chat_responses.append({
                "kind": "planner" if "工具路由器" in prompt and "context=" in prompt else "chat",
                "status": response.status,
                "ok": bool(res_payload.get("ok")),
                "elapsed_ms": res_payload.get("elapsed_ms"),
                "content_preview": content[:2000],
            })

        page.route("**/api/comfyui/image-preview", image_preview_handler)
        page.route("**/api/comfyui/jobs/fake-i2i-edit-*", fake_job_handler)
        page.route("**/api/ai-agent/write-tools/execute", write_tool_handler)
        page.on("response", on_response)

        login(page, args.base_url.rstrip("/"), "root", args.root_password)
        open_ai_agent(page, args.base_url.rstrip("/"))
        status = api_fetch(page, "GET", "/api/ai-agent/status")
        models = api_fetch(page, "GET", "/api/ai-agent/models")
        seed_recent_image(page, source_ref)

        cases = [
            (
                "style_img2img",
                "請把剛剛那張站內圖片改成透明水彩風格，保留人物構圖與大致比例，使用圖生圖，denoise_strength 0.62，1024x1024。",
                True,
            ),
            (
                "outpaint",
                "請把上一張圖向左右各外延 128px、上下各外延 64px，feathering 48，延續原本背景，不要換主體。",
                True,
            ),
            (
                "inpaint_missing_mask",
                "請局部重繪上一張圖中央區域，把中間改成一朵花，但我沒有提供 mask。",
                False,
            ),
        ]
        for case_id, text, expect_write in cases:
            before_chat = len(chat_responses)
            before_writes = len(fake_writes)
            result = send_case(page, text, fake_writes, expect_write=expect_write)
            result["case_id"] = case_id
            result["chat_responses"] = chat_responses[before_chat:]
            result["new_writes"] = fake_writes[before_writes:]
            records.append(result)

        screenshot_path = out_dir / "ai_agent_i2i_edit_context.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        browser.close()

    by_case = {record["case_id"]: record for record in records}
    style_write = _last_write(by_case, "style_img2img")
    style_args = style_write.get("arguments") or {}
    outpaint_write = _last_write(by_case, "outpaint")
    outpaint_args = outpaint_write.get("arguments") or {}
    inpaint_tail = (by_case.get("inpaint_missing_mask") or {}).get("thread_tail") or ""
    checks = {
        "style_uses_comfyui_generate": style_write.get("tool") == "write_comfyui_generate",
        "style_uses_img2img": style_args.get("generation_mode") == "img2img",
        "style_uses_recent_source_ref": style_args.get("source_image_ref") == source_ref,
        "style_keeps_denoise": str(style_args.get("denoise_strength")) in {"0.62", "0.620000", "0.62"},
        "outpaint_uses_comfyui_generate": outpaint_write.get("tool") == "write_comfyui_generate",
        "outpaint_mode_and_source": outpaint_args.get("generation_mode") == "outpaint" and outpaint_args.get("source_image_ref") == source_ref,
        "outpaint_edges": (
            int(outpaint_args.get("outpaint_left") or 0) == 128
            and int(outpaint_args.get("outpaint_right") or 0) == 128
            and int(outpaint_args.get("outpaint_top") or 0) == 64
            and int(outpaint_args.get("outpaint_bottom") or 0) == 64
            and int(outpaint_args.get("outpaint_feathering") or 0) == 48
        ),
        "inpaint_missing_mask_does_not_write": (by_case.get("inpaint_missing_mask") or {}).get("writes_delta") == 0,
        "inpaint_missing_mask_clarifies": "mask" in inpaint_tail.lower() or "遮罩" in inpaint_tail,
        "no_browser_errors": not browser_errors,
    }
    report = {
        "ok": all(checks.values()),
        "checks": checks,
        "base_url": args.base_url,
        "source_ref": source_ref,
        "status": status,
        "models": models,
        "records": records,
        "fake_writes": fake_writes,
        "chat_responses": chat_responses,
        "browser_errors": browser_errors,
        "artifacts": {"screenshot": str(screenshot_path)},
    }
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / "report.md"
    md_path.write_text(
        "# AI Agent Image-to-Image Edit Context Probe\n\n"
        + "\n".join(f"- {key}: {'PASS' if value else 'FAIL'}" for key, value in checks.items())
        + f"\n\nJSON: `{json_path}`\nScreenshot: `{screenshot_path}`\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": report["ok"], "out_dir": str(out_dir), "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
