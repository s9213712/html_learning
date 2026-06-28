#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ai_agent_real_i2i_edit_audit import (  # noqa: E402
    ai_agent_preflight,
    anomaly_metrics,
    api_fetch,
    first_result_image,
    import_image,
    latest_job_id_from_text,
    login,
    open_ai_agent,
    save_preview,
    send_ai_agent_message,
    thread_messages,
    thread_text,
    wait_job,
)


PROMPT = (
    "請真的使用本站 ComfyUI 圖生圖/img2img，而不是只回文字。使用剛剛提供的 source image，"
    "以 Qwen Image Edit GGUF Lite 或可用的官方 Qwen 圖片編輯 workflow，把畫面改成柔和水彩插畫風，"
    "保留原本女孩、桌面、杯子與整體構圖，不要新增文字或灰色框。請送出任務並持續追蹤進度；"
    "generation_mode=img2img，batch_size=1，confirm_billing=true，denoise_strength=0.65，steps=28。"
)


def seed_source_context(page, imported: dict[str, Any]) -> dict[str, Any]:
    return page.evaluate(
        """(source) => {
          AI_AGENT_STATE.messages = [];
          AI_AGENT_STATE.lastComfyuiJob = null;
          AI_AGENT_STATE.lastComfyuiArgs = {
            prompt: "semantic image edit probe source",
            generation_mode: "img2img",
            source_image_ref: source.image_ref,
          };
          AI_AGENT_STATE.messages.push({
            role: "assistant",
            content: "已提供一張測試原圖，context=source image for Qwen GGUF semantic image edit probe。這張圖片已匯入站內雲端硬碟與 ComfyUI input，可作為 source_image_ref。",
            images: [{
              image_ref: source.image_ref,
              cloud_file_id: source.cloud_file_id || "",
              storage_file_id: source.storage_file_id || "",
              filename: source.filename,
              mime_type: source.mime_type || "image/png",
            }],
          });
          renderAiAgentThread();
          return {
            recent_image_refs: typeof aiAgentRecentImageRefs === "function" ? aiAgentRecentImageRefs(8) : [],
            messages: AI_AGENT_STATE.messages,
          };
        }""",
        imported,
    )


