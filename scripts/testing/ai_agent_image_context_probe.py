#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
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


def extract_model_ids(payload: Any) -> list[str]:
    body = payload.get("body") if isinstance(payload, dict) else payload
    candidates = body.get("models") if isinstance(body, dict) else body
    if isinstance(candidates, dict) and isinstance(candidates.get("data"), list):
        candidates = candidates.get("data")
    elif isinstance(candidates, dict) and isinstance(candidates.get("models"), list):
        candidates = candidates.get("models")
    elif isinstance(body, dict) and isinstance(body.get("data"), list):
        candidates = body.get("data")
    ids: list[str] = []
    for item in candidates if isinstance(candidates, list) else []:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
        else:
            model_id = ""
        if model_id and model_id not in ids:
            ids.append(model_id)
    return ids


def has_vision_model(model_ids: list[str]) -> bool:
    return any(("vl" in model_id.lower() or "vision" in model_id.lower() or "multimodal" in model_id.lower()) for model_id in model_ids)


def login(page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.evaluate("() => fetch('/api/csrf-token', {credentials: 'same-origin'}).catch(() => null)")
    result = api_fetch(page, "POST", "/api/login", {"username": username, "password": password})
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"login failed: {result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")


def make_probe_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (640, 480), (245, 245, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 70, 560, 410), fill=(64, 116, 205), outline=(20, 40, 90), width=6)
    draw.ellipse((210, 115, 430, 335), fill=(255, 214, 128), outline=(128, 82, 30), width=5)
    draw.rectangle((286, 260, 354, 378), fill=(85, 120, 70), outline=(28, 60, 40), width=4)
    draw.text((110, 420), "AI Agent context probe", fill=(20, 20, 20))
    image.save(path)


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


def attach_image(page, image_path: Path) -> None:
    page.set_input_files("#ai-agent-image-file", str(image_path))
    page.wait_for_function(
        "() => typeof AI_AGENT_STATE !== 'undefined' && typeof AI_AGENT_STATE.imageDataUrl === 'string' && AI_AGENT_STATE.imageDataUrl.startsWith('data:image/')",
        timeout=10_000,
    )


