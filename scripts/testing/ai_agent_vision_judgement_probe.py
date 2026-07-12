#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from ai_agent_real_i2i_edit_audit import api_fetch, login


DEFAULT_VISION_MODEL = "qwen3.5:cloud"
DEFAULT_AI_AGENT_API_BASE_URL = "http://127.0.0.1:11434/v1"


def image_data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def ensure_vision_settings(page, model: str, api_base_url: str) -> dict[str, Any]:
    current = api_fetch(page, "GET", "/api/admin/settings")
    settings = (current.get("body") or {}).get("settings") if isinstance(current.get("body"), dict) else {}
    allowed = [
        item.strip()
        for item in str((settings or {}).get("ai_agent_allowed_models") or "").split(",")
        if item.strip()
    ]
    if model not in allowed:
        allowed.append(model)
    payload = {
        "ai_agent_provider": "openai_compatible",
        "ai_agent_api_base_url": api_base_url.rstrip("/"),
        "ai_agent_model": model,
        "ai_agent_allowed_models": ",".join(allowed),
        "ai_agent_allow_image_input": True,
    }
    updated = api_fetch(page, "PUT", "/api/admin/settings", payload)
    return {"before": current, "request": payload, "after": updated}


def ask_vision_judgement(page, image_path: Path, model: str) -> dict[str, Any]:
    prompt = (
        "你是本站 AI Agent 的產圖驗收者。請直接觀看附圖，不要只依賴文字描述。"
        "這張圖是剛剛 t2i 產出的基底圖，原始需求是：1920x1080 anime style 1girl，"
        "正面半身女孩在木桌後，畫面上半部中央要有清楚臉部、五官、大眼睛、鼻子、嘴巴、溫和微笑表情，"
        "自然長髮框住臉部但不能遮住五官；桌面左側要有紅蘋果，右側要有無文字藍色杯子，右側手部可以故意異常供後續修正；"
        "圖片上不應有 prompt 文字、水印、logo，也不能沒有臉。"
        "請輸出 JSON，欄位：pass(boolean), needs_regeneration(boolean), score_0_to_100(number), "
        "observed_issues(array), face_visible(boolean), facial_features_clear(boolean), hair_visible(boolean), "
        "apple_visible(boolean), cup_visible(boolean), prompt_text_visible(boolean), regeneration_prompt_delta(string), reasoning(string)。"
        "如果臉部不存在或五官不清楚，必須判定 needs_regeneration=true。"
    )
    started = time.perf_counter()
    response = api_fetch(
        page,
        "POST",
        "/api/ai-agent/chat",
        {
            "prompt": prompt,
            "image_data_url": image_data_url(image_path),
            "model": model,
            "session_id": f"vision-judgement-{int(time.time())}",
        },
    )
    elapsed = round(time.perf_counter() - started, 3)
    return {"elapsed_seconds": elapsed, "response": response}


def extract_agent_content(body: dict[str, Any]) -> str:
    if not isinstance(body, dict):
        return ""
    message = body.get("message")
    if isinstance(message, dict) and str(message.get("content") or "").strip():
        return str(message.get("content") or "")
    return str(body.get("content") or body.get("msg") or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    parser.add_argument("--username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--api-base-url", default=DEFAULT_AI_AGENT_API_BASE_URL)
    args = parser.parse_args()

    image_path = Path(args.image).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "ok": False,
        "base_url": args.base_url.rstrip("/"),
        "image": str(image_path),
        "model": args.model,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(ignore_https_errors=True).new_page()
        login(page, report["base_url"], args.username, args.root_password)
        report["settings_update"] = ensure_vision_settings(page, args.model, args.api_base_url)
        report["judgement"] = ask_vision_judgement(page, image_path, args.model)
        browser.close()

    body = ((report.get("judgement") or {}).get("response") or {}).get("body") or {}
    report["ok"] = bool(((report.get("judgement") or {}).get("response") or {}).get("ok"))
    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (out_dir / "vision_judgement.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    content = extract_agent_content(body)
    lines = [
        "# AI Agent Vision Judgement Probe",
        "",
        f"- OK: `{report['ok']}`",
        f"- Model: `{args.model}`",
        f"- Image: `{image_path}`",
        f"- Elapsed seconds: `{(report.get('judgement') or {}).get('elapsed_seconds')}`",
        "",
        "## AI Agent Response",
        "",
        "```text",
        content[:12000],
        "```",
        "",
        "## Raw Response",
        "",
        "```json",
        json.dumps(body, ensure_ascii=False, indent=2)[:12000],
        "```",
    ]
    (out_dir / "vision_judgement.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(out_dir / "vision_judgement.md")}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
