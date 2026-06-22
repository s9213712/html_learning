#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


def now_id() -> str:
    return str(int(time.time()))[-8:]


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def add(self, case_id: str, item: str, ok: bool, result: str, evidence: dict[str, Any] | None = None) -> None:
        row = {
            "case_id": case_id,
            "item": item,
            "ok": bool(ok),
            "result": result,
            "evidence": evidence or {},
        }
        self.rows.append(row)
        print(f"[{'PASS' if ok else 'FAIL'}] {case_id} {item}: {result}", flush=True)
        if not ok:
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


def install_ai_planner_mock(page) -> dict[str, Any]:
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
            if "生圖" in user_text or "comfyui" in user_text.lower():
                plan = {
                    "action": "comfyui_generate",
                    "confidence": 0.98,
                    "execute_write": True,
                    "reason": "explicit image generation request",
                    "args": {
                        "prompt": f"AI agent image generation probe icon {now_id()}, clean geometric robot, white background",
                        "negative_prompt": "low quality, blurry",
                        "width": 512,
                        "height": 512,
                        "steps": 1,
                        "cfg_scale": 3,
                        "batch_size": 1,
                    },
                }
            elif "cpu" in user_text.lower() or "ram" in user_text.lower() or "資源" in user_text:
                plan = {"action": "readonly", "confidence": 0.96, "readonly_scope": "resources", "reason": "resource status"}
            else:
                plan = {"action": "chat", "confidence": 0.9, "reason": "plain chat"}
            state["planner_calls"] += 1
            state["plans"].append({"user": user_text[:240], "plan": plan})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}}),
            )
            return
        state["chat_calls"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": {"role": "assistant", "content": "聊天測試完成。"}}),
        )

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


def thread_text(page) -> str:
    return page.locator("#ai-agent-thread").inner_text(timeout=10000)


def wait_thread_any(page, needles: list[str], timeout: int = 30000) -> str:
    page.locator("#ai-agent-thread").wait_for(state="visible", timeout=timeout)
    page.wait_for_function(
        """needles => {
          const text = document.querySelector('#ai-agent-thread')?.innerText || '';
          return needles.some((needle) => text.includes(needle));
        }""",
        arg=needles,
        timeout=timeout,
    )
    return thread_text(page)


