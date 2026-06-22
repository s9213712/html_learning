#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


AI_TOOLS = [
    "write_community_create_thread",
    "write_community_reply_thread",
    "write_member_create_user",
    "write_member_update_user",
    "write_bug_report_review",
]

GOV_TOOL_CANDIDATES = [
    "write_member_reward",
    "write_member_penalty",
    "write_community_reward",
    "write_community_penalty",
    "write_governance_proposal_create",
    "write_governance_vote",
    "write_governance_execute",
    "write_emergency_governance_action",
]


def now_id() -> str:
    return str(int(time.time()))[-8:]


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


def find_board(page) -> int | None:
    boards = api_fetch(page, "GET", "/api/community/boards")
    for board in boards["body"].get("boards", []) or []:
        if board.get("id"):
            return int(board["id"])
    return None


def newest_thread_id(page, board_id: int, title: str) -> int | None:
    threads = api_fetch(page, "GET", f"/api/community/boards/{board_id}/threads?limit=20")
    for item in threads["body"].get("threads", []) or []:
        if item.get("title") == title and item.get("id"):
            return int(item["id"])
    return None


def create_bug_report(page, run_id: str) -> str | None:
    result = api_fetch(
        page,
        "POST",
        "/api/bug-reports",
        {
            "title": f"AI governance probe {run_id}",
            "description": "Synthetic bug report for AI Agent governance audit.",
            "severity": "low",
            "feature": "ai_agent_governance",
            "page": "/",
            "steps": "probe",
            "expected": "review tool can award points",
            "actual": "probe",
        },
    )
    return result["body"].get("report_id") if result["status"] == 200 and result["body"].get("ok") else None


