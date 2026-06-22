# AI Agent Function Audit: Trading

Date: 2026-06-22
Target: `https://127.0.0.1:54384`
Artifact: `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_trading_capability_probe_v1.json`

## Scope

This pass audited trading, trading bots, workflow/backtest, market context, background trading jobs, matching, and liquidation scans as a single functional area. It separates "site trading API works" from "AI Agent can operate it".

## Result Table

| ID | Item | Result | Evidence |
| --- | --- | --- | --- |
| TRD-01 | Site trading functions exist | PASS | `markets`, `dashboard`, `workflow-templates`, root background status, and bot-audit dashboard all returned 200. Observed 5 markets and 11 workflow templates. |
| TRD-02 | AI Agent trading tool allowlist | GAP | AI Agent exposed 12 tools, but 0 trading tools. Attempting to configure `write_trading_place_order` was rejected with 400 as an unknown tool. |
| TRD-03 | Dialogue request for order/bot workflow | GAP | Natural-language request for BTC/USDT workflow, backtest optimization, and order execution caused no trading/write request. Response completed in 0.551s and did not claim success. |
| TRD-04 | Direct trading write-tool names | GAP | 9/9 proposed trading tool names were rejected as unsupported write tools. |
| TRD-05 | Backtest / parameter search base capability | PASS | Direct site backtest API worked on synthetic DCA candles: 6 trades, `return_percent=0.1`. AI Agent is not wired to use it. |
| TRD-06 | Background bot / liquidation / matching | PASS | Direct root APIs for liquidation scan, order matching, and bot audit run returned 200. AI Agent is not wired to use them. |
| TRD-07 | Permission and overreach boundary | PASS | normal member got 403 on root trading background status and 403 on AI write-tool endpoint. |
| TRD-08 | Response time / repetition | PASS | Trading intent response took 0.551s; no empty message or adjacent repeat was observed. |

## Capability Boundary

- Trading module itself is operational enough for market reads, workflow template reads, synthetic bot backtest, background status, liquidation scan, order matching, and bot audit.
- AI Agent currently cannot place/cancel orders, create trading bots, generate executable workflow templates, run parameter optimization, execute market analysis as a structured tool, trigger background trading jobs, match orders, or scan liquidations.
- This is a product design gap, not a failure of the underlying trading APIs.
- The current safety boundary is conservative: unknown trading write tools are rejected, normal users cannot access root trading operations, and the Agent does not silently claim that trades were executed.

## Fix Direction

Add trading AI tools in phases:

1. Read-only tools: `read_trading_market_context`, `read_trading_dashboard`, `read_trading_background_status`, `read_trading_bot_audit`.
2. Simulation tools: `simulate_trading_order`, `backtest_trading_strategy`, `optimize_trading_parameters`.
3. Draft tools: `draft_trading_workflow`, `draft_grid_bot`, `draft_dca_bot`.
4. Root/member write tools with strict confirmation: `write_trading_place_order`, `write_trading_cancel_order`, `write_trading_bot_create`, `write_trading_background_run_once`, `write_trading_liquidation_scan`.

Execution should use a staged policy: read -> draft -> simulate/backtest -> explain risk -> require confirmation -> execute through existing site APIs. Every write needs idempotency keys, budget limits, role checks, audit rows, and clear rollback/undo messaging where possible.

## Next Function

Recommended next scoped audit: server status, logs, server issue handling, and emergency response boundaries.
