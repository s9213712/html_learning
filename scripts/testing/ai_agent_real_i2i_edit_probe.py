#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
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


def login(page, base_url: str, password: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.evaluate("() => fetch('/api/csrf-token', {credentials: 'same-origin'}).catch(() => null)")
    result = api_fetch(page, "POST", "/api/login", {"username": "root", "password": password})
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"login failed: {result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")


def open_ai_agent(page, base_url: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.locator("#tab-module-ai-agent").wait_for(state="visible", timeout=20_000)
    page.click("#tab-module-ai-agent")
    page.locator("#module-ai-agent.active").wait_for(state="visible", timeout=15_000)
    page.locator("#ai-agent-input").wait_for(state="visible", timeout=15_000)


def latest_output_ref(page) -> dict[str, Any]:
    result = api_fetch(page, "GET", "/api/comfyui/history")
    if result["status"] != 200:
        raise RuntimeError(f"history failed: {result}")
    for item in result["body"].get("history") or []:
        images = ((item.get("result") or {}).get("images")) or []
        for image in images:
            ref = image.get("image_ref")
            if isinstance(ref, dict) and ref.get("filename") and ref.get("type") == "output":
                return ref
    raise RuntimeError("no output image_ref found in ComfyUI history")


def seed_recent_image(page, source_ref: dict[str, Any]) -> None:
    page.evaluate(
        """sourceRef => {
          AI_AGENT_STATE.messages = [];
          AI_AGENT_STATE.lastComfyuiJob = {
            job_id: "real-i2i-source",
            status: "completed",
            progress: {percent: 100},
            result: {images: [{filename: sourceRef.filename, image_ref: sourceRef, mime_type: "image/png"}]},
          };
          AI_AGENT_STATE.lastComfyuiArgs = {
            prompt: "previous generated site image",
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


def wait_for_job(page, job_id: str, *, timeout_seconds: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.time() + timeout_seconds
    last_job: dict[str, Any] = {}
    polls: list[dict[str, Any]] = []
    while time.time() < deadline:
        result = api_fetch(page, "GET", f"/api/comfyui/jobs/{job_id}")
        last_job = (result.get("body") or {}).get("job") or {}
        polls.append({
            "status": result.get("status"),
            "ok": result.get("ok"),
            "job_status": last_job.get("status"),
            "phase": (last_job.get("progress") or {}).get("phase") if isinstance(last_job.get("progress"), dict) else None,
            "error": last_job.get("error") or (result.get("body") or {}).get("msg"),
        })
        if last_job.get("status") in {"completed", "error", "cancelled"}:
            return last_job, polls
        time.sleep(2)
    return {**last_job, "timed_out": True}, polls


def write_call_job_id(write_calls: list[dict[str, Any]]) -> str:
    if not write_calls:
        return ""
    result = (write_calls[-1].get("response") or {}).get("result") or {}
    return str(((result.get("job") or {}).get("job_id")) or "")


def thread_job_id(text: str) -> str:
    matches = re.findall(r"Job ID[:：]\s*([A-Za-z0-9_-]+)", text or "")
    return matches[-1] if matches else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/hackme_ai_agent_real_i2i_edit_{stamp}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    browser_errors: list[str] = []
    chat_responses: list[dict[str, Any]] = []
    write_calls: list[dict[str, Any]] = []
    report: dict[str, Any] = {"ok": False}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        page.on("console", lambda msg: browser_errors.append(msg.text) if msg.type == "error" else None)

        def on_response(response):
            if "/api/ai-agent/chat" in response.url:
                try:
                    payload = response.json()
                except Exception as exc:
                    chat_responses.append({"status": response.status, "error": str(exc)})
                    return
                chat_responses.append({
                    "status": response.status,
                    "ok": bool(payload.get("ok")),
                    "elapsed_ms": payload.get("elapsed_ms"),
                    "content": str((payload.get("message") or {}).get("content") or "")[:2200],
                })
            if "/api/ai-agent/write-tools/execute" in response.url:
                try:
                    request_payload = response.request.post_data_json or {}
                except Exception:
                    request_payload = {}
                try:
                    response_payload = response.json()
                except Exception as exc:
                    response_payload = {"error": str(exc)}
                write_calls.append({
                    "status": response.status,
                    "request": request_payload,
                    "response": response_payload,
                })

        page.on("response", on_response)

        base_url = args.base_url.rstrip("/")
        login(page, base_url, args.root_password)
        open_ai_agent(page, base_url)
        source_ref = latest_output_ref(page)
        seed_recent_image(page, source_ref)

        prompt = (
            "請真的使用本站圖生圖功能，把剛剛那張站內圖片改成淡透明水彩風格，"
            "保留構圖，使用 generation_mode img2img，denoise_strength 0.25，steps 1，batch_size 1。"
        )
        started = time.perf_counter()
        page.fill("#ai-agent-input", prompt)
        page.click("#ai-agent-send-btn")
        try:
            page.wait_for_function("() => window.__unused__ || false", timeout=1_000)
        except PlaywrightTimeoutError:
            pass
        deadline = time.time() + 240
        job_id = ""
        while time.time() < deadline:
            job_id = write_call_job_id(write_calls)
            if job_id:
                break
            time.sleep(1)
        job, job_polls = wait_for_job(page, job_id, timeout_seconds=args.timeout_seconds) if job_id else ({}, [])
        thread_tail = thread_text(page)[-4000:]
        thread_completed = "ComfyUI 產圖完成" in thread_tail and "輸出：" in thread_tail
        elapsed = round(time.perf_counter() - started, 3)

        final_job_id = job_id or write_call_job_id(write_calls) or thread_job_id(thread_tail)
        screenshot = out_dir / "ai_agent_real_i2i_edit.png"
        page.screenshot(path=str(screenshot), full_page=True)
        report = {
            "ok": bool(final_job_id and (job.get("status") == "completed" or thread_completed) and not browser_errors),
            "prompt": prompt,
            "source_ref": source_ref,
            "job_id": final_job_id,
            "elapsed_seconds": elapsed,
            "chat_responses": chat_responses,
            "write_calls": write_calls,
            "job": job,
            "job_polls": job_polls,
            "browser_errors": browser_errors,
            "thread_tail": thread_tail,
            "screenshot": str(screenshot),
        }
        browser.close()

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(report_path), "job_status": (report.get("job") or {}).get("status")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