def install_governance_planner_mock(page) -> dict[str, Any]:
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
            plan = {"action": "governance_reward_penalty", "confidence": 0.95, "execute_write": True, "reason": "member/community governance requested"}
            state["planner_calls"] += 1
            state["plans"].append({"user": user_text[:240], "plan": plan})
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}}))
            return
        state["chat_calls"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": "目前 AI Agent 尚未接上會員獎懲、社群 reward/penalty 或治理提案執行工具；不能假裝已完成治理處置。"}}))

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--root-password", required=True)
    parser.add_argument("--test-password", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rec = Recorder()
    run_id = now_id()
    username = f"ai_gov_{run_id}"
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
        planner_state = install_governance_planner_mock(root_page)
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
                "ai_agent_api_key": "sk-gov-probe",
                "ai_agent_model": "qa-router",
                "ai_agent_allowed_models": "qa-router",
                "ai_agent_allowed_tools": ",".join(AI_TOOLS),
                "ai_agent_operation_mode": "write",
            },
        )
        tools = api_fetch(root_page, "GET", "/api/ai-agent/write-tools")
        tool_names = [tool.get("name") for tool in tools["body"].get("tools", [])]
        rec.add(
            "GOV-01",
            "AI Agent 已接治理相關工具",
            settings["status"] == 200 and tools["status"] == 200 and set(AI_TOOLS).issubset(tool_names),
            f"tools={tool_names}",
            {"tools": tool_names},
        )

        board_id = find_board(root_page)
        title = f"AI governance probe thread {run_id}"
        thread = execute_ai_tool(root_page, "write_community_create_thread", {"board_id": board_id, "title": title, "content": "AI governance probe content.", "post_type": "normal"}) if board_id else {"status": 0, "body": {}}
        thread_id = newest_thread_id(root_page, board_id, title) if board_id else None
        reply = execute_ai_tool(root_page, "write_community_reply_thread", {"thread_id": thread_id, "content": "AI governance probe reply."}) if thread_id else {"status": 0, "body": {}}
        detail = api_fetch(root_page, "GET", f"/api/community/threads/{thread_id}") if thread_id else {"status": 0, "body": {}}
        post_id = None
        for post in detail["body"].get("posts", []) if isinstance(detail.get("body"), dict) else []:
            if post.get("content") == "AI governance probe reply.":
                post_id = int(post.get("id"))
                break
        rec.add(
            "GOV-02",
            "發文與回覆",
            bool(board_id and thread["body"].get("ok") and thread_id and reply["body"].get("ok") and post_id),
            f"board={board_id}, thread={thread_id}, post={post_id}",
            {"thread": thread["body"], "reply": reply["body"]},
        )

        penalty_post_id = None
        if thread_id:
            penalty_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
            penalty_page = penalty_ctx.new_page()
            login(penalty_page, args.base_url, "test", args.test_password)
            penalty_content = f"AI governance penalty target {run_id}"
            api_fetch(penalty_page, "POST", f"/api/community/threads/{thread_id}/posts", {"content": penalty_content})
            penalty_ctx.close()
            detail_after_penalty_post = api_fetch(root_page, "GET", f"/api/community/threads/{thread_id}")
            for post in detail_after_penalty_post["body"].get("posts", []) if isinstance(detail_after_penalty_post.get("body"), dict) else []:
                if post.get("content") == penalty_content:
                    penalty_post_id = int(post.get("id"))
                    break

        member = execute_ai_tool(
            root_page,
            "write_member_create_user",
            {
                "username": username,
                "password": args.test_password,
                "password_confirm": args.test_password,
                "nickname": f"AI Gov {run_id}",
                "role": "user",
                "status": "active",
                "member_level": "normal",
            },
        )
        users = api_fetch(root_page, "GET", f"/api/admin/users?q={username}&page_size=10")
        created = next((item for item in users["body"].get("users", []) if item.get("username") == username), None)
        update = execute_ai_tool(root_page, "write_member_update_user", {"user_id": created.get("id"), "base_level": "restricted", "sanction_status": "restricted", "level_update_reason": "AI governance probe restriction"}) if created else {"status": 0, "body": {}}
        rec.add(
            "GOV-03",
            "會員建立/懲處式更新",
            bool(member["body"].get("ok") and created and update["body"].get("ok")),
            f"user={created.get('id') if created else None}, update={update['status']}",
            {"member": member["body"], "update": update["body"]},
        )

        bug_id = create_bug_report(root_page, run_id)
        bug_review = execute_ai_tool(root_page, "write_bug_report_review", {"report_id": bug_id, "decision": "approve", "review_note": "AI governance probe reward.", "reward_points": 1}) if bug_id else {"status": 0, "body": {}}
        rec.add(
            "GOV-04",
            "Bug bounty 獎勵",
            bool(bug_id and bug_review["body"].get("ok")),
            f"bug={bug_id}, review={bug_review['status']}",
            {"review": bug_review["body"]},
        )

        community_reward = api_fetch(root_page, "POST", f"/api/community/threads/{thread_id}/reward", {"points": 1, "reason": "AI governance probe reward"}) if thread_id else {"status": 0, "body": {}}
        community_penalty = api_fetch(root_page, "POST", f"/api/community/posts/{penalty_post_id}/penalty", {"points": 1, "reason": "AI governance probe penalty"}) if penalty_post_id else {"status": 0, "body": {}}
        moderation_list = api_fetch(root_page, "GET", "/api/admin/moderation/proposals")
        rec.add(
            "GOV-05",
            "站內社群獎懲/治理 API 存在",
            community_reward["status"] == 200 and community_reward["body"].get("ok") and community_penalty["status"] == 200 and community_penalty["body"].get("ok") and moderation_list["status"] == 200,
            f"reward={community_reward['status']}, penalty={community_penalty['status']}, moderation={moderation_list['status']}",
            {"reward": community_reward["body"], "penalty": community_penalty["body"], "penalty_post_id": penalty_post_id, "moderation_keys": sorted(moderation_list["body"].keys()) if isinstance(moderation_list.get("body"), dict) else []},
        )

        unsupported = {tool: execute_ai_tool(root_page, tool, {"target_user_id": created.get("id") if created else 0, "thread_id": thread_id, "post_id": post_id}) for tool in GOV_TOOL_CANDIDATES}
        rec.add(
            "GOV-06",
            "AI Agent 未接社群獎懲/治理工具",
            all(result["status"] == 400 and not result["body"].get("ok") for result in unsupported.values()),
            f"rejected={sum(1 for result in unsupported.values() if result['status'] == 400)}/{len(unsupported)}",
            {"unsupported": {tool: {"status": result["status"], "body": result["body"]} for tool, result in unsupported.items()}},
            expected_gap=True,
        )

        open_ai_agent(root_page, args.base_url)
        before = len([r for r in request_log if "/api/ai-agent/write-tools/execute" in r["url"]])
        t0 = time.perf_counter()
        send_ai_text(root_page, "請獎勵這篇好文、懲處違規留言，並建立會員治理提案執行")
        thread_text = wait_thread_any(root_page, ["尚未接上會員獎懲", "不能假裝", "治理處置"], timeout=25000)
        elapsed = round(time.perf_counter() - t0, 3)
        after = len([r for r in request_log if "/api/ai-agent/write-tools/execute" in r["url"]])
        rec.add(
            "GOV-07",
            "對話治理要求不靜默成功",
            after == before and "不能假裝" in thread_text,
            f"response_s={elapsed}, write_requests={after - before}",
            {"thread_tail": thread_text[-800:], "planner": planner_state},
            expected_gap=True,
        )

        test_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        test_page = test_ctx.new_page()
        login(test_page, args.base_url, "test", args.test_password)
        member_tools = api_fetch(test_page, "GET", "/api/ai-agent/write-tools")
        member_gov = execute_ai_tool(test_page, "write_member_update_user", {"user_id": created.get("id") if created else 0, "base_level": "suspended"})
        rec.add(
            "GOV-08",
            "權限與越權",
            member_tools["status"] == 403 and member_gov["status"] == 403,
            f"member_tools={member_tools['status']}, member_gov={member_gov['status']}",
            {"member_tools": member_tools["body"], "member_gov": member_gov["body"]},
        )
        test_ctx.close()

        anomaly = anomaly_metrics(thread_messages(root_page))
        rec.add(
            "GOV-09",
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