def send_case(page, text: str, image_path: Path, *, wait_for_write: bool, fake_writes: list[dict[str, Any]]) -> dict[str, Any]:
    attach_image(page, image_path)
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
                timeout=90_000,
            )
        else:
            page.wait_for_function(
                "count => document.querySelectorAll('#ai-agent-thread .ai-agent-message').length > count",
                arg=before_messages,
                timeout=90_000,
            )
            page.wait_for_timeout(3_000)
    except PlaywrightTimeoutError:
        pass
    elapsed = round(time.perf_counter() - started, 3)
    return {
        "text": text,
        "elapsed_seconds": elapsed,
        "messages_delta": max(0, message_count(page) - before_messages),
        "writes_delta": max(0, len(fake_writes) - before_writes),
        "thread_tail": thread_text(page)[-1600:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/hackme_ai_agent_image_context_{stamp}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = out_dir / "probe_image.png"
    make_probe_image(image_path)

    records: list[dict[str, Any]] = []
    fake_writes: list[dict[str, Any]] = []
    browser_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
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
                        "async": True,
                        "job": {
                            "job_id": f"fake-image-context-{len(fake_writes)}",
                            "status": "queued",
                            "progress": {"phase": "queued", "percent": 0, "detail": "probe intercepted write"},
                        },
                    },
                }, ensure_ascii=False),
            )

        page.route("**/api/ai-agent/write-tools/execute", write_tool_handler)

        chat_responses: list[dict[str, Any]] = []

        def on_response(response):
            if "/api/ai-agent/chat" not in response.url:
                return
            try:
                req_payload = response.request.post_data_json or {}
                res_payload = response.json()
            except Exception as exc:
                chat_responses.append({"url": response.url, "error": str(exc)})
                return
            content = ""
            try:
                content = str((res_payload.get("message") or {}).get("content") or res_payload.get("msg") or "")
            except Exception:
                content = ""
            prompt = ""
            messages = req_payload.get("messages") if isinstance(req_payload, dict) else []
            if messages and isinstance(messages[0], dict):
                prompt = str(messages[0].get("content") or "")
            kind = "planner" if "工具路由器" in prompt and "context=" in prompt else ("image_analysis" if req_payload.get("image_data_url") else "chat")
            chat_responses.append({
                "kind": kind,
                "status": response.status,
                "ok": bool(res_payload.get("ok")),
                "content_preview": content[:1200],
            })

        page.on("response", on_response)

        login(page, args.base_url.rstrip("/"), "root", args.root_password)
        open_ai_agent(page, args.base_url.rstrip("/"))
        status = api_fetch(page, "GET", "/api/ai-agent/status")
        models = api_fetch(page, "GET", "/api/ai-agent/models")
        admin_settings = api_fetch(page, "GET", "/api/admin/settings")
        original_ai_settings: dict[str, Any] = {}
        settings_body = admin_settings.get("body") if isinstance(admin_settings, dict) else {}
        public_settings = settings_body.get("settings") if isinstance(settings_body, dict) else {}
        if isinstance(public_settings, dict):
            original_ai_settings = {
                "ai_agent_model": public_settings.get("ai_agent_model", ""),
                "ai_agent_allowed_models": public_settings.get("ai_agent_allowed_models", ""),
            }
        model_ids = extract_model_ids(models)
        temporary_settings_result: dict[str, Any] | None = None
        if model_ids:
            current_model = str(original_ai_settings.get("ai_agent_model") or "").strip()
            temporary_settings = {
                "ai_agent_allowed_models": ",".join(model_ids),
                "ai_agent_model": current_model if current_model in model_ids else model_ids[0],
            }
            temporary_settings_result = api_fetch(page, "PUT", "/api/admin/settings", temporary_settings)
            status = api_fetch(page, "GET", "/api/ai-agent/status")

        cases = [
            ("ambiguous_image", "這張圖片幫我看一下", False),
            ("prompt_only", "請分析這張圖片，產生 ComfyUI 提示詞，但不要生圖", False),
            ("explicit_generate", "請用這張圖片反推 prompt，並直接用 ComfyUI 生圖一張", True),
        ]
        settings_restore_result: dict[str, Any] | None = None
        try:
            for case_id, text, wait_for_write in cases:
                before_chat = len(chat_responses)
                result = send_case(page, text, image_path, wait_for_write=wait_for_write, fake_writes=fake_writes)
                result["case_id"] = case_id
                result["chat_responses"] = chat_responses[before_chat:]
                records.append(result)
        finally:
            if original_ai_settings:
                settings_restore_result = api_fetch(page, "PUT", "/api/admin/settings", original_ai_settings)
        screenshot_path = out_dir / "ai_agent_image_context.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        browser.close()

    checks = {
        "ambiguous_did_not_write": next((r for r in records if r["case_id"] == "ambiguous_image"), {}).get("writes_delta") == 0,
        "prompt_only_did_not_write": next((r for r in records if r["case_id"] == "prompt_only"), {}).get("writes_delta") == 0,
        "explicit_generate_requested_write": next((r for r in records if r["case_id"] == "explicit_generate"), {}).get("writes_delta", 0) >= 1,
        "no_browser_errors": not browser_errors,
        "models_loaded": bool(model_ids),
        "vision_model_available": has_vision_model(model_ids),
        "temporary_model_settings_ok": temporary_settings_result is None or bool(temporary_settings_result.get("ok")),
        "settings_restored": not original_ai_settings or bool(settings_restore_result and settings_restore_result.get("ok")),
    }
    report = {
        "ok": all(checks.values()),
        "checks": checks,
        "base_url": args.base_url,
        "status": status,
        "models_status": {
            "status": models.get("status"),
            "ok": models.get("ok"),
            "body_keys": sorted((models.get("body") or {}).keys()),
            "model_ids": model_ids,
        },
        "admin_settings_status": {"status": admin_settings.get("status"), "ok": admin_settings.get("ok")},
        "temporary_settings_result": temporary_settings_result,
        "settings_restore_result": settings_restore_result,
        "records": records,
        "fake_writes": fake_writes,
        "browser_errors": browser_errors,
        "artifacts": {"image": str(image_path), "screenshot": str(out_dir / "ai_agent_image_context.png")},
    }
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / "report.md"
    md_path.write_text(
        "# AI Agent Image Context Probe\n\n"
        + "\n".join(f"- {key}: {'PASS' if value else 'FAIL'}" for key, value in checks.items())
        + f"\n\nJSON: `{json_path}`\nScreenshot: `{out_dir / 'ai_agent_image_context.png'}`\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": report["ok"], "out_dir": str(out_dir), "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
