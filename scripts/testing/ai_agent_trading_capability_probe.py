#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


AI_AGENT_NON_TRADING_TOOLS = [
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

TRADING_TOOL_CANDIDATES = [
    "write_trading_place_order",
    "write_trading_cancel_order",
    "write_trading_bot_create",
    "write_trading_bot_backtest_optimize",
    "write_trading_market_analysis",
    "write_trading_grid_bot_create",
    "write_trading_lending_liquidation_scan",
    "write_trading_background_run_once",
    "write_trading_order_match",
]


def now_id() -> str:
    return str(int(time.time()))[-8:]


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def add(self, case_id: str, item: str, ok: bool, result: str, evidence: dict[str, Any] | None = None, *, expected_gap: bool = False) -> None:
        row = {
            "case_id": case_id,
            "item": item,
            "ok": bool(ok),
            "expected_gap": bool(expected_gap),
            "result": result,
            "evidence": evidence or {},
        }
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


def install_trading_planner_mock(page) -> dict[str, Any]:
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
            plan = {
                "action": "trading_bot_backtest_optimize",
                "confidence": 0.97,
                "execute_write": True,
                "reason": "user requested trading workflow/backtest/order",
                "args": {"market_symbol": "BTC/USDT", "strategy": "dca", "objective": "best_return_with_drawdown_limit"},
            }
            state["planner_calls"] += 1
            state["plans"].append({"user": user_text[:240], "plan": plan})
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}}))
            return
        state["chat_calls"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "message": {"role": "assistant", "content": "目前 AI Agent 尚未接上交易下單、機器人 workflow 或回測最佳化工具；我不能假裝已完成交易操作。"}}))

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


def synthetic_candles() -> list[dict[str, Any]]:
    rows = []
    base = 100_000
    for idx in range(12):
        close = base + (idx * 450) + (200 if idx % 3 == 0 else -120)
        rows.append(
            {
                "time": f"2026-01-01T{idx:02d}:00:00Z",
                "open_points": close - 100,
                "high_points": close + 250,
                "low_points": max(1, close - 350),
                "close_points": close,
                "volume": 1,
            }
        )
    return rows


