#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def now_id() -> str:
    return str(int(time.time()))[-8:]


class Recorder:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str = "", **data: Any) -> None:
        item = {"name": name, "ok": bool(ok), "detail": detail, "data": data}
        self.checks.append(item)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
        if not ok:
            self.failures.append(item)


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


def login(page, base_url: str, username: str, password: str) -> dict[str, Any]:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    result = api_fetch(page, "POST", "/api/login", {"username": username, "password": password})
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"login failed for {username}: {result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")
    return result


def install_ai_chat_router(page, recorder: Recorder) -> dict[str, Any]:
    state = {"planner_calls": 0, "chat_calls": 0, "plans": []}

    def plan_for(text: str) -> dict[str, Any]:
        lowered = text.lower()
        if "cpu" in lowered or "ram" in lowered or "資源" in text:
            return {"action": "readonly", "confidence": 0.95, "readonly_scope": "resources", "reason": "resource status"}
        if "攻擊" in text or "安全事件" in text or "attack" in lowered:
            return {"action": "readonly", "confidence": 0.95, "readonly_scope": "attack_diag", "reason": "security diagnostics"}
        if "生圖" in text or "comfyui" in lowered:
            return {
                "action": "comfyui_generate",
                "confidence": 0.96,
                "execute_write": True,
                "reason": "explicit image generation",
                "args": {
                    "prompt": "front-end qa tiny robot, clean line art",
                    "negative_prompt": "low quality",
                    "width": 512,
                    "height": 512,
                    "steps": 1,
                    "cfg_scale": 3,
                    "batch_size": 1,
                    "official_workflow_id": "origin_sdxl_txt2img",
                },
            }
        if "不完整" in text or "幫我處理" in text:
            return {"action": "clarify", "confidence": 0.92, "question": "請補充你希望我查詢、寫入或排錯哪一個功能。"}
        if "聊天" in text or "hello" in lowered:
            return {"action": "chat", "confidence": 0.9, "reason": "plain chat"}
        return {"action": "readonly", "confidence": 0.7, "readonly_scope": "all", "reason": "fallback status"}

    def handler(route, request):
        try:
            payload = request.post_data_json or {}
        except Exception:
            payload = {}
        messages = payload.get("messages") if isinstance(payload, dict) else []
        first = messages[0].get("content") if messages and isinstance(messages[0], dict) else ""
        if isinstance(first, list):
            first_text = "\n".join(str(item.get("text", "")) for item in first if isinstance(item, dict))
        else:
            first_text = str(first or "")
        is_planner = "工具路由器" in first_text and "context=" in first_text
        if is_planner:
            match = re.search(r"\nuser=(.*)$", first_text, flags=re.S)
            user_text = match.group(1).strip() if match else first_text
            plan = plan_for(user_text)
            state["planner_calls"] += 1
            state["plans"].append({"user": user_text[:300], "plan": plan})
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
            body=json.dumps({"ok": True, "message": {"role": "assistant", "content": "一般聊天回覆完成：我會維持在允許的站內範圍內。"}}),
        )

    page.route("**/api/ai-agent/chat", handler)
    return state


def wait_thread_contains(page, text: str, timeout: int = 15000) -> str:
    page.locator("#ai-agent-thread").wait_for(state="visible", timeout=timeout)
    page.wait_for_function(
        """needle => (document.querySelector('#ai-agent-thread')?.innerText || '').includes(needle)""",
        arg=text,
        timeout=timeout,
    )
    return page.locator("#ai-agent-thread").inner_text(timeout=timeout)


def wait_thread_contains_any(page, needles: list[str], timeout: int = 15000) -> str:
    page.locator("#ai-agent-thread").wait_for(state="visible", timeout=timeout)
    page.wait_for_function(
        """needles => {
          const text = document.querySelector('#ai-agent-thread')?.innerText || '';
          return needles.some((needle) => text.includes(needle));
        }""",
        arg=needles,
        timeout=timeout,
    )
    return page.locator("#ai-agent-thread").inner_text(timeout=timeout)


