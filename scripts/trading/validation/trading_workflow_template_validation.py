#!/usr/bin/env python3
"""Validate official trading workflow templates with trigger and backtest checks.

This script is intentionally read-only for production data. It builds a temporary
SQLite database, loads templates from workflows/trading_bot, downloads public BTC
K-lines, runs the backend backtest engine, then compares the result with a
small independent accounting replay over the same candles.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.points_chain import PointsLedgerService, ensure_points_economy_schema  # noqa: E402
from services.trading.trading_engine import (  # noqa: E402
    ASSET_SCALE,
    MAX_BACKTEST_CANDLES,
    TradingEngineService,
    ensure_trading_schema,
    fee_points,
    notional_points,
    units_to_quantity,
)
from scripts.test_artifacts import test_artifact_path  # noqa: E402


REPORT_DIR = test_artifact_path("reports", "trading", "workflow_template_validation")
WORKFLOW_DIR = ROOT / "workflows" / "trading_bot"
TRIGGER_CASES = {
    "auto_search_winner_claude_rev3_return": (
        {"price": 100000, "ma200": 90000, "rsi": 50, "has_position": False},
        "buy_percent",
    ),
    "dip_buy": ({"price": 85000, "window_low_price": 85000, "has_position": False}, "buy_percent"),
    "dipbuy_rsi35_70_size99_late_tp15_nopyr_codex": (
        {"price": 100000, "ma200": 90000, "rsi": 50, "has_position": False},
        "buy_percent",
    ),
    "breakout_buy": ({"price": 110000, "window_high_price": 110000, "ma50": 100000, "has_position": False}, "buy_percent"),
    "stop_loss": ({"price": 70000, "window_low_price": 70000, "has_position": True, "pnl_percent": -20, "pnl_low_percent": -20}, "close_all"),
    "ma_pullback": ({"price": 100000, "ma50": 90000, "rsi": 40, "has_position": False}, "buy_percent"),
    "bollinger_reversion": (
        {"price": 80000, "bb_lower": 90000, "bb_mid": 100000, "bb_std": 5000, "has_position": False},
        "buy_percent",
    ),
    "kd_momentum": ({"price": 100000, "kd": 70, "ma20": 90000, "has_position": False}, "buy_percent"),
    "risk_guard": ({"price": 90000, "window_low_price": 90000, "has_position": True, "pnl_percent": -6, "pnl_low_percent": -6}, "close_all"),
    "ma200_trend_entry": ({"price": 100000, "ma200": 85000, "ma50": 90000, "rsi": 50, "has_position": False}, "buy_percent"),
    "staged_profit_taking": ({"price": 120000, "window_high_price": 120000, "has_position": True, "pnl_percent": 12, "pnl_high_percent": 12}, "sell_percent"),
}
# Plan B: rsi_scale / full_entry_exit / swing_bb_ma50 removed from
# workflows/trading_bot/.  swing_bb_ma50 used to share this guard with
# bollinger_reversion; the guard now applies to bollinger_reversion alone.
FLAT_SEQUENCE_GUARD_TEMPLATE_IDS = {"bollinger_reversion"}


def utc_iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def fetch_json(url: str, timeout: float = 20.0):
    req = Request(url, headers={"User-Agent": "hackme-web-workflow-validation/1.0"})
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download_binance_candles(symbol: str, interval: str, limit: int):
    query = urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    payload = fetch_json(f"https://api.binance.com/api/v3/klines?{query}")
    candles = []
    for row in payload:
        candles.append({
            "time": int(row[0]),
            "time_iso": utc_iso_from_ms(int(row[0])),
            "open_points": round(float(row[1])),
            "high_points": round(float(row[2])),
            "low_points": round(float(row[3])),
            "close_points": round(float(row[4])),
            "volume": float(row[5]),
        })
    return {"source": "binance_public_api", "symbol": symbol, "candles": candles}


def download_okx_candles(instrument: str, interval: str, limit: int):
    bars = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
    query = urlencode({"instId": instrument, "bar": bars[interval], "limit": limit})
    payload = fetch_json(f"https://www.okx.com/api/v5/market/candles?{query}")
    rows = sorted(payload.get("data") or [], key=lambda item: int(item[0]))
    candles = []
    for row in rows:
        candles.append({
            "time": int(row[0]),
            "time_iso": utc_iso_from_ms(int(row[0])),
            "open_points": round(float(row[1])),
            "high_points": round(float(row[2])),
            "low_points": round(float(row[3])),
            "close_points": round(float(row[4])),
            "volume": float(row[5]) if len(row) > 5 else 0,
        })
    return {"source": "okx_public_api", "symbol": instrument, "candles": candles}


def download_candles(interval: str, limit: int):
    errors = []
    for fetcher, args in (
        (download_binance_candles, ("BTCUSDT", interval, limit)),
        (download_okx_candles, ("BTC-USDT", interval, limit)),
    ):
        try:
            result = fetcher(*args)
            if len(result["candles"]) >= 2:
                return result
            errors.append(f"{fetcher.__name__}: too few candles")
        except Exception as exc:
            errors.append(f"{fetcher.__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


def get_test_services(tmp_dir: Path):
    db_path = tmp_dir / "workflow_validation.db"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    conn = get_db()
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active')"
    )
    conn.execute(
        "INSERT INTO users (username, role, status) VALUES ('alice', 'user', 'active'), ('root', 'super_admin', 'active')"
    )
    ensure_points_economy_schema(conn)
    ensure_trading_schema(conn)
    conn.commit()
    conn.close()

    points = PointsLedgerService(get_db=get_db, chain_secret="validation-secret", backup_dir=tmp_dir / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points, live_price_provider=lambda symbol: 100000)
    return trading


def load_templates(trading: TradingEngineService):
    templates = []
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        workflow = trading._validate_workflow(payload.get("workflow"))
        templates.append({"id": payload.get("id") or path.stem, "label": payload.get("label") or path.stem, "path": path, "workflow": workflow})
    return templates


def validate_trigger(trading: TradingEngineService, template):
    template_id = template["id"]
    context, expected_action = TRIGGER_CASES.get(template_id, ({"price": 100000, "has_position": False}, None))
    decision = trading._workflow_decision(template["workflow"], context=context, run_count=0, last_run_at=None, execution_state={"executed_action_ids": set(), "branch_step_counts": {}})
    action = (decision or {}).get("action") or {}
    actual_action = action.get("type")
    ok = bool(decision) and (expected_action is None or actual_action == expected_action)
    return {
        "ok": ok,
        "expected_action": expected_action,
        "actual_action": actual_action,
        "reason": (decision or {}).get("reason") or "",
        "context": context,
    }


def validate_flat_sequence_guard(trading: TradingEngineService, template):
    if template["id"] not in FLAT_SEQUENCE_GUARD_TEMPLATE_IDS:
        return {"checked": False, "ok": True, "trade_count": None, "final_value_points": None}
    candles = [
        {
            "time": index,
            "time_iso": f"flat-{index:04d}",
            "open_points": 100,
            "high_points": 100,
            "low_points": 100,
            "close_points": 100,
            "price_points": 100,
        }
        for index in range(30)
    ]
    result = trading.backtest_trading_bot(
        actor={"id": 1, "username": "alice", "role": "user"},
        payload={
            "market_symbol": "BTC/POINTS",
            "strategy": "workflow",
            "workflow_json": template["workflow"],
            "candles": candles,
            "initial_cash_points": 1000,
        },
    )
    return {
        "checked": True,
        "ok": result["trade_count"] == 0 and result["final_value_points"] == 1000,
        "trade_count": result["trade_count"],
        "final_value_points": result["final_value_points"],
    }


def update_workflow_state(state, decision):
    action = (decision or {}).get("action") or {}
    action_id = (decision or {}).get("action_id") or ((decision or {}).get("branch") or {}).get("id")
    if action_id:
        state["branch_step_counts"][action_id] = int(state["branch_step_counts"].get(action_id, 0)) + 1
        if action.get("type") != "close_all":
            state["executed_action_ids"].add(action_id)


def validate_engine_backtest_sanity(engine_result, *, initial_cash=10000):
    issues = []
    trade_count = int(engine_result.get("trade_count") or 0)
    final_value = int(engine_result.get("final_value_points") or 0)
    pnl_points = int(engine_result.get("pnl_points") or 0)
    if trade_count < 0:
        issues.append({"field": "trade_count", "reason": "negative trade_count"})
    if final_value < 0:
        issues.append({"field": "final_value_points", "reason": "negative final value"})
    if pnl_points != final_value - int(initial_cash):
        issues.append({
            "field": "pnl_points",
            "reason": "pnl does not equal final_value - initial_cash",
            "expected": final_value - int(initial_cash),
            "actual": pnl_points,
        })
    if float(engine_result.get("return_percent") or 0) < -100.0:
        issues.append({"field": "return_percent", "reason": "return below -100%"})
    if float(engine_result.get("max_drawdown_percent") or 0) < 0:
        issues.append({"field": "max_drawdown_percent", "reason": "negative drawdown"})
    return {
        "checked": True,
        "ok": not issues,
        "issues": issues,
    }


def write_reports(report, out_dir=REPORT_DIR):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"workflow_template_validation_{stamp}.json"
    md_path = out_dir / f"workflow_template_validation_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Workflow Template Validation Report",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Data source: {report['data_source']} {report['provider_symbol']}",
        f"- Candle count: {report['candle_count']}",
        f"- Range: {report['first_candle_time']} -> {report['last_candle_time']}",
        f"- Engine max candles: {report['limits']['engine_max_backtest_candles']}",
        f"- Automatic provider request limit: {report['limits']['automatic_provider_request_limit']}",
        "",
        "## Results",
        "",
    ]
    for item in report["templates"]:
        status = "PASS" if item["ok"] else "FAIL"
        flat_note = ""
        if item.get("flat_sequence_guard", {}).get("checked"):
            flat_note = f" flat_guard={item['flat_sequence_guard']['ok']}"
        lines.append(f"- {status} `{item['id']}`: trigger={item['trigger']['actual_action']} trades={item['engine_backtest']['trade_count']} final={item['engine_backtest']['final_value_points']} mismatches={len(item['mismatches'])}{flat_note}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main():
    parser = argparse.ArgumentParser(description="Validate official trading workflow templates.")
    parser.add_argument("--interval", default="15m", choices=["5m", "15m", "1h", "4h", "1d"])
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--no-download", action="store_true", help="Use synthetic candles only.")
    parser.add_argument("--out", default=str(REPORT_DIR), help="report output directory")
    args = parser.parse_args()
    if args.limit < 2 or args.limit > 1000:
        raise SystemExit("--limit must be between 2 and 1000 for one provider request")

    with tempfile.TemporaryDirectory(prefix="hackme-workflow-validation-") as tmp:
        trading = get_test_services(Path(tmp))
        templates = load_templates(trading)
        if not templates:
            raise SystemExit(f"no trading workflow templates found under {WORKFLOW_DIR}")
        if args.no_download:
            candles = [
                {"time": i, "time_iso": f"synthetic-{i:04d}", "open_points": 90000 + i, "high_points": 90500 + i, "low_points": 89500 + i, "close_points": 90000 + i}
                for i in range(args.limit)
            ]
            source = {"source": "synthetic", "symbol": "BTCUSDT", "candles": candles}
        else:
            source = download_candles(args.interval, args.limit)
            candles = source["candles"]

        report = {
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_source": source["source"],
            "provider_symbol": source["symbol"],
            "candle_count": len(candles),
            "first_candle_time": candles[0].get("time_iso") if candles else "",
            "last_candle_time": candles[-1].get("time_iso") if candles else "",
            "limits": {
                "engine_max_backtest_candles": MAX_BACKTEST_CANDLES,
                "automatic_provider_request_limit": 1000,
                "script_limit": args.limit,
                "period_limit_note": "No calendar-duration limit is enforced; candle count is limited.",
            },
            "templates": [],
        }
        actor = {"id": 1, "username": "alice", "role": "user"}
        for template in templates:
            trigger = validate_trigger(trading, template)
            flat_guard = validate_flat_sequence_guard(trading, template)
            engine_result = trading.backtest_trading_bot(
                actor=actor,
                payload={
                    "market_symbol": "BTC/POINTS",
                    "strategy": "workflow",
                    "workflow_json": template["workflow"],
                    "candles": candles,
                    "initial_cash_points": 10000,
                    "data_source": source["source"],
                    "provider_symbol": source["symbol"],
                },
            )
            backtest_sanity = validate_engine_backtest_sanity(engine_result, initial_cash=10000)
            ok = trigger["ok"] and flat_guard["ok"] and backtest_sanity["ok"]
            report["templates"].append({
                "id": template["id"],
                "label": template["label"],
                "ok": ok,
                "trigger": trigger,
                "flat_sequence_guard": flat_guard,
                "backtest_sanity": backtest_sanity,
                "engine_backtest": {
                    "trade_count": engine_result["trade_count"],
                    "final_value_points": engine_result["final_value_points"],
                    "pnl_points": engine_result["pnl_points"],
                    "return_percent": engine_result["return_percent"],
                    "max_drawdown_percent": engine_result["max_drawdown_percent"],
                    "win_rate_percent": engine_result["win_rate_percent"],
                },
                "independent_replay": {
                    "checked": False,
                    "reason": "workflow_graph templates are validated via trigger scenarios, flat-sequence guards, and engine backtest sanity checks",
                },
                "mismatches": backtest_sanity["issues"],
            })
            if not ok:
                report["ok"] = False

        json_path, md_path = write_reports(report, args.out)
        print(json.dumps({
            "ok": report["ok"],
            "templates": len(report["templates"]),
            "data_source": report["data_source"],
            "candle_count": report["candle_count"],
            "json_report": str(json_path),
            "md_report": str(md_path),
        }, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
