#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


TOOL_CANDIDATES = [
    "write_cloud_drive_create_text",
    "write_cloud_drive_upload",
    "write_cloud_drive_delete",
    "write_cloud_drive_remote_download",
    "write_share_create",
    "write_share_update",
    "write_share_revoke",
    "write_task_cancel",
    "write_task_retry",
    "write_automation_job_run",
]


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def add(self, case_id: str, item: str, ok: bool, result: str, evidence: dict[str, Any] | None = None, *, expected_gap: bool = False) -> None:
        row = {"case_id": case_id, "item": item, "ok": bool(ok), "expected_gap": bool(expected_gap), "result": result, "evidence": evidence or {}}
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


def login(page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    result = api_fetch(page, "POST", "/api/login", {"username": username, "password": password})
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"login failed for {username}: {result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")


def execute_ai_tool(page, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return api_fetch(page, "POST", "/api/ai-agent/write-tools/execute", {"tool": tool, "arguments": arguments or {}, "confirm": "EXECUTE"})


def install_drive_planner_mock(page) -> dict[str, Any]:
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
            plan = {"action": "cloud_drive_manage", "confidence": 0.96, "execute_write": True, "reason": "drive/share/task requested"}
            state["planner_calls"] += 1
            state["plans"].append({"user": user_text[:240], "plan": plan})
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}}))
            return
        state["chat_calls"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": "目前 AI Agent 尚未接上雲端硬碟、分享管理、任務取消/重試或自動化作業工具；不能假裝已完成檔案或分享操作。"}}))

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--root-password", required=True)
    parser.add_argument("--test-password", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rec = Recorder()
    run_id = str(int(time.time()))[-8:]
    request_log: list[dict[str, Any]] = []
    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        root_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        root_page = root_ctx.new_page()
        root_page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        root_page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        root_page.on("request", lambda request: request_log.append({"url": request.url, "method": request.method, "post_data": request.post_data}))
        planner_state = install_drive_planner_mock(root_page)
        login(root_page, args.base_url, "root", args.root_password)

        api_fetch(
            root_page,
            "PUT",
            "/api/admin/settings",
            {
                "feature_ai_agent_enabled": True,
                "module_ai_agent_min_role": "user",
                "ai_agent_provider": "openai_compatible",
                "ai_agent_api_base_url": "http://127.0.0.1:9",
                "ai_agent_api_key": "sk-drive-probe",
                "ai_agent_model": "qa-router",
                "ai_agent_allowed_models": "qa-router",
                "ai_agent_allowed_tools": "",
                "ai_agent_operation_mode": "write",
            },
        )

        files = api_fetch(root_page, "GET", "/api/cloud-drive/files")
        remote_caps = api_fetch(root_page, "GET", "/api/cloud-drive/remote-download/capabilities")
        remote_tasks = api_fetch(root_page, "GET", "/api/cloud-drive/remote-download/tasks")
        shares = api_fetch(root_page, "GET", "/api/shares")
        jobs = api_fetch(root_page, "GET", "/api/jobs")
        rec.add(
            "DST-01",
            "站內雲端/分享/任務 API 存在",
            all(item["status"] == 200 and item["body"].get("ok", True) for item in [files, remote_caps, remote_tasks, shares, jobs]),
            f"files={files['status']}, remote_caps={remote_caps['status']}, shares={shares['status']}, jobs={jobs['status']}",
            {"files_keys": sorted(files["body"].keys()), "shares_count": len(shares["body"].get("shares") or []), "jobs_keys": sorted(jobs["body"].keys())},
        )

        text_file = api_fetch(root_page, "POST", "/api/cloud-drive/files/text", {"filename": f"ai-agent-drive-probe-{run_id}.txt", "content": "AI Agent drive probe"})
        rec.add(
            "DST-02",
            "站內可建立小型文字檔",
            text_file["status"] == 200 and text_file["body"].get("ok"),
            f"create_text={text_file['status']}",
            {"body_keys": sorted(text_file["body"].keys()), "file": text_file["body"].get("file")},
        )

        tools = api_fetch(root_page, "GET", "/api/ai-agent/write-tools")
        unsupported = {tool: execute_ai_tool(root_page, tool, {"filename": "x.txt", "content": "x"}) for tool in TOOL_CANDIDATES}
        rec.add(
            "DST-03",
            "AI Agent 未接雲端/分享/任務工具",
            tools["status"] == 200
            and all(result["status"] == 400 and not result["body"].get("ok") for result in unsupported.values()),
            f"listed_tools={len(tools['body'].get('tools', []))}, rejected={sum(1 for result in unsupported.values() if result['status'] == 400)}/{len(unsupported)}",
            {"tools": tools["body"].get("tools"), "unsupported": {tool: {"status": result["status"], "body": result["body"]} for tool, result in unsupported.items()}},
            expected_gap=True,
        )

        open_ai_agent(root_page, args.base_url)
        before = len([r for r in request_log if any(path in r["url"] for path in ("/api/cloud-drive", "/api/shares", "/api/jobs", "/api/ai-agent/write-tools/execute"))])
        t0 = time.perf_counter()
        send_ai_text(root_page, "請建立雲端硬碟檔案、分享給會員，並把相關任務排程自動化")
        thread = wait_thread_any(root_page, ["尚未接上雲端硬碟", "不能假裝", "分享管理"], timeout=25000)
        elapsed = round(time.perf_counter() - t0, 3)
        after = len([r for r in request_log if any(path in r["url"] for path in ("/api/cloud-drive", "/api/shares", "/api/jobs", "/api/ai-agent/write-tools/execute"))])
        rec.add(
            "DST-04",
            "對話雲端/分享/任務要求不靜默成功",
            after == before and "不能假裝" in thread,
            f"response_s={elapsed}, drive_share_job_requests={after - before}",
            {"thread_tail": thread[-800:], "planner": planner_state},
            expected_gap=True,
        )

        test_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        test_page = test_ctx.new_page()
        login(test_page, args.base_url, "test", args.test_password)
        member_tools = api_fetch(test_page, "GET", "/api/ai-agent/write-tools")
        member_ai = execute_ai_tool(test_page, "write_cloud_drive_delete", {"file_id": "1"})
        member_files = api_fetch(test_page, "GET", "/api/cloud-drive/files")
        rec.add(
            "DST-05",
            "權限與越權",
            member_tools["status"] == 403 and member_ai["status"] == 403 and member_files["status"] == 200,
            f"member_tools={member_tools['status']}, member_ai={member_ai['status']}, member_files={member_files['status']}",
            {"member_tools": member_tools["body"], "member_ai": member_ai["body"]},
        )
        test_ctx.close()

        anomaly = anomaly_metrics(thread_messages(root_page))
        rec.add(
            "DST-06",
            "回應時間/跳針",
            elapsed < 25 and anomaly["empty"] == 0 and anomaly["repeated_adjacent"] == 0,
            f"response_s={elapsed}, empty={anomaly['empty']}, repeated_adjacent={anomaly['repeated_adjacent']}",
            {"anomaly": anomaly},
        )

        root_ctx.close()
        browser.close()

    filtered_console_errors = [
        item for item in console_errors
        if "favicon" not in item.lower()
        and "the server responded with a status of 400" not in item.lower()
        and "the server responded with a status of 403" not in item.lower()
    ]
    result = {
        "ok": not rec.failures,
        "rows": rec.rows,
        "failures": rec.failures,
        "expected_gaps": [row for row in rec.rows if row.get("expected_gap")],
        "console_errors": console_errors,
        "unexpected_console_errors": filtered_console_errors,
        "page_errors": page_errors,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "rows": len(rec.rows), "failures": len(rec.failures), "gaps": len(result["expected_gaps"]), "out": str(out)}, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