def send_ai_text(page, text: str) -> None:
    page.fill("#ai-agent-input", text)
    page.click("#ai-agent-send-btn")


def open_ai_agent_tab(page) -> None:
    page.locator("#tab-module-ai-agent").wait_for(state="visible", timeout=20000)
    page.click("#tab-module-ai-agent")
    page.locator("#module-ai-agent.active").wait_for(state="visible", timeout=15000)
    page.locator("#ai-agent-input").wait_for(state="visible", timeout=15000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--root-password", required=True)
    parser.add_argument("--manager-password", required=True)
    parser.add_argument("--test-password", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rec = Recorder()
    run_id = now_id()
    write_username = f"ai_front_{run_id}"
    unsupported_tool = "write_member_delete_user"
    root_requests: list[dict[str, Any]] = []
    test_requests: list[dict[str, Any]] = []
    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        root_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
        root_page = root_ctx.new_page()
        root_page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        root_page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        root_page.on("dialog", lambda dialog: dialog.accept())
        root_chat_state = install_ai_chat_router(root_page, rec)
        root_page.on(
            "request",
            lambda request: root_requests.append({"url": request.url, "post_data": request.post_data})
            if "/api/ai-agent/write-tools/execute" in request.url
            else None,
        )

        login(root_page, args.base_url, "root", args.root_password)
        settings_payload = {
            "feature_ai_agent_enabled": True,
            "module_ai_agent_min_role": "user",
            "ai_agent_provider": "openai_compatible",
            "ai_agent_api_base_url": "http://127.0.0.1:9",
            "ai_agent_api_key": "sk-frontqa",
            "ai_agent_model": "qa-router",
            "ai_agent_allowed_models": "qa-router,qwen3-vl",
            "ai_agent_allowed_tools": ",".join([
                "write_comfyui_generate",
                "write_chess_create_practice",
                "write_member_create_user",
                "write_member_update_user",
                "audit_scan",
                "write_launch_logs_verify",
                "write_launch_requirements_check",
            ]),
            "ai_agent_operation_mode": "assist",
            "ai_agent_task_site_guide": True,
            "ai_agent_task_troubleshoot": True,
            "ai_agent_task_prompt": True,
        }
        saved = api_fetch(root_page, "PUT", "/api/admin/settings", settings_payload)
        rec.add("root_configures_ai_agent_scope", saved["status"] == 200 and saved["body"].get("ok"), f"status={saved['status']}", body=saved["body"])

        root_page.goto(args.base_url + "/", wait_until="domcontentloaded")
        open_ai_agent_tab(root_page)
        root_page.evaluate("""() => typeof loadAiAgentStatus === 'function' ? loadAiAgentStatus({force: true}) : null""")
        root_page.wait_for_function(
            """() => (document.querySelector('#ai-agent-operation-mode-state')?.innerText || '').includes('協助')""",
            timeout=15000,
        )
        mode_state = root_page.locator("#ai-agent-operation-mode-state").inner_text()
        tools_state = root_page.locator("#ai-agent-effective-tools").inner_text()
        panel_hidden = root_page.locator("#ai-agent-write-tools-panel").evaluate("el => el.hidden && el.getAttribute('aria-hidden') === 'true'")
        generate_enabled = root_page.locator("#ai-agent-comfyui-generate-btn").is_enabled()
        rec.add("root_frontend_ai_agent_loaded", "協助" in mode_state and "write_comfyui_generate" in tools_state, mode_state, tools=tools_state[:500])
        rec.add("internal_write_panel_hidden_but_comfyui_attempt_enabled", panel_hidden and generate_enabled, f"hidden={panel_hidden}, enabled={generate_enabled}")

        models_status = api_fetch(root_page, "GET", "/api/ai-agent/models")
        rec.add(
            "models_endpoint_degrades_without_frontend_5xx",
            models_status["status"] == 200 and models_status["body"].get("backend_unavailable") is True,
            f"status={models_status['status']}, ok={models_status['body'].get('ok')}",
            body=models_status["body"],
        )

        send_ai_text(root_page, "請查目前 CPU RAM disk 資源狀態")
        readonly_text = wait_thread_contains(root_page, "資源：CPU")
        rec.add("natural_language_readonly_resources_completed", "唯讀查詢：已直接讀取站內唯讀資料" in readonly_text, readonly_text[-600:])

        send_ai_text(root_page, "幫我處理一下不完整需求")
        clarify_text = wait_thread_contains(root_page, "請補充你希望我查詢")
        rec.add("natural_language_clarify_stays_non_writing", "請補充" in clarify_text, clarify_text[-500:])

        send_ai_text(root_page, "hello 一般聊天")
        chat_text = wait_thread_contains(root_page, "一般聊天回覆完成")
        rec.add("natural_language_chat_fallback_completed", "一般聊天回覆完成" in chat_text, chat_text[-500:])

        before_write_requests = len(root_requests)
        send_ai_text(root_page, "請用 ComfyUI 生圖一張前台 QA 小機器人")
        comfy_text = wait_thread_contains_any(root_page, ["write_comfyui_generate", "ComfyUI 產圖送出失敗", "ComfyUI 產圖已送出"], timeout=25000)
        after_write_requests = len(root_requests)
        last_write_body = {}
        if root_requests:
            try:
                last_write_body = json.loads(root_requests[-1].get("post_data") or "{}")
            except Exception:
                last_write_body = {}
        rec.add(
            "natural_language_comfyui_attempt_uses_confirmed_elevation",
            after_write_requests > before_write_requests
            and last_write_body.get("tool") == "write_comfyui_generate"
            and last_write_body.get("confirm") == "EXECUTE"
            and last_write_body.get("elevate_once") == "ALLOW_WRITE_ONCE",
            f"requests={after_write_requests - before_write_requests}, thread_tail={comfy_text[-400:]}",
            body=last_write_body,
        )

        write_settings = api_fetch(root_page, "PUT", "/api/admin/settings", {"ai_agent_operation_mode": "write"})
        rec.add("root_switches_ai_agent_write_mode", write_settings["status"] == 200 and write_settings["body"].get("ok"), f"status={write_settings['status']}", body=write_settings["body"])
        tools_list = api_fetch(root_page, "GET", "/api/ai-agent/write-tools")
        tool_names = [tool.get("name") for tool in tools_list["body"].get("tools", [])]
        rec.add("root_write_tools_list_scoped", tools_list["status"] == 200 and "write_member_create_user" in tool_names and unsupported_tool not in tool_names, f"tools={tool_names}")

        chess = api_fetch(
            root_page,
            "POST",
            "/api/ai-agent/write-tools/execute",
            {
                "tool": "write_chess_create_practice",
                "arguments": {"side": "white", "difficulty": "normal"},
                "confirm": "EXECUTE",
            },
        )
        rec.add("root_write_tool_chess_practice_completed", chess["status"] == 200 and chess["body"].get("ok"), f"status={chess['status']}", body=chess["body"])

        create_member = api_fetch(
            root_page,
            "POST",
            "/api/ai-agent/write-tools/execute",
            {
                "tool": "write_member_create_user",
                "arguments": {
                    "username": write_username,
                    "password": args.test_password,
                    "password_confirm": args.test_password,
                    "nickname": f"AI Front {run_id}",
                    "role": "user",
                    "status": "active",
                    "member_level": "trusted",
                },
                "confirm": "EXECUTE",
            },
        )
        user_search = api_fetch(root_page, "GET", f"/api/admin/users?q={write_username}&page_size=10")
        user_found = any(user.get("username") == write_username for user in user_search["body"].get("users", []))
        rec.add("root_write_tool_member_create_completed", create_member["status"] == 200 and create_member["body"].get("ok") and user_found, f"status={create_member['status']}, found={user_found}", body=create_member["body"])

        missing_confirm = api_fetch(
            root_page,
            "POST",
            "/api/ai-agent/write-tools/execute",
            {"tool": "write_member_create_user", "arguments": {"username": f"bad_{run_id}"}},
        )
        rec.add("write_tool_requires_explicit_confirm", missing_confirm["status"] in {400, 409}, f"status={missing_confirm['status']}", body=missing_confirm["body"])

        unsupported = api_fetch(
            root_page,
            "POST",
            "/api/ai-agent/write-tools/execute",
            {"tool": unsupported_tool, "arguments": {"user_id": 3}, "confirm": "EXECUTE"},
        )
        rec.add("unsupported_or_destructive_tool_rejected", unsupported["status"] == 400 and not unsupported["body"].get("ok"), f"status={unsupported['status']}", body=unsupported["body"])

        test_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        test_page = test_ctx.new_page()
        test_chat_state = install_ai_chat_router(test_page, rec)
        test_page.on(
            "request",
            lambda request: test_requests.append({"url": request.url, "post_data": request.post_data})
            if "/api/ai-agent/write-tools/execute" in request.url
            else None,
        )
        login(test_page, args.base_url, "test", args.test_password)
        open_ai_agent_tab(test_page)
        test_page.wait_for_function(
            """() => (document.querySelector('#ai-agent-operation-mode-state')?.innerText || '').length > 0""",
            timeout=15000,
        )
        send_ai_text(test_page, "請看攻擊診斷和安全事件")
        test_diag_text = wait_thread_contains(test_page, "安全審計完整資料限 root", timeout=15000)
        rec.add("member_readonly_attack_diag_is_scoped", "安全審計完整資料限 root" in test_diag_text, test_diag_text[-500:])

        before_member_write = len(test_requests)
        send_ai_text(test_page, "請用 ComfyUI 生圖一張普通會員測試圖")
        member_comfy_text = wait_thread_contains(test_page, "需要 root 身分", timeout=15000)
        rec.add(
            "member_natural_language_write_denied_before_api_call",
            len(test_requests) == before_member_write and "需要 root 身分" in member_comfy_text,
            f"write_requests={len(test_requests) - before_member_write}, tail={member_comfy_text[-400:]}",
        )
        member_execute = api_fetch(
            test_page,
            "POST",
            "/api/ai-agent/write-tools/execute",
            {"tool": "write_member_create_user", "arguments": {"username": f"deny_{run_id}"}, "confirm": "EXECUTE"},
        )
        rec.add("member_write_tool_api_forbidden", member_execute["status"] == 403 and not member_execute["body"].get("ok"), f"status={member_execute['status']}", body=member_execute["body"])

        rec.add("planner_was_used_for_frontend_commands", root_chat_state["planner_calls"] >= 4 and test_chat_state["planner_calls"] >= 2, f"root={root_chat_state['planner_calls']}, test={test_chat_state['planner_calls']}", root_plans=root_chat_state["plans"], test_plans=test_chat_state["plans"])
        rec.add("frontend_no_page_errors", not page_errors, "; ".join(page_errors[:3]), errors=page_errors)
        filtered_console_errors = [
            msg for msg in console_errors
            if "favicon" not in msg.lower()
            and "the server responded with a status of 400" not in msg.lower()
        ]
        rec.add("frontend_no_unexpected_console_errors", not filtered_console_errors, "; ".join(filtered_console_errors[:5]), errors=filtered_console_errors)

        test_ctx.close()
        root_ctx.close()
        browser.close()

    result = {
        "ok": not rec.failures,
        "run_id": run_id,
        "base_url": args.base_url,
        "checks": rec.checks,
        "failures": rec.failures,
        "events": rec.events,
        "root_write_requests": root_requests,
        "test_write_requests": test_requests,
        "console_errors": console_errors,
        "page_errors": page_errors,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "checks": len(rec.checks), "failures": len(rec.failures), "out": str(out)}, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
