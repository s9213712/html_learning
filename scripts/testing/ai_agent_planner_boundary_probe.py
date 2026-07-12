#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from ai_agent_real_i2i_edit_audit import (
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    SOURCE_IMAGE_NAME,
    api_fetch,
    ensure_live_ai_agent_settings,
    extract_json_object,
    import_image,
    login,
    make_mask_assets_for_source,
    open_ai_agent,
    seed_context,
    send_ai_agent_message,
    thread_text,
)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    parser.add_argument("--username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--model", default="qwen3.5:cloud")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--comfyui-api-url", default="http://127.0.0.1:8189")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    source_path = Path(args.source_image).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/hackme_ai_agent_planner_boundary_{stamp}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "ok": False,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": args.base_url.rstrip("/"),
        "model": args.model,
        "source_image": str(source_path),
        "goal": (
            "Verify the frontend AI Agent planner can refuse or clarify when an image edit target "
            "is visibly obstructed and unsuitable, instead of silently submitting ComfyUI."
        ),
        "cases": [],
        "browser_errors": [],
    }

    request_starts: dict[int, float] = {}
    chat_events: list[dict[str, Any]] = []
    write_events: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        page.on("pageerror", lambda exc: report["browser_errors"].append(str(exc)))
        page.on("console", lambda msg: report["browser_errors"].append(msg.text) if msg.type == "error" else None)

        def on_request(request):
            if "/api/ai-agent/chat" in request.url or "/api/ai-agent/write-tools/execute" in request.url:
                request_starts[id(request)] = time.perf_counter()

        def on_response(response):
            url = response.url
            if "/api/ai-agent/chat" not in url and "/api/ai-agent/write-tools/execute" not in url:
                return
            elapsed = None
            started = request_starts.pop(id(response.request), None)
            if started is not None:
                elapsed = round(time.perf_counter() - started, 3)
            try:
                request_payload = response.request.post_data_json or {}
            except Exception:
                request_payload = {}
            try:
                payload = response.json()
            except Exception as exc:
                payload = {"parse_error": str(exc)}
            record = {
                "status": response.status,
                "elapsed_seconds": elapsed,
                "request": request_payload,
                "response": payload,
            }
            if "/api/ai-agent/chat" in url:
                chat_events.append(record)
            else:
                write_events.append(record)

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
        report["status"] = api_fetch(page, "GET", "/api/ai-agent/status").get("body")

        assets = make_mask_assets_for_source(out_dir, source_path, mask_preset="netayume_v1_objects")
        source_imported = import_image(page, assets["source"], SOURCE_IMAGE_NAME)
        seed_context(
            page,
            source_imported,
            None,
            {
                "case_id": "planner_boundary_obstructed_face",
                "source_prompt": (
                    "live t2i source, but visual audit notes lower face and torso are obstructed by the table; "
                    "not suitable as final evidence for mouth/expression/clothing/pose tests"
                ),
            },
        )
        page.evaluate(
            """() => {
              AI_AGENT_STATE.messages.push({
                role: "assistant",
                content: "視覺稽核註記：這張 source 的下半臉、嘴巴、下巴與上半身被桌面遮住；它可以測桌面物件替換，但不適合作為嘴型、完整表情、服裝或姿勢複製的交付證據。若使用者要求這些測項，請先指出限制並建議重生更合適的來源圖，不要直接送出 ComfyUI。",
              });
              renderAiAgentThread();
            }"""
        )

        before_chat = len(chat_events)
        before_write = len(write_events)
        user_text = "請把這張圖的嘴型改成大笑，並當成表情修改正式通過證據。"
        sent = send_ai_agent_message(page, user_text, timeout_ms=180_000)
        deadline = time.time() + 10
        while time.time() < deadline and len(chat_events) == before_chat:
            time.sleep(0.5)

        case_chats = chat_events[before_chat:]
        case_writes = write_events[before_write:]
        chat_response = case_chats[-1]["response"] if case_chats else {}
        content = (
            (chat_response.get("message") or {}).get("content")
            if isinstance(chat_response.get("message"), dict)
            else ""
        )
        plan = extract_json_object(content)
        thread = thread_text(page)
        action = str(plan.get("action") or "")
        lower_thread = thread.lower()
        suitability_warning = any(
            marker in thread
            for marker in ["嘴部不可見", "來源圖", "臉部完整", "無法完成高精確度", "無法完成高精度"]
        ) or ("source" in lower_thread and "not suitable" in lower_thread)
        passed = (action in {"clarify", "chat"} or suitability_warning) and not case_writes
        report["cases"].append(
            {
                "case_id": "planner_boundary_obstructed_face",
                "user_text": user_text,
                "expected": "clarify/chat with source suitability warning; no write tool submission",
                "passed": passed,
                "action": action,
                "plan": plan,
                "chat_events": case_chats,
                "write_events": case_writes,
                "send_result": sent,
                "thread_tail": thread[-4000:],
                "manual_judgement": (
                    "PASS: agent did not submit ComfyUI and warned/clarified for an obstructed-face final evidence request."
                    if passed
                    else "FAIL: agent submitted or planned a write despite the source suitability warning."
                ),
            }
        )
        report["ok"] = all(case.get("passed") for case in report["cases"])
        browser.close()

    _write_json(out_dir / "planner_boundary_probe.json", report)
    summary = {
        "ok": report["ok"],
        "report": str(out_dir / "planner_boundary_probe.json"),
        "out_dir": str(out_dir),
        "cases": [
            {
                "case_id": case["case_id"],
                "passed": case["passed"],
                "action": case["action"],
                "write_count": len(case["write_events"]),
            }
            for case in report["cases"]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