def write_markdown(report: dict[str, Any], path: Path) -> None:
    result_image = report.get("result_image_rel") or ""
    expected = report.get("expected") or (
        "Execute the natural-language img2img edit through an available official Qwen image-edit "
        "workflow, preserve non-target regions, and avoid added text or gray-frame artifacts."
    )
    lines = [
        "# AI Agent Qwen Img2Img Probe",
        "",
        f"- OK: {report.get('ok')}",
        f"- Base URL: {report.get('base_url')}",
        f"- Source image: `{report.get('source_image')}`",
        f"- Imported cloud_file_id: `{(report.get('imported_image') or {}).get('cloud_file_id') or ''}`",
        f"- Job ID: `{report.get('job_id') or ''}`",
        f"- Job status: `{report.get('job_status') or ''}`",
        f"- Chat elapsed seconds: `{report.get('chat_elapsed_seconds')}`",
        f"- Write elapsed seconds: `{report.get('write_elapsed_seconds')}`",
        f"- Tokens/s: `{report.get('tokens_per_second')}`",
        f"- Usage: `{json.dumps(report.get('usage') or {}, ensure_ascii=False)}`",
        f"- Model: `{report.get('chat_model') or ''}`",
        "",
        "## Natural Language",
        "",
        str(report.get("natural_language") or PROMPT),
        "",
        "## Expected",
        "",
        str(expected),
        "",
        "## Result Image",
        "",
    ]
    if result_image:
        lines.append(f"![result]({result_image})")
    else:
        lines.append("No result image captured.")
    lines.extend([
        "",
        "## Final Thread",
        "",
        "```text",
        str(report.get("final_thread") or "")[:12000],
        "```",
        "",
        "## Write Response",
        "",
        "```json",
        json.dumps(report.get("write_response") or {}, ensure_ascii=False, indent=2)[:12000],
        "```",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    parser.add_argument("--username", default="root")
    parser.add_argument("--root-password", default="RootSmoke123!")
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--job-timeout-seconds", type=int, default=1800)
    parser.add_argument("--instruction", default=PROMPT)
    args = parser.parse_args()

    source_path = Path(args.source_image)
    out_dir = Path(args.out_dir)
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "ok": False,
        "base_url": args.base_url.rstrip("/"),
        "source_image": str(source_path),
        "natural_language": args.instruction,
        "expected": (
            "Execute the natural-language img2img edit through an available official Qwen image-edit "
            "workflow, preserve non-target regions, and avoid added text or gray-frame artifacts."
        ),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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
            record = {"status": response.status, "elapsed_seconds": elapsed, "request": request_payload, "response": payload}
            if "/api/ai-agent/chat" in url:
                chat_events.append(record)
            else:
                write_events.append(record)

        page.on("request", on_request)
        page.on("response", on_response)

        login(page, report["base_url"], args.username, args.root_password)
        open_ai_agent(page, report["base_url"])
        report["preflight"] = ai_agent_preflight(page)
        report["ai_agent_status"] = api_fetch(page, "GET", "/api/ai-agent/status").get("body")
        report["comfyui_status"] = api_fetch(page, "GET", "/api/comfyui/status").get("body")
        report["workflow_list"] = api_fetch(page, "GET", "/api/comfyui/workflows").get("body")

        imported = import_image(page, source_path, "agent_qwen_probe_source_1024x1024.png")
        report["imported_image"] = imported
        report["seed_context"] = seed_source_context(page, imported)

        before_chat = len(chat_events)
        before_writes = len(write_events)
        send_result = send_ai_agent_message(page, args.instruction)
        report["send_result"] = send_result
        deadline = time.time() + 30
        while time.time() < deadline and len(write_events) == before_writes:
            time.sleep(1)

        case_chats = chat_events[before_chat:]
        case_writes = write_events[before_writes:]
        report["chat_events"] = case_chats
        report["write_events"] = case_writes
        chat_response = case_chats[-1]["response"] if case_chats and isinstance(case_chats[-1].get("response"), dict) else {}
        usage = chat_response.get("usage") if isinstance(chat_response.get("usage"), dict) else {}
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or usage.get("eval_count")
        chat_elapsed = case_chats[-1].get("elapsed_seconds") if case_chats else None
        report["usage"] = usage
        report["chat_model"] = chat_response.get("model") or chat_response.get("selected_model") or ""
        report["chat_elapsed_seconds"] = chat_elapsed
        report["tokens_per_second"] = round(float(completion_tokens) / float(chat_elapsed), 3) if completion_tokens and chat_elapsed else None

        write = case_writes[-1] if case_writes else {}
        report["write_response"] = write.get("response") if isinstance(write.get("response"), dict) else {}
        report["write_request"] = write.get("request") if isinstance(write.get("request"), dict) else {}
        report["write_elapsed_seconds"] = write.get("elapsed_seconds")
        result_payload = (report["write_response"] or {}).get("result") if isinstance(report.get("write_response"), dict) else {}
        job_payload = result_payload.get("job") if isinstance(result_payload, dict) and isinstance(result_payload.get("job"), dict) else {}
        job_id = str(job_payload.get("job_id") or "") or latest_job_id_from_text(thread_text(page))
        report["job_id"] = job_id
        if job_id:
            job, polls = wait_job(page, job_id, args.job_timeout_seconds)
            report["job"] = job
            report["job_polls"] = polls
            report["job_status"] = job.get("status")
            image = first_result_image(job)
            if image:
                preview_path = assets_dir / "qwen_img2img_result.png"
                preview = save_preview(page, image["image_ref"], preview_path)
                report["preview"] = preview
                if preview.get("ok"):
                    report["result_image_rel"] = f"assets/{preview_path.name}"
        messages = thread_messages(page)
        report["thread_messages"] = messages
        report["final_thread"] = thread_text(page)
        report["anomaly_metrics"] = anomaly_metrics(messages)
        report["ok"] = bool(
            report.get("job_id")
            and str(report.get("job_status") or "").lower() == "completed"
            and (report.get("preview") or {}).get("ok")
            and not (report.get("anomaly_metrics") or {}).get("progress_regressions")
        )
        browser.close()

    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, out_dir / "report.md")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
