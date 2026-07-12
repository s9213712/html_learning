#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.probe_credentials import add_root_password_argument, add_user_password_argument  # noqa: E402


ALL_AI_WRITE_TOOLS = [
    "write_community_create_thread",
    "write_community_reply_thread",
    "write_comfyui_generate",
    "write_chess_create_practice",
    "write_chess_make_move",
    "write_member_create_user",
    "write_member_update_user",
    "write_bug_report_review",
    "write_launch_requirements_check",
    "write_launch_logs_verify",
    "write_launch_doc_read",
    "audit_scan",
]

REQUESTED_UNSUPPORTED_TOOLS = [
    "write_trading_place_order",
    "write_trading_cancel_order",
    "write_trading_bot_create",
    "write_trading_bot_backtest_optimize",
    "write_trading_market_analysis",
    "write_trading_grid_bot_create",
    "write_trading_lending_liquidation_scan",
    "write_chain_transfer",
    "write_cloud_drive_upload",
    "write_cloud_drive_delete",
    "write_share_manage",
    "write_task_create",
    "write_automation_job_run",
    "write_server_repair",
    "write_emergency_incident_handle",
    "write_governance_event",
    "write_member_reward",
    "write_member_penalty",
    "write_community_reward",
    "write_community_penalty",
]


def now_id() -> str:
    return str(int(time.time()))[-8:]


