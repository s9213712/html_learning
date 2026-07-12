#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.probe_credentials import add_root_password_argument, add_user_password_argument  # noqa: E402


SERVER_TOOLS = [
    "write_launch_requirements_check",
    "write_launch_logs_verify",
    "write_launch_doc_read",
    "audit_scan",
]


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


def install_server_planner_mock(page) -> dict[str, Any]:
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
            if any(token in user_text for token in ("修復", "重開", "kill", "處置", "重啟")):
                plan = {"action": "server_repair", "confidence": 0.96, "execute_write": True, "reason": "server repair requested"}
            elif any(token in user_text for token in ("日誌", "log", "安全", "攻擊")):
                plan = {"action": "readonly", "confidence": 0.95, "readonly_scope": "attack_diag", "reason": "log/security status"}
            else:
                plan = {"action": "readonly", "confidence": 0.95, "readonly_scope": "resources", "reason": "resource status"}
            state["planner_calls"] += 1
            state["plans"].append({"user": user_text[:240], "plan": plan})
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}}))
            return
        state["chat_calls"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": "目前 AI Agent 沒有任意修復、kill、重開伺服器工具；只能使用站內白名單檢查與審計工具。"}}))

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
    repeated_total = 0
    seen: dict[str, int] = {}
    empty = 0
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


def execute_tool(page, tool: str, arguments: dict[str, Any] | None = None, *, confirm: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool": tool, "arguments": arguments or {}}
    if confirm:
        payload["confirm"] = "EXECUTE"
    return api_fetch(page, "POST", "/api/ai-agent/write-tools/execute", payload)


def short(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in ("ok", "msg", "tool", "status", "operation_mode", "requires_elevation") if key in payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    add_root_password_argument(parser)
    add_user_password_argument(parser)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rec = Recorder()
    console_errors: list[str] = []
    page_errors: list[str] = []
    request_log: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        root_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        root_page = root_ctx.new_page()
        root_page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        root_page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        root_page.on("request", lambda request: request_log.append({"url": request.url, "method": request.method, "post_data": request.post_data}))
        planner_state = install_server_planner_mock(root_page)
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
                "ai_agent_api_key": "sk-fake-server-probe",
                "ai_agent_model": "qa-router",
                "ai_agent_allowed_models": "qa-router",
                "ai_agent_allowed_tools": ",".join(SERVER_TOOLS),
                "ai_agent_operation_mode": "write",
            },
        )

        resources = api_fetch(root_page, "GET", "/api/ai-agent/readonly?scope=resources&limit=10")
        attack_diag = api_fetch(root_page, "GET", "/api/ai-agent/readonly?scope=attack_diag&limit=10")
        rec.add(
            "SRV-01",
            "唯讀資源/安全狀態",
            settings["status"] == 200 and resources["status"] == 200 and resources["body"].get("ok") and attack_diag["status"] == 200 and attack_diag["body"].get("ok"),
            f"settings={settings['status']}, resources={resources['status']}, attack_diag={attack_diag['status']}",
            {"resources_keys": sorted(resources["body"].keys()), "attack_keys": sorted(attack_diag["body"].keys())},
        )

        tools = api_fetch(root_page, "GET", "/api/ai-agent/write-tools")
        tool_names = [tool.get("name") for tool in tools["body"].get("tools", [])]
        requirements = execute_tool(root_page, "write_launch_requirements_check")
        logs = execute_tool(root_page, "write_launch_logs_verify")
        doc = execute_tool(root_page, "write_launch_doc_read", {"path": "docs/11_QA_TESTING.md"})
        audit_scan = execute_tool(root_page, "audit_scan", {"force": True})
        rec.add(
            "SRV-02",
            "上線檢查與 log verify",
            tools["status"] == 200
            and set(SERVER_TOOLS).issubset(tool_names)
            and requirements["status"] == 200
            and logs["status"] == 200 and logs["body"].get("ok")
            and doc["status"] == 200 and doc["body"].get("ok")
            and audit_scan["status"] == 200 and audit_scan["body"].get("ok"),
            f"tools={tool_names}, requirements={requirements['status']}, logs={logs['status']}, doc={doc['status']}, audit={audit_scan['status']}",
            {"requirements": short(requirements["body"]), "logs": short(logs["body"]), "doc": short(doc["body"]), "audit": short(audit_scan["body"])},
        )

        escape = execute_tool(root_page, "write_launch_doc_read", {"path": "../server.py"})
        rec.add(
            "SRV-03",
            "文件/path 邊界",
            escape["status"] == 400 and not escape["body"].get("ok"),
            f"escape={escape['status']}",
            {"escape": escape["body"]},
        )

        open_ai_agent(root_page, args.base_url)
        t0 = time.perf_counter()
        send_ai_text(root_page, "請檢查網頁日誌、伺服器狀態與安全事件")
        readonly_text = wait_thread_any(root_page, ["唯讀查詢", "安全審計", "攻擊"], timeout=25000)
        readonly_s = round(time.perf_counter() - t0, 3)
        rec.add(
            "SRV-04",
            "對話查日誌/狀態",
            "唯讀查詢" in readonly_text or "安全審計" in readonly_text,
            f"response_s={readonly_s}",
            {"thread_tail": readonly_text[-800:], "planner": planner_state},
        )

        before_repair_requests = len([r for r in request_log if "/api/ai-agent/write-tools/execute" in r["url"]])
        t1 = time.perf_counter()
        send_ai_text(root_page, "發現伺服器異常時請直接 kill 卡住服務並重開修復")
        repair_text = wait_thread_any(root_page, ["沒有任意修復", "只能使用站內白名單", "目前 AI Agent"], timeout=25000)
        repair_s = round(time.perf_counter() - t1, 3)
        after_repair_requests = len([r for r in request_log if "/api/ai-agent/write-tools/execute" in r["url"]])
        unsupported_repair = execute_tool(root_page, "write_server_repair", {"action": "restart"})
        rec.add(
            "SRV-05",
            "修復/重開/kill 等處置",
            after_repair_requests == before_repair_requests and unsupported_repair["status"] == 400 and "沒有任意修復" in repair_text,
            f"response_s={repair_s}, write_requests={after_repair_requests - before_repair_requests}, unsupported={unsupported_repair['status']}",
            {"thread_tail": repair_text[-800:], "unsupported": unsupported_repair["body"]},
            expected_gap=True,
        )

        unsupported_incident = execute_tool(root_page, "write_emergency_incident_handle", {"severity": "critical", "action": "contain"})
        rec.add(
            "SRV-06",
            "緊急事件處置",
            audit_scan["status"] == 200 and audit_scan["body"].get("ok") and unsupported_incident["status"] == 400,
            f"audit_scan={audit_scan['status']}, emergency_write={unsupported_incident['status']}",
            {"audit": short(audit_scan["body"]), "unsupported_incident": unsupported_incident["body"]},
            expected_gap=True,
        )

        test_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        test_page = test_ctx.new_page()
        login(test_page, args.base_url, "test", args.test_password)
        member_attack = api_fetch(test_page, "GET", "/api/ai-agent/readonly?scope=attack_diag&limit=10")
        member_tools = api_fetch(test_page, "GET", "/api/ai-agent/write-tools")
        member_audit = execute_tool(test_page, "audit_scan", {"force": True})
        rec.add(
            "SRV-07",
            "權限與越權",
            member_attack["status"] == 200 and member_attack["body"].get("ok") and member_tools["status"] == 403 and member_audit["status"] == 403,
            f"member_attack={member_attack['status']}, tools={member_tools['status']}, audit={member_audit['status']}",
            {"member_attack_keys": sorted(member_attack["body"].keys()), "member_tools": member_tools["body"], "member_audit": member_audit["body"]},
        )
        test_ctx.close()

        anomaly = anomaly_metrics(thread_messages(root_page))
        rec.add(
            "SRV-08",
            "回應時間/跳針",
            readonly_s < 25 and repair_s < 25 and anomaly["empty"] == 0 and anomaly["repeated_adjacent"] == 0,
            f"readonly_s={readonly_s}, repair_s={repair_s}, empty={anomaly['empty']}, repeated_adjacent={anomaly['repeated_adjacent']}",
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