def thread_messages(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#ai-agent-thread .ai-agent-message'))
          .map((el) => (el.innerText || '').trim())
          .filter(Boolean)"""
    )


def thread_anomaly_metrics(messages: list[str]) -> dict[str, Any]:
    repeated_adjacent = 0
    repeated_total = 0
    seen: dict[str, int] = {}
    job_ids: list[str] = []
    empty = 0
    progress_snapshots: list[dict[str, Any]] = []
    progress_snapshot_counts: dict[str, int] = {}
    for idx, message in enumerate(messages):
        normalized = re.sub(r"\s+", " ", message).strip()
        if not normalized:
            empty += 1
        if idx > 0 and normalized == re.sub(r"\s+", " ", messages[idx - 1]).strip():
            repeated_adjacent += 1
        seen[normalized] = seen.get(normalized, 0) + 1
        if seen[normalized] == 2:
            repeated_total += 1
        found_job_ids = re.findall(r"Job ID[:：]\s*([A-Za-z0-9_-]+)", message)
        job_ids.extend(found_job_ids)
        for job_id in found_job_ids:
            percent_match = re.search(r"進度[:：]\s*(\d+)%", message)
            status_match = re.search(r"狀態[:：]\s*([^\n]+)", message)
            percent = int(percent_match.group(1)) if percent_match else None
            status = status_match.group(1).strip() if status_match else ""
            snapshot_key = f"{job_id}|{percent}|{status}"
            progress_snapshot_counts[snapshot_key] = progress_snapshot_counts.get(snapshot_key, 0) + 1
            progress_snapshots.append({"job_id": job_id, "percent": percent, "status": status})
    duplicate_progress_snapshots = {
        snapshot: count for snapshot, count in progress_snapshot_counts.items() if count > 1
    }
    regressions = []
    last_percent_by_job: dict[str, int] = {}
    for snapshot in progress_snapshots:
        percent = snapshot.get("percent")
        job_id = str(snapshot.get("job_id") or "")
        if percent is None or not job_id:
            continue
        previous = last_percent_by_job.get(job_id)
        if previous is not None and percent < previous:
            regressions.append({"job_id": job_id, "previous": previous, "current": percent, "status": snapshot.get("status")})
        last_percent_by_job[job_id] = percent
    return {
        "message_count": len(messages),
        "empty_messages": empty,
        "repeated_adjacent": repeated_adjacent,
        "repeated_total": repeated_total,
        "job_ids": job_ids,
        "progress_snapshots": progress_snapshots,
        "duplicate_progress_snapshots": duplicate_progress_snapshots,
        "progress_regressions": regressions,
    }


def extract_job_id(text: str) -> str:
    match = re.search(r"Job ID[:：]\s*([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else ""


def execute_tool(page, tool: str, arguments: dict[str, Any] | None = None, *, confirm: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool": tool, "arguments": arguments or {}}
    if confirm:
        payload["confirm"] = "EXECUTE"
    return api_fetch(page, "POST", "/api/ai-agent/write-tools/execute", payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--root-password", required=True)
    parser.add_argument("--test-password", required=True)
    parser.add_argument("--comfyui-api-url", default="http://127.0.0.1:8189")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rec = Recorder()
    run_id = now_id()
    console_errors: list[str] = []
    page_errors: list[str] = []
    write_requests: list[dict[str, Any]] = []
    job_requests: list[str] = []
    timings: dict[str, Any] = {}
    request_timings: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        root_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
        root_page = root_ctx.new_page()
        root_page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        root_page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        root_page.on(
            "request",
            lambda request: (
                write_requests.append({"url": request.url, "post_data": request.post_data})
                if "/api/ai-agent/write-tools/execute" in request.url
                else job_requests.append(request.url)
                if "/api/comfyui/jobs/" in request.url
                else None
            ),
        )
        root_page.on("request", lambda request: request_timings.append({"event": "request", "url": request.url, "at": time.perf_counter()}))
        root_page.on("response", lambda response: request_timings.append({"event": "response", "url": response.url, "status": response.status, "at": time.perf_counter()}))
        planner_state = install_ai_planner_mock(root_page)
        login(root_page, args.base_url, "root", args.root_password)

        settings = api_fetch(
            root_page,
            "PUT",
            "/api/admin/settings",
            {
                "feature_ai_agent_enabled": True,
                "module_ai_agent_min_role": "user",
                "ai_agent_provider": "openai_compatible",
                "ai_agent_api_base_url": "http://127.0.0.1:9",
                "ai_agent_api_key": "sk-img-probe",
                "ai_agent_model": "qa-router",
                "ai_agent_allowed_models": "qa-router",
                "ai_agent_allowed_tools": "write_comfyui_generate",
                "ai_agent_operation_mode": "write",
                "comfyui_connection_mode": "remote",
                "comfyui_remote_api_url": args.comfyui_api_url,
            },
        )
        tools = api_fetch(root_page, "GET", "/api/ai-agent/write-tools")
        tool_names = [tool.get("name") for tool in tools["body"].get("tools", [])]
        rec.add(
            "IMG-01",
            "AI Agent 設定與工具白名單",
            settings["status"] == 200 and tools["status"] == 200 and tool_names == ["write_comfyui_generate"],
            f"settings={settings['status']}, tools={tool_names}",
            {"settings": settings["body"], "tools": tools["body"]},
        )

        models = api_fetch(root_page, "GET", "/api/comfyui/models")
        model_count = len(models["body"].get("models") or [])
        rec.add(
            "IMG-02",
            "遠端 ComfyUI 連線與模型",
            models["status"] == 200 and model_count > 0,
            f"status={models['status']}, models={model_count}",
            {"sample_models": (models["body"].get("models") or [])[:5], "msg": models["body"].get("msg")},
        )

        open_ai_agent(root_page, args.base_url)
        before_writes = len(write_requests)
        before_messages = thread_messages(root_page)
        t0 = time.perf_counter()
        send_ai_text(root_page, "請用 ComfyUI 生圖一張 AI agent 產圖測試圖，並持續追蹤進度")
        generation_text = wait_thread_any(root_page, ["ComfyUI 產圖已送出", "ComfyUI 產圖送出失敗"], timeout=60000)
        t_generation = time.perf_counter()
        after_writes = len(write_requests)
        job_id = extract_job_id(generation_text)
        timings["generation_submit_seconds"] = round(t_generation - t0, 3)
        timings["messages_added_for_generation"] = max(0, len(thread_messages(root_page)) - len(before_messages))
        last_write_payload = {}
        if write_requests:
            try:
                last_write_payload = json.loads(write_requests[-1].get("post_data") or "{}")
            except Exception:
                last_write_payload = {}
        rec.add(
            "IMG-03",
            "對話指令轉生圖",
            after_writes > before_writes
            and last_write_payload.get("tool") == "write_comfyui_generate"
            and last_write_payload.get("confirm") == "EXECUTE",
            f"write_requests={after_writes - before_writes}, job_id={job_id or '-'}, response_s={timings['generation_submit_seconds']}",
            {"payload": last_write_payload, "thread_tail": generation_text[-800:]},
        )

        before_poll_count = len(job_requests)
        root_page.wait_for_timeout(2500)
        watch_count = root_page.evaluate("""() => typeof AI_AGENT_STATE === "object" ? Object.keys(AI_AGENT_STATE.comfyuiWatchJobs || {}).length : 0""")
        latest_job = api_fetch(root_page, "GET", f"/api/comfyui/jobs/{job_id}") if job_id else {"status": 0, "body": {}}
        timings["job_poll_requests_after_submit"] = len(job_requests) - before_poll_count
        rec.add(
            "IMG-04",
            "Job ID 與進度追蹤",
            bool(job_id) and latest_job["status"] == 200 and latest_job["body"].get("ok") and len(job_requests) >= 1,
            f"job_id={job_id or '-'}, watch_jobs={watch_count}, job_polls={len(job_requests)}, poll_status={latest_job['status']}",
            {"job": latest_job["body"], "job_requests": job_requests[-5:]},
        )

        send_button_enabled = root_page.locator("#ai-agent-send-btn").is_enabled()
        input_enabled = root_page.locator("#ai-agent-input").is_enabled()
        t_followup0 = time.perf_counter()
        send_ai_text(root_page, "產圖期間也請查 CPU RAM disk 資源狀態")
        followup_text = wait_thread_any(root_page, ["資源：CPU", "唯讀查詢"], timeout=25000)
        timings["followup_readonly_seconds"] = round(time.perf_counter() - t_followup0, 3)
        rec.add(
            "IMG-05",
            "非阻塞",
            send_button_enabled and input_enabled and ("資源：CPU" in followup_text or "唯讀查詢" in followup_text),
            f"send_button={send_button_enabled}, input={input_enabled}, response_s={timings['followup_readonly_seconds']}",
            {"followup_tail": followup_text[-800:]},
        )

        bad = api_fetch(root_page, "PUT", "/api/admin/settings", {"comfyui_connection_mode": "remote", "comfyui_remote_api_url": "http://127.0.0.1:9"})
        t_failure0 = time.perf_counter()
        send_ai_text(root_page, "請用 ComfyUI 生圖一張失敗處理測試")
        failure_text = wait_thread_any(root_page, ["ComfyUI 產圖送出失敗", "目前無法讀取 ComfyUI checkpoint 清單"], timeout=30000)
        timings["failure_response_seconds"] = round(time.perf_counter() - t_failure0, 3)
        rec.add(
            "IMG-06",
            "失敗處理",
            bad["status"] == 200 and ("送出失敗" in failure_text or "無法讀取" in failure_text),
            f"bad_setting={bad['status']}, response_s={timings['failure_response_seconds']}",
            {"failure_tail": failure_text[-900:]},
        )

        restored = api_fetch(root_page, "PUT", "/api/admin/settings", {"comfyui_connection_mode": "remote", "comfyui_remote_api_url": args.comfyui_api_url})
        restored_models = api_fetch(root_page, "GET", "/api/comfyui/models")
        retry = execute_tool(
            root_page,
            "write_comfyui_generate",
            {
                "prompt": f"AI agent retry after failure probe {run_id}",
                "negative_prompt": "low quality",
                "width": 512,
                "height": 512,
                "steps": 1,
                "cfg_scale": 3,
                "batch_size": 1,
                "confirm_billing": True,
                "timeout_seconds": 120,
            },
        )
        rec.add(
            "IMG-07",
            "恢復/重送",
            restored["status"] == 200
            and restored_models["status"] == 200
            and len(restored_models["body"].get("models") or []) > 0
            and retry["status"] == 200
            and retry["body"].get("ok"),
            f"restore={restored['status']}, models={len(restored_models['body'].get('models') or [])}, retry={retry['status']}",
            {"retry": retry["body"]},
        )

        test_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        test_page = test_ctx.new_page()
        login(test_page, args.base_url, "test", args.test_password)
        member_tools = api_fetch(test_page, "GET", "/api/ai-agent/write-tools")
        member_execute = execute_tool(
            test_page,
            "write_comfyui_generate",
            {"prompt": "member should not execute", "width": 512, "height": 512, "steps": 1},
        )
        rec.add(
            "IMG-08",
            "權限邊界",
            member_tools["status"] == 403 and member_execute["status"] == 403,
            f"member_tools={member_tools['status']}, member_execute={member_execute['status']}",
            {"member_tools": member_tools["body"], "member_execute": member_execute["body"]},
        )
        test_ctx.close()

        completion_observed = False
        completion_text = ""
        if job_id:
            deadline = time.time() + 120
            while time.time() < deadline:
                status = api_fetch(root_page, "GET", f"/api/comfyui/jobs/{job_id}")
                job = (status["body"].get("job") or {}) if status["status"] == 200 else {}
                if str(job.get("status") or "").lower() in {"completed", "error", "failed", "cancelled"}:
                    root_page.wait_for_timeout(2500)
                    completion_text = thread_text(root_page)
                    completion_observed = "ComfyUI 產圖完成" in completion_text or "ComfyUI 產圖失敗" in completion_text
                    break
                root_page.wait_for_timeout(3000)
        rec.add(
            "IMG-09",
            "產物回報",
            completion_observed,
            "completed/error message observed" if completion_observed else "job still queued/running during probe window",
            {"job_id": job_id, "thread_tail": completion_text[-900:] if completion_text else thread_text(root_page)[-900:]},
        )

        final_messages = thread_messages(root_page)
        anomaly = thread_anomaly_metrics(final_messages)
        job_request_times = [item["at"] for item in request_timings if item["event"] == "request" and "/api/comfyui/jobs/" in item["url"]]
        poll_intervals = [
            round(job_request_times[idx] - job_request_times[idx - 1], 3)
            for idx in range(1, len(job_request_times))
        ]
        timings["job_poll_intervals_seconds"] = poll_intervals
        timings["total_probe_dialogue_messages"] = anomaly["message_count"]
        rec.add(
            "IMG-10",
            "回應時間與跳針/異常狀態",
            anomaly["empty_messages"] == 0
            and anomaly["repeated_adjacent"] == 0
            and anomaly["repeated_total"] <= 2
            and not anomaly["duplicate_progress_snapshots"]
            and not anomaly["progress_regressions"]
            and timings.get("generation_submit_seconds", 999) < 60
            and timings.get("followup_readonly_seconds", 999) < 25
            and timings.get("failure_response_seconds", 999) < 30,
            (
                f"submit_s={timings.get('generation_submit_seconds')}, "
                f"followup_s={timings.get('followup_readonly_seconds')}, "
                f"failure_s={timings.get('failure_response_seconds')}, "
                f"repeated_adjacent={anomaly['repeated_adjacent']}, repeated_total={anomaly['repeated_total']}"
            ),
            {"anomaly": anomaly, "timings": timings, "messages_tail": final_messages[-8:]},
        )

        root_ctx.close()
        browser.close()

    filtered_console_errors = [
        item for item in console_errors
        if "favicon" not in item.lower()
        and "the server responded with a status of 400" not in item.lower()
        and "the server responded with a status of 409" not in item.lower()
    ]
    result = {
        "ok": not rec.failures,
        "run_id": run_id,
        "base_url": args.base_url,
        "comfyui_api_url": args.comfyui_api_url,
        "rows": rec.rows,
        "failures": rec.failures,
        "planner_state": planner_state,
        "timings": timings,
        "write_requests": write_requests,
        "job_requests": job_requests,
        "console_errors": console_errors,
        "unexpected_console_errors": filtered_console_errors,
        "page_errors": page_errors,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "rows": len(rec.rows), "failures": len(rec.failures), "out": str(out)}, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