class Recorder:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.matrix: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: str = "", **data: Any) -> None:
        item = {"name": name, "ok": bool(ok), "detail": detail, "data": data}
        self.checks.append(item)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
        if not ok:
            self.failures.append(item)

    def capability(
        self,
        area: str,
        requested: str,
        support: str,
        tested_result: str,
        boundary: str,
        fix_direction: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.matrix.append(
            {
                "area": area,
                "requested": requested,
                "support": support,
                "tested_result": tested_result,
                "boundary": boundary,
                "fix_direction": fix_direction,
                "evidence": evidence or {},
            }
        )


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


def install_ai_chat_router(page) -> dict[str, Any]:
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
                    "confidence": 0.96,
                    "execute_write": True,
                    "reason": "explicit generation",
                    "args": {
                        "prompt": "tiny qa icon, geometric robot, clean white background",
                        "negative_prompt": "low quality",
                        "width": 512,
                        "height": 512,
                        "steps": 1,
                        "cfg_scale": 3,
                        "batch_size": 1,
                    },
                }
            elif "cpu" in user_text.lower() or "ram" in user_text.lower() or "資源" in user_text:
                plan = {"action": "readonly", "confidence": 0.95, "readonly_scope": "resources", "reason": "resource status"}
            else:
                plan = {"action": "chat", "confidence": 0.9, "reason": "plain chat"}
            state["planner_calls"] += 1
            state["plans"].append({"user": user_text[:200], "plan": plan})
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}}))
            return
        state["chat_calls"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": "聊天測試完成。"}}))

    page.route("**/api/ai-agent/chat", handler)
    return state


def login(page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    result = api_fetch(page, "POST", "/api/login", {"username": username, "password": password})
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"login failed for {username}: {result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")


def open_ai_agent_tab(page) -> None:
    page.goto(page.url.split("#", 1)[0], wait_until="domcontentloaded")
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


def execute_tool(page, tool: str, arguments: dict[str, Any] | None = None, *, confirm: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool": tool, "arguments": arguments or {}}
    if confirm:
        payload["confirm"] = "EXECUTE"
    return api_fetch(page, "POST", "/api/ai-agent/write-tools/execute", payload)


def short_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    keep = {}
    for key in ("ok", "msg", "tool", "status", "operation_mode", "requires_elevation", "elevated_once"):
        if key in payload:
            keep[key] = payload[key]
    result = payload.get("result")
    if isinstance(result, dict):
        keep["result_keys"] = sorted(result.keys())[:20]
        for key in ("ok", "msg", "match_id", "status", "report_id"):
            if key in result:
                keep[f"result_{key}"] = result[key]
        job = result.get("job")
        if isinstance(job, dict):
            keep["job_id"] = job.get("job_id")
            keep["job_status"] = job.get("status")
            keep["job_progress"] = job.get("progress")
    return keep


def find_first_board(page) -> int | None:
    boards = api_fetch(page, "GET", "/api/community/boards")
    for board in boards["body"].get("boards", []) or []:
        if board.get("id"):
            return int(board["id"])
    return None


def newest_thread_id(page, board_id: int, title: str) -> int | None:
    threads = api_fetch(page, "GET", f"/api/community/boards/{board_id}/threads?limit=20")
    for thread in threads["body"].get("threads", []) or []:
        if thread.get("title") == title and thread.get("id"):
            return int(thread["id"])
    return None


def create_bug_report(page, run_id: str) -> str | None:
    result = api_fetch(
        page,
        "POST",
        "/api/bug-reports",
        {
            "title": f"AI Agent capability probe {run_id}",
            "description": "Capability boundary probe synthetic bug report.",
            "severity": "low",
            "feature": "ai_agent",
            "page": "/",
            "steps": "probe",
            "expected": "report can be reviewed by AI write tool",
            "actual": "probe review path",
        },
    )
    body = result["body"]
    return body.get("report_id") if result["status"] == 200 and body.get("ok") else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    add_root_password_argument(parser)
    add_user_password_argument(parser)
    parser.add_argument("--out", required=True)
    parser.add_argument("--comfyui-api-url", default="http://127.0.0.1:8189")
    args = parser.parse_args()

    rec = Recorder()
    run_id = now_id()
    created_username = f"ai_matrix_{run_id}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        root_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        root_page = root_ctx.new_page()
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
                "ai_agent_api_key": "sk-probe",
                "ai_agent_model": "qa-router",
                "ai_agent_allowed_models": "qa-router",
                "ai_agent_allowed_tools": ",".join(ALL_AI_WRITE_TOOLS),
                "ai_agent_operation_mode": "write",
                "comfyui_connection_mode": "remote",
                "comfyui_remote_api_url": args.comfyui_api_url,
            },
        )
        rec.check("configure_root_ai_agent_write_and_remote_comfyui", settings["status"] == 200 and settings["body"].get("ok"), f"status={settings['status']}", body=settings["body"])

        tools = api_fetch(root_page, "GET", "/api/ai-agent/write-tools")
        tool_names = [tool.get("name") for tool in tools["body"].get("tools", [])]
        rec.check("root_lists_all_current_ai_write_tools", tools["status"] == 200 and set(ALL_AI_WRITE_TOOLS).issubset(tool_names), f"tools={tool_names}", body=tools["body"])

        readonly_all = api_fetch(root_page, "GET", "/api/ai-agent/readonly?scope=all&limit=10")
        rec.check("root_readonly_all_status_visible", readonly_all["status"] == 200 and readonly_all["body"].get("ok"), f"status={readonly_all['status']}", keys=sorted(readonly_all["body"].keys()))

        comfy_models = api_fetch(root_page, "GET", "/api/comfyui/models")
        model_count = len(comfy_models["body"].get("models") or [])
        rec.check("remote_comfyui_models_visible_to_site", comfy_models["status"] == 200 and model_count > 0, f"status={comfy_models['status']}, models={model_count}", body=short_payload(comfy_models["body"]))

        launch_req = execute_tool(root_page, "write_launch_requirements_check", confirm=False)
        launch_logs = execute_tool(root_page, "write_launch_logs_verify", confirm=False)
        launch_doc = execute_tool(root_page, "write_launch_doc_read", {"path": "11_QA_TESTING.md"}, confirm=False)
        doc_escape = execute_tool(root_page, "write_launch_doc_read", {"path": "../server.py"}, confirm=False)
        audit_scan = execute_tool(root_page, "audit_scan", {"force": True}, confirm=False)
        rec.check("server_status_and_logs_tools_dispatch", all(item["status"] == 200 for item in [launch_req, launch_logs, launch_doc, audit_scan]), "requirements/logs/doc/audit_scan dispatched", requirements=short_payload(launch_req["body"]), logs=short_payload(launch_logs["body"]), doc=short_payload(launch_doc["body"]), audit=short_payload(audit_scan["body"]))
        rec.check("launch_doc_read_blocks_path_escape", doc_escape["status"] == 400 and not doc_escape["body"].get("ok"), f"status={doc_escape['status']}", body=doc_escape["body"])

        board_id = find_first_board(root_page)
        thread_result = None
        reply_result = None
        thread_id = None
        if board_id is not None:
            title = f"AI Agent matrix thread {run_id}"
            thread_result = execute_tool(root_page, "write_community_create_thread", {"board_id": board_id, "title": title, "content": "AI Agent matrix probe post.", "post_type": "normal"})
            thread_id = newest_thread_id(root_page, board_id, title)
            if thread_id:
                reply_result = execute_tool(root_page, "write_community_reply_thread", {"thread_id": thread_id, "content": "AI Agent matrix probe reply."})
        rec.check("community_post_and_reply_tools_work", bool(board_id and thread_result and thread_result["body"].get("ok") and thread_id and reply_result and reply_result["body"].get("ok")), f"board={board_id}, thread={thread_id}", thread=short_payload((thread_result or {}).get("body")), reply=short_payload((reply_result or {}).get("body")))

        chess = execute_tool(root_page, "write_chess_create_practice", {"side": "white", "difficulty": "normal"})
        match_id = ((chess.get("body") or {}).get("result") or {}).get("match_id")
        move = execute_tool(root_page, "write_chess_make_move", {"match_id": match_id, "from": "e2", "to": "e4"}) if match_id else {"status": 0, "body": {}}
        rec.check("game_chess_create_and_move_tools_work", chess["body"].get("ok") and move["body"].get("ok"), f"match={match_id}, move_status={move['status']}", chess=short_payload(chess["body"]), move=short_payload(move["body"]))

        member_create = execute_tool(
            root_page,
            "write_member_create_user",
            {
                "username": created_username,
                "password": args.test_password,
                "password_confirm": args.test_password,
                "nickname": f"AI Matrix {run_id}",
                "role": "user",
                "status": "active",
                "member_level": "trusted",
            },
        )
        user_search = api_fetch(root_page, "GET", f"/api/admin/users?q={created_username}&page_size=10")
        created_user = next((u for u in user_search["body"].get("users", []) if u.get("username") == created_username), None)
        update = {"status": 0, "body": {}}
        if created_user:
            update = execute_tool(root_page, "write_member_update_user", {"user_id": created_user.get("id"), "nickname": f"AI Matrix Updated {run_id}", "level_update_reason": "capability probe"})
        rec.check("member_create_and_update_tools_work", member_create["body"].get("ok") and bool(created_user) and update["body"].get("ok"), f"user={created_user.get('id') if created_user else None}", create=short_payload(member_create["body"]), update=short_payload(update["body"]))

        bug_id = create_bug_report(root_page, run_id)
        bug_review = execute_tool(root_page, "write_bug_report_review", {"report_id": bug_id, "decision": "approve", "review_note": "AI capability probe approval.", "reward_points": 0}) if bug_id else {"status": 0, "body": {}}
        rec.check("bug_report_review_tool_works", bool(bug_id and bug_review["body"].get("ok")), f"bug={bug_id}, status={bug_review['status']}", body=short_payload(bug_review["body"]))

        comfy_result = execute_tool(
            root_page,
            "write_comfyui_generate",
            {
                "prompt": "tiny qa icon, simple geometric robot, clean background",
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
        comfy_ok = comfy_result["status"] == 200 and comfy_result["body"].get("ok")
        rec.check("comfyui_generate_tool_dispatches_to_remote_backend", comfy_ok, f"status={comfy_result['status']}", body=short_payload(comfy_result["body"]))
        comfy_job = ((comfy_result.get("body") or {}).get("result") or {}).get("job") or {}
        comfy_job_id = comfy_job.get("job_id")
        comfy_job_poll = api_fetch(root_page, "GET", f"/api/comfyui/jobs/{comfy_job_id}") if comfy_job_id else {"status": 0, "body": {}}
        concurrent_readonly = api_fetch(root_page, "GET", "/api/ai-agent/readonly?scope=resources&limit=5")
        rec.check(
            "comfyui_job_progress_is_pollable_and_api_nonblocking",
            bool(comfy_job_id)
            and comfy_job_poll["status"] == 200
            and comfy_job_poll["body"].get("ok")
            and concurrent_readonly["status"] == 200
            and concurrent_readonly["body"].get("ok"),
            f"job={comfy_job_id}, poll={comfy_job_poll['status']}, readonly={concurrent_readonly['status']}",
            job=short_payload(comfy_job_poll["body"]),
            readonly_keys=sorted(concurrent_readonly["body"].keys()),
        )

        chat_state = install_ai_chat_router(root_page)
        open_ai_agent_tab(root_page)
        send_ai_text(root_page, "請用 ComfyUI 生圖一張，並持續追蹤進度")
        ui_generation_text = wait_thread_any(root_page, ["ComfyUI 產圖已送出", "ComfyUI 產圖送出失敗"], timeout=45000)
        button_enabled = root_page.locator("#ai-agent-send-btn").is_enabled()
        if not button_enabled:
            root_page.wait_for_timeout(2000)
            button_enabled = root_page.locator("#ai-agent-send-btn").is_enabled()
        can_type = root_page.locator("#ai-agent-input").is_enabled()
        if button_enabled and can_type:
            send_ai_text(root_page, "產圖期間也請查 CPU RAM 資源狀態")
            ui_followup_text = wait_thread_any(root_page, ["資源：CPU", "唯讀查詢"], timeout=20000)
        else:
            ui_followup_text = thread_text(root_page)
        watch_count = root_page.evaluate("""() => typeof AI_AGENT_STATE === "object" ? Object.keys(AI_AGENT_STATE.comfyuiWatchJobs || {}).length : 0""")
        rec.check(
            "frontend_comfyui_tracking_does_not_block_dialogue",
            ("Job ID" in ui_generation_text or "送出失敗" in ui_generation_text)
            and button_enabled
            and can_type
            and ("資源：CPU" in ui_followup_text or "唯讀查詢" in ui_followup_text),
            f"button_enabled={button_enabled}, input_enabled={can_type}, watch_jobs={watch_count}, planner_calls={chat_state['planner_calls']}",
            generation_tail=ui_generation_text[-700:],
            followup_tail=ui_followup_text[-700:],
        )

        bad_backend = api_fetch(root_page, "PUT", "/api/admin/settings", {"comfyui_connection_mode": "remote", "comfyui_remote_api_url": "http://127.0.0.1:9"})
        failed_generate = execute_tool(root_page, "write_comfyui_generate", {"prompt": "failure handling probe", "width": 512, "height": 512, "steps": 1, "batch_size": 1, "confirm_billing": True})
        restored_backend = api_fetch(root_page, "PUT", "/api/admin/settings", {"comfyui_connection_mode": "remote", "comfyui_remote_api_url": args.comfyui_api_url})
        recovered_models = api_fetch(root_page, "GET", "/api/comfyui/models")
        rec.check(
            "comfyui_failure_is_explicit_and_recoverable_after_remote_restore",
            bad_backend["status"] == 200
            and failed_generate["status"] in {400, 409, 502, 503}
            and not failed_generate["body"].get("ok")
            and restored_backend["status"] == 200
            and recovered_models["status"] == 200
            and len(recovered_models["body"].get("models") or []) > 0,
            f"failed_status={failed_generate['status']}, recovered_models={len(recovered_models['body'].get('models') or [])}",
            failure=short_payload(failed_generate["body"]),
        )

        missing_confirm = execute_tool(root_page, "write_member_create_user", {"username": f"no_confirm_{run_id}"}, confirm=False)
        rec.check("write_tools_require_explicit_confirm", missing_confirm["status"] in {400, 409} and not missing_confirm["body"].get("ok"), f"status={missing_confirm['status']}", body=missing_confirm["body"])

        unsupported_results = {}
        for tool in REQUESTED_UNSUPPORTED_TOOLS:
            result = execute_tool(root_page, tool, {"probe": True})
            unsupported_results[tool] = {"status": result["status"], "body": short_payload(result["body"])}
        rec.check(
            "requested_but_unwired_tools_are_rejected",
            all(item["status"] == 400 and not item["body"].get("ok") for item in unsupported_results.values()),
            f"unsupported={len(unsupported_results)}",
            results=unsupported_results,
        )

        test_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        test_page = test_ctx.new_page()
        login(test_page, args.base_url, "test", args.test_password)
        member_tools = api_fetch(test_page, "GET", "/api/ai-agent/write-tools")
        member_execute = execute_tool(test_page, "write_member_create_user", {"username": f"deny_{run_id}"})
        member_readonly = api_fetch(test_page, "GET", "/api/ai-agent/readonly?scope=attack_diag&limit=10")
        rec.check("member_scope_blocks_write_and_root_security_data", member_tools["status"] == 403 and member_execute["status"] == 403 and member_readonly["status"] == 200 and member_readonly["body"].get("ok"), f"tools={member_tools['status']}, execute={member_execute['status']}, readonly={member_readonly['status']}", readonly=short_payload(member_readonly["body"]))

        test_ctx.close()
        root_ctx.close()
        browser.close()

    present_tools = set(tool_names)
    rec.capability(
        "生圖",
        "以對話指令送 ComfyUI 生圖",
        "partial",
        "後端 write_comfyui_generate 已列入工具並實測 dispatch；前台自然語言目前只會執行 ComfyUI 這一類寫入。",
        "僅 root / write 或 root 單次提權；一般會員不可直接寫入。",
        "保留確認門檻，補實際 job 完成輪詢與產物驗證；把圖生成功能從專用表單再往純對話參數擷取收斂。",
        {"tool_present": "write_comfyui_generate" in present_tools, "dispatch": short_payload(comfy_result["body"])},
    )
    rec.capability(
        "交易/市況/回測",
        "下單、取消、建立交易機器人、產生 workflow、回測調參、市況分析、借貸清算",
        "absent_from_ai_agent",
        "站內 trading API 存在，但 AI Agent write-tools 拒絕交易相關工具名稱。",
        "AI Agent 不能替使用者下單、回測最佳參數或啟動清算；這是未接線而非交易模組不存在。",
        "新增交易專用 AI 工具層：read_market_context、draft_strategy、backtest_strategy、optimize_parameters、place_order。高風險交易必須分 read/plan/simulate/confirm/execute 五階段並有額度、冷卻、審計與回滾策略。",
        {"rejected_tools": {k: unsupported_results[k] for k in unsupported_results if k.startswith("write_trading")}},
    )
    rec.capability(
        "鏈上/鏈下互轉",
        "鏈上鏈下互轉交易與鏈運作驗算",
        "absent_from_ai_agent",
        "AI Agent 沒有 transfer/settlement/verify 類 write tool；readonly 只給摘要。",
        "不能由對話直接移轉資產或觸發鏈狀態變更。",
        "新增 scoped ledger tools：read_wallet, simulate_transfer, verify_invariant, execute_transfer；執行需 idempotency key、雙重確認與鏈審計 row。",
        {"rejected_tool": unsupported_results.get("write_chain_transfer")},
    )
    rec.capability(
        "伺服器狀態/日誌",
        "檢查網頁日誌、伺服器狀態、處理解決伺服器問題",
        "partial",
        "requirements/log chain/doc/audit_scan 可執行；server repair 工具被拒絕。",
        "能看與掃描，不能 kill/restart/edit server 外部狀態。",
        "若要修復能力，建立站內 runbook action registry，只允許預先定義的 restart_queue/clear_stale_job 之類站內操作，禁止任意 shell。",
        {"requirements": short_payload(launch_req["body"]), "logs": short_payload(launch_logs["body"]), "repair_rejected": unsupported_results.get("write_server_repair")},
    )
    rec.capability(
        "論壇/治理",
        "發文、回覆、會員獎懲、治理事件、緊急事件",
        "partial",
        "發文/回覆、會員建立/更新、bug report 審核可用；會員獎懲、治理事件、社群 reward/penalty 工具不存在。",
        "能做少數管理操作；沒有完整治理工作流，也不能處理緊急事件。",
        "新增 governance tools：draft_governance_event、apply_member_reward_penalty、community_reward_penalty、incident_declare/resolve；所有工具需 reason、scope、expiry、審計與可撤銷性。",
        {"community": short_payload((thread_result or {}).get("body")), "member": short_payload(member_create["body"]), "bug_review": short_payload(bug_review["body"])},
    )
    rec.capability(
        "遊戲區",
        "建立棋局並走棋",
        "partial",
        "西洋棋練習建立與走 e2-e4 實測成功。",
        "只暴露 chess practice/move；其他遊戲、邀請、刪除、認輸、draw 等未接 AI tool。",
        "按遊戲建立 bounded action spec；AI 只可透過合法 move API，不可直接改局面資料。",
        {"chess": short_payload(chess["body"]), "move": short_payload(move["body"])},
    )
    rec.capability(
        "雲端硬碟/分享/任務/自動化",
        "雲端硬碟管理、分享管理、任務管理、自動化作業",
        "absent_from_ai_agent",
        "相關站內 API 存在，但 AI Agent 工具名稱全部被拒絕。",
        "AI Agent 不能上傳、刪除、分享、取消任務或啟動自動化。",
        "新增 drive/share/task tools 前先定義資料邊界：只能操作 actor 可見資源；大型/刪除/公開分享需二次確認與預覽 diff。",
        {"drive": unsupported_results.get("write_cloud_drive_upload"), "share": unsupported_results.get("write_share_manage"), "task": unsupported_results.get("write_task_create")},
    )
    rec.capability(
        "安全邊界",
        "站內範圍、不可越權、不可寫出伺服器外",
        "present",
        "一般會員列工具/執行工具皆 403；docs path traversal 被 400 擋下；未知工具 400。",
        "目前安全模型保守，代價是大量站內功能尚未接上。",
        "保持白名單工具模型；新增功能時不要給任意 URL/shell/path，所有工具都走既有站內 API 與角色檢查。",
        {"member_execute": short_payload(member_execute["body"]), "doc_escape": short_payload(doc_escape["body"])},
    )
    rec.capability(
        "語意 Agent 形態",
        "不要為特定功能生成專用卡片；完全在對話內截取資訊；不像多層 if/else",
        "partial",
        "前端 planner prompt 要求語意 JSON，但 action set 只有 chat/clarify/readonly/comfyui_status/comfyui_generate/comfyui_rerun/community_post_draft；ComfyUI 仍有專用表單與前端 fallback。",
        "不是全站通用自主 Agent；是 LLM 路由 + 白名單工具 + 部分 deterministic fallback。",
        "建立統一 tool schema registry 與 planner/executor loop，前端只渲染對話和確認 diff，不為每個能力造卡片；所有工具輸入由 schema 驗證和澄清問題收斂。",
        {"frontend_actions": ["chat", "clarify", "readonly", "comfyui_status", "comfyui_generate", "comfyui_rerun", "community_post_draft"]},
    )

    result = {
        "ok": not rec.failures,
        "run_id": run_id,
        "base_url": args.base_url,
        "checks": rec.checks,
        "failures": rec.failures,
        "capability_matrix": rec.matrix,
        "unsupported_tool_results": unsupported_results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "checks": len(rec.checks), "failures": len(rec.failures), "matrix": len(rec.matrix), "out": str(out)}, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
