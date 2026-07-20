#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.dont_write_bytecode = True

from scripts.test_artifacts import test_artifact_path  # noqa: E402

from ai_agent_real_i2i_edit_audit import (
    ai_agent_preflight,
    api_fetch,
    ensure_live_ai_agent_settings,
    login,
    open_ai_agent,
    send_ai_agent_message,
    thread_messages,
    thread_text,
)


def fetch_url_json(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8", errors="replace") or "{}")
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}


def write_report(report: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# AI Agent ComfyUI Status Probe",
        "",
        f"- Started: {report.get('started_at')}",
        f"- Base URL: `{report.get('base_url')}`",
        f"- Model: `{report.get('model')}`",
        f"- Overall: `{report.get('status')}`",
        "",
        "## Prompt",
        "",
        report.get("prompt", ""),
        "",
        "## Result",
        "",
        f"- send ok: `{report.get('send_result', {}).get('ok')}`",
        f"- response seconds: `{report.get('response_seconds')}`",
        f"- final UI msg: `{report.get('final_ui_message')}`",
        f"- messages after: `{report.get('message_count_after')}`",
        f"- send disabled after: `{report.get('send_disabled_after')}`",
        "",
        "## Chat Events",
        "",
    ]
    for event in report.get("chat_events", []):
        usage = (event.get("response") or {}).get("usage") or {}
        message = ((event.get("response") or {}).get("message") or {}).get("content") or (event.get("response") or {}).get("msg") or ""
        lines.extend([
            f"- HTTP `{event.get('status')}`, elapsed `{event.get('elapsed_seconds')}` sec, usage `{json.dumps(usage, ensure_ascii=False)}`",
            "",
            "```text",
            str(message)[:3000],
            "```",
            "",
        ])
    lines.extend([
        "## Thread Tail",
        "",
        "```text",
        str(report.get("thread_tail") or "")[-5000:],
        "```",
        "",
        "## ComfyUI Queue Snapshot",
        "",
        "```json",
        json.dumps(report.get("comfyui_queue"), ensure_ascii=False, indent=2)[:8000],
        "```",
    ])
    (out_dir / "AI_AGENT_COMFYUI_STATUS_PROBE.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    parser.add_argument("--username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--model", default="qwen3.5:cloud")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--comfyui-api-url", default="http://127.0.0.1:8189")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--timeout-ms", type=int, default=180_000)
    parser.add_argument(
        "--prompt",
        default=(
            "目前 ComfyUI 是否還在生圖？請查站內可見狀態與進度，告訴我 job 是否 running、pending 或卡住；"
            "如果是長時間 running，請明確說明你看到的狀態，不要假裝完成。"
        ),
    )
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = (
        Path(args.out_dir).resolve()
        if args.out_dir
        else test_artifact_path("reports", f"{stamp}_ai_agent_comfyui_status_probe").resolve()
    )
    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": args.base_url.rstrip("/"),
        "model": args.model,
        "prompt": args.prompt,
        "status": "started",
        "chat_events": [],
        "browser_errors": [],
    }
    request_starts: dict[int, float] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        page.on("pageerror", lambda exc: report["browser_errors"].append(str(exc)))
        page.on("console", lambda msg: report["browser_errors"].append(msg.text) if msg.type == "error" else None)

        def on_request(request):
            if "/api/ai-agent/chat" in request.url:
                request_starts[id(request)] = time.perf_counter()

        def on_response(response):
            if "/api/ai-agent/chat" not in response.url:
                return
            started = request_starts.pop(id(response.request), None)
            elapsed = round(time.perf_counter() - started, 3) if started is not None else None
            try:
                request_payload = response.request.post_data_json or {}
            except Exception:
                request_payload = {}
            try:
                payload = response.json()
            except Exception as exc:
                payload = {"parse_error": str(exc)}
            report["chat_events"].append({
                "status": response.status,
                "elapsed_seconds": elapsed,
                "request": request_payload,
                "response": payload,
            })

        page.on("request", on_request)
        page.on("response", on_response)

        login(page, report["base_url"], args.username, args.root_password)
        report["settings_update"] = ensure_live_ai_agent_settings(
            page,
            model=args.model,
            api_base_url=args.api_base_url,
            comfyui_api_url=args.comfyui_api_url,
        )
        open_ai_agent(page, report["base_url"])
        report["preflight"] = ai_agent_preflight(page)
        report["comfyui_status_before"] = api_fetch(page, "GET", "/api/comfyui/status").get("body")
        report["readonly_comfyui_before"] = api_fetch(page, "GET", "/api/ai-agent/readonly?scope=comfyui&limit=20").get("body")

        start = time.perf_counter()
        report["send_result"] = send_ai_agent_message(page, args.prompt, timeout_ms=args.timeout_ms)
        report["response_seconds"] = round(time.perf_counter() - start, 3)
        report["thread_text"] = thread_text(page)
        report["thread_tail"] = report["thread_text"][-8000:]
        report["messages"] = thread_messages(page)
        report["message_count_after"] = len(report["messages"])
        report["final_ui_message"] = page.locator("#ai-agent-msg").inner_text(timeout=5_000)
        report["send_disabled_after"] = page.locator("#ai-agent-send-btn").is_disabled()
        report["readonly_comfyui_after"] = api_fetch(page, "GET", "/api/ai-agent/readonly?scope=comfyui&limit=20").get("body")
        report["comfyui_queue"] = fetch_url_json(args.comfyui_api_url.rstrip("/") + "/queue")
        report["status"] = "ok" if report["send_result"].get("ok") and not report["send_disabled_after"] else "needs_review"
        browser.close()

    write_report(report, out_dir)
    print(json.dumps({"status": report["status"], "out_dir": str(out_dir)}, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