def execute_ai_tool(page, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return api_fetch(page, "POST", "/api/ai-agent/write-tools/execute", {"tool": tool, "arguments": arguments or {}, "confirm": "EXECUTE"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--root-password", required=True)
    parser.add_argument("--test-password", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rec = Recorder()
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
        planner_state = install_trading_planner_mock(root_page)
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
                "ai_agent_api_key": "sk-trading-probe",
                "ai_agent_model": "qa-router",
                "ai_agent_allowed_models": "qa-router",
                "ai_agent_allowed_tools": ",".join(AI_AGENT_NON_TRADING_TOOLS),
                "ai_agent_operation_mode": "write",
            },
        )
        markets = api_fetch(root_page, "GET", "/api/trading/markets")
        dashboard = api_fetch(root_page, "GET", "/api/trading/dashboard")
        templates = api_fetch(root_page, "GET", "/api/trading/workflow-templates")
        benchmarks = api_fetch(root_page, "GET", "/api/trading/workflow-template-benchmarks")
        background = api_fetch(root_page, "GET", "/api/root/trading/background/status")
        bot_audit = api_fetch(root_page, "GET", "/api/root/trading/bot-audit/dashboard")
        rec.add(
            "TRD-01",
            "站內交易功能存在",
            all(item["status"] == 200 and item["body"].get("ok", True) for item in [markets, dashboard, templates, benchmarks, background, bot_audit]),
            f"markets={markets['status']}, dashboard={dashboard['status']}, templates={templates['status']}, background={background['status']}, bot_audit={bot_audit['status']}",
            {
                "market_count": len(markets["body"].get("markets") or []),
                "template_count": len(templates["body"].get("templates") or []),
                "background_keys": sorted(background["body"].keys()),
            },
        )

        tools = api_fetch(root_page, "GET", "/api/ai-agent/write-tools")
        tool_names = [tool.get("name") for tool in tools["body"].get("tools", [])]
        invalid_setting = api_fetch(root_page, "PUT", "/api/admin/settings", {"ai_agent_allowed_tools": "write_trading_place_order"})
        api_fetch(root_page, "PUT", "/api/admin/settings", {"ai_agent_allowed_tools": ",".join(AI_AGENT_NON_TRADING_TOOLS)})
        rec.add(
            "TRD-02",
            "AI Agent 交易工具白名單",
            tools["status"] == 200
            and not any(name in tool_names for name in TRADING_TOOL_CANDIDATES)
            and invalid_setting["status"] == 400,
            f"tools={len(tool_names)}, trading_tools=0, invalid_setting={invalid_setting['status']}",
            {"tool_names": tool_names, "invalid_setting": invalid_setting["body"]},
            expected_gap=True,
        )

        open_ai_agent(root_page, args.base_url)
        before_trade_requests = len([r for r in request_log if "/api/trading/" in r["url"] or "/api/root/trading/" in r["url"] or "/api/ai-agent/write-tools/execute" in r["url"]])
        t0 = time.perf_counter()
        send_ai_text(root_page, "幫我產生 BTC/USDT 交易機器人 workflow，自己回測找最佳參數，如果符合條件就下單")
        thread = wait_thread_any(root_page, ["尚未接上交易", "不能假裝", "目前 AI Agent"], timeout=25000)
        response_s = round(time.perf_counter() - t0, 3)
        after_trade_requests = len([r for r in request_log if "/api/trading/" in r["url"] or "/api/root/trading/" in r["url"] or "/api/ai-agent/write-tools/execute" in r["url"]])
        rec.add(
            "TRD-03",
            "對話要求下單/機器人 workflow",
            after_trade_requests == before_trade_requests and "不能假裝" in thread,
            f"response_s={response_s}, trading_or_write_requests={after_trade_requests - before_trade_requests}",
            {"thread_tail": thread[-800:], "planner": planner_state},
            expected_gap=True,
        )

        unsupported = {tool: execute_ai_tool(root_page, tool, {"market_symbol": "BTC/USDT"}) for tool in TRADING_TOOL_CANDIDATES}
        rec.add(
            "TRD-04",
            "直接交易 write tool 名稱",
            all(result["status"] == 400 and not result["body"].get("ok") for result in unsupported.values()),
            f"rejected={sum(1 for result in unsupported.values() if result['status'] == 400)}/{len(unsupported)}",
            {"unsupported": {tool: {"status": result["status"], "body": result["body"]} for tool, result in unsupported.items()}},
            expected_gap=True,
        )

        backtest = api_fetch(
            root_page,
            "POST",
            "/api/trading/bots/backtest",
            {
                "market_symbol": "BTC/USDT",
                "strategy": "dca",
                "initial_cash_points": 10_000,
                "order_points": 100,
                "interval_candles": 2,
                "timeframe": "1h",
                "candles": synthetic_candles(),
            },
        )
        rec.add(
            "TRD-05",
            "回測/最佳參數",
            backtest["status"] == 200 and backtest["body"].get("ok"),
            f"backtest={backtest['status']}, trades={backtest['body'].get('trade_count')}",
            {"backtest_keys": sorted(backtest["body"].keys()), "return_percent": backtest["body"].get("return_percent")},
        )

        liquidation = api_fetch(root_page, "POST", "/api/root/trading/liquidations/scan", {"limit": 5})
        match = api_fetch(root_page, "POST", "/api/root/trading/orders/match", {"market_symbol": "BTC/USDT", "limit": 5})
        audit_run = api_fetch(root_page, "POST", "/api/root/trading/bot-audit/run", {"limit": 5, "force": True})
        rec.add(
            "TRD-06",
            "背景 bot/清算/撮合",
            all(item["status"] == 200 and item["body"].get("ok", True) for item in [liquidation, match, audit_run]),
            f"liquidation={liquidation['status']}, match={match['status']}, audit={audit_run['status']}",
            {
                "liquidation": {k: liquidation["body"].get(k) for k in ("ok", "scanned", "candidates", "liquidated", "errors")},
                "match": {k: match["body"].get(k) for k in ("ok", "matched", "errors")},
                "audit": {k: audit_run["body"].get(k) for k in ("ok", "audited", "skipped")},
            },
        )

        test_ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        test_page = test_ctx.new_page()
        login(test_page, args.base_url, "test", args.test_password)
        member_root_bg = api_fetch(test_page, "GET", "/api/root/trading/background/status")
        member_ai_tool = execute_ai_tool(test_page, "write_trading_place_order", {"market_symbol": "BTC/USDT"})
        rec.add(
            "TRD-07",
            "權限與越權",
            member_root_bg["status"] == 403 and member_ai_tool["status"] == 403,
            f"member_root_bg={member_root_bg['status']}, member_ai_tool={member_ai_tool['status']}",
            {"member_root_bg": member_root_bg["body"], "member_ai_tool": member_ai_tool["body"]},
        )
        test_ctx.close()

        anomaly = anomaly_metrics(thread_messages(root_page))
        rec.add(
            "TRD-08",
            "回應時間/跳針",
            response_s < 25 and anomaly["empty"] == 0 and anomaly["repeated_adjacent"] == 0,
            f"response_s={response_s}, empty={anomaly['empty']}, repeated_adjacent={anomaly['repeated_adjacent']}",
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
