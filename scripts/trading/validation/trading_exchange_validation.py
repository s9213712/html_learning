#!/usr/bin/env python3
"""
Deterministic exchange, wallet, bot, backtest, and liquidation validation.

This complements trading_stress_pentest.py. The stress script validates HTTP,
permissions, concurrency, and restore consistency. This script validates
calculation correctness with controlled prices/candles so failures are
reproducible without relying on live Binance prices.
"""

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.points_chain import PointsLedgerService, ensure_points_economy_schema
from services.platform.db_mode_triggers import register_app_mode_function
from services.trading.trading_engine import (
    ASSET_SCALE,
    TradingEngineService,
    ensure_trading_schema,
    fee_points,
    notional_points,
    units_to_quantity,
)
from services.points_chain.wallet_identity import official_hot_wallet_address
from scripts.test_artifacts import test_artifact_path


REPORT_DIR = test_artifact_path("trading_validation")
VALIDATION_MODE_READER = lambda: "production"


class ValidationFailure(RuntimeError):
    pass


class MutablePriceProvider:
    def __init__(self, prices):
        self.prices = dict(prices)

    def __call__(self, symbol):
        return int(self.prices[symbol])

    def set(self, symbol, price):
        self.prices[symbol] = int(price)


def actor(user_id=1, username="alice", role="user"):
    return {"id": user_id, "username": username, "role": role}


def make_candles(prices, prefix="2026-01-01T00:"):
    candles = []
    for index, price in enumerate(prices):
        candles.append({
            "time": f"{prefix}{index:02d}:00Z",
            "time_iso": f"{prefix}{index:02d}:00Z",
            "open_points": price,
            "high_points": price,
            "low_points": price,
            "close_points": price,
            "volume": 100 + index,
        })
    return candles


def make_services(tmp_path):
    db_path = Path(tmp_path) / "exchange_validation.db"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        register_app_mode_function(conn, mode_reader=VALIDATION_MODE_READER)
        return conn

    conn = get_db()
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active')"
    )
    conn.execute(
        "INSERT INTO users (username, role, status) VALUES "
        "('alice', 'user', 'active'), "
        "('bob', 'manager', 'active'), "
        "('root', 'super_admin', 'active')"
    )
    ensure_points_economy_schema(conn)
    ensure_trading_schema(conn)
    conn.commit()
    conn.close()

    prices = MutablePriceProvider({"BTC/POINTS": 1000, "ETH/POINTS": 1000})
    points = PointsLedgerService(
        get_db=get_db,
        chain_secret="exchange-validation-secret",
        backup_dir=Path(tmp_path) / "points_chain_backups",
        mode_reader=VALIDATION_MODE_READER,
    )
    trading = TradingEngineService(get_db=get_db, points_service=points, live_price_provider=prices)
    return points, trading, prices, get_db


def official_hot_credit_metadata(points, user_id):
    return {
        "source_fund_key": "official_treasury",
        "destination_wallet_address": official_hot_wallet_address(points.chain_secret, int(user_id)),
        "settlement_rail": "internal_hot_wallet",
        "chain_required": False,
        "approval_required": False,
        "network_fee_points": 0,
        "service_fee_points": 0,
    }


def fund_user(points, *, user_id, amount, action_type, actor):
    return points.record_transaction(
        user_id=user_id,
        currency_type="points",
        direction="credit",
        amount=amount,
        action_type=action_type,
        actor=actor,
        public_metadata=official_hot_credit_metadata(points, user_id),
    )


def burn_validation_spend(points, *, user_id, amount, action_type, actor):
    return points.record_transaction(
        user_id=user_id,
        currency_type="points",
        direction="debit",
        amount=amount,
        action_type=action_type,
        actor=actor,
        public_metadata={
            "destination_fund_key": "burn",
            "settlement_rail": "internal_hot_wallet",
            "chain_required": False,
            "approval_required": False,
            "network_fee_points": 0,
            "service_fee_points": 0,
        },
    )


def ok(results, name, passed, **details):
    item = {"name": name, "ok": bool(passed), **details}
    results.append(item)
    print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    if not passed:
        raise ValidationFailure(f"{name}: {details}")
    return item


def expect_raises(results, name, func, contains=None):
    try:
        func()
    except Exception as exc:
        message = str(exc)
        passed = contains is None or contains in message
        return ok(results, name, passed, error=message)
    return ok(results, name, False, error="operation unexpectedly succeeded")


def validation_scenarios():
    results = []
    with tempfile.TemporaryDirectory(prefix="hackme_exchange_validation_") as tmp:
        points, trading, prices, get_db = make_services(tmp)
        root = actor(3, "root", "super_admin")
        alice = actor(1, "alice", "user")

        # Wallet and spot trading.
        fund_user(points, user_id=1, amount=5000, action_type="validation_funding", actor=root)
        before_wallet = points.get_wallet(1)
        buy = trading.place_order(
            actor=alice,
            market_symbol="ETH/POINTS",
            side="buy",
            order_type="market",
            quantity="2",
        )
        after_buy_wallet = points.get_wallet(1)
        sell = trading.place_order(
            actor=alice,
            market_symbol="ETH/POINTS",
            side="sell",
            order_type="market",
            quantity="0.5",
        )
        after_sell_wallet = points.get_wallet(1)
        dash = trading.user_dashboard(user_id=1)
        ok(
            results,
            "spot buy/sell updates wallet and ETH position without negative balance",
            buy["executed"] is True
            and sell["executed"] is True
            and after_buy_wallet["points_balance"] >= 0
            and after_sell_wallet["points_balance"] >= 0
            and any(row["market_symbol"] == "ETH/POINTS" and row["quantity"] == "1.5" for row in dash["positions"]),
            before_wallet=before_wallet,
            after_buy_wallet=after_buy_wallet,
            after_sell_wallet=after_sell_wallet,
        )
        ok(results, "trading reserve receives spot fees", trading.root_report()["reserve_pool"]["balance_points"] > 0)

        expect_raises(
            results,
            "oversell is rejected",
            lambda: trading.place_order(actor=alice, market_symbol="ETH/POINTS", side="sell", order_type="market", quantity="99"),
            "insufficient spot position",
        )
        expect_raises(
            results,
            "zero quantity is rejected",
            lambda: trading.place_order(actor=alice, market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0"),
            "must be positive",
        )
        expect_raises(
            results,
            "negative quantity is rejected",
            lambda: trading.place_order(actor=alice, market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="-1"),
            "must be positive",
        )

        # DCA bot trigger.
        dca = trading.save_trading_bot(
            actor=alice,
            payload={
                "bot_type": "dca",
                "name": "validation dca",
                "market_symbol": "ETH/POINTS",
                "budget_points": 100,
                "interval_hours": 1,
                "max_runs": 2,
                "cooldown_seconds": 0,
            },
        )
        dca_run = trading.run_trading_bots(actor=alice)
        ok(
            results,
            "DCA bot triggers one market buy from budget",
            dca["ok"] is True and len(dca_run["triggered"]) == 1 and dca_run["failed"] == [],
            run=dca_run,
        )

        conditional = trading.save_trading_bot(
            actor=alice,
            payload={
                "bot_type": "conditional",
                "name": "validation conditional",
                "market_symbol": "ETH/POINTS",
                "side": "buy",
                "order_type": "market",
                "quantity": "0.01",
                "trigger_type": "price_below",
                "trigger_price_points": 1200,
                "max_runs": 1,
                "cooldown_seconds": 0,
            },
        )
        conditional_run = trading.run_trading_bots(actor=alice)
        ok(
            results,
            "conditional automation bot triggers when price rule is met",
            conditional["ok"] is True
            and any(row.get("bot_uuid") == conditional["bot"]["bot_uuid"] for row in conditional_run["triggered"]),
            run=conditional_run,
        )

        # Workflow bot with nested AND/OR and two scaling steps.
        workflow_tmp = Path(tmp) / "workflow_case"
        workflow_tmp.mkdir(parents=True, exist_ok=True)
        workflow_points, workflow_trading, _workflow_prices, _workflow_db = make_services(workflow_tmp)
        fund_user(workflow_points, user_id=1, amount=5000, action_type="validation_workflow_funding", actor=root)
        workflow = {
            "version": 1,
            "strategy_kind": "workflow",
            "branches": [
                {
                    "id": "entry",
                    "name": "nested entry",
                    "logic": "AND",
                    "conditions": [
                        {"type": "price_below", "value": 1200},
                        {"OR": [{"type": "price_above", "value": 900}, {"type": "has_position", "value": True}]},
                    ],
                    "actions": [
                        {"type": "buy_amount", "amount_points": 100, "step": 1, "order_type": "market"},
                        {"type": "buy_amount", "amount_points": 200, "step": 2, "order_type": "market"},
                    ],
                    "cooldown_seconds": 0,
                    "priority": 10,
                }
            ],
        }
        bot = workflow_trading.save_trading_bot(
            actor=alice,
            payload={
                "bot_type": "conditional",
                "name": "validation workflow",
                "market_symbol": "ETH/POINTS",
                "side": "buy",
                "order_type": "market",
                "quantity": "0.01",
                "max_runs": 5,
                "cooldown_seconds": 0,
                "workflow": workflow,
            },
        )
        first_workflow = workflow_trading.run_trading_bots(actor=alice)
        second_workflow = workflow_trading.run_trading_bots(actor=alice)
        third_workflow = workflow_trading.run_trading_bots(actor=alice)
        ok(
            results,
            "workflow bot honors nested condition and does not repeat exhausted scaling steps",
            bot["ok"] is True
            and len(first_workflow["triggered"]) >= 1
            and len(second_workflow["triggered"]) >= 1
            and all(row.get("bot_uuid") != bot["bot"]["bot_uuid"] for row in third_workflow["triggered"]),
            first=first_workflow,
            second=second_workflow,
            third=third_workflow,
        )

        position_conn = get_db()
        try:
            position_after_incremental_buys = position_conn.execute(
                "SELECT * FROM trading_spot_positions WHERE user_id=? AND market_symbol=?",
                (1, "ETH/POINTS"),
            ).fetchone()
        finally:
            position_conn.close()
        avg_cost_after_incremental_buys = float(position_after_incremental_buys["avg_cost_points"] or 0) if position_after_incremental_buys else 0.0
        ok(
            results,
            "incremental spot buys preserve sane average cost accounting",
            bool(position_after_incremental_buys)
            and avg_cost_after_incremental_buys > 0
            and avg_cost_after_incremental_buys < 10_000,
            avg_cost_points=avg_cost_after_incremental_buys,
            quantity_units=int(position_after_incremental_buys["quantity_units"] or 0) if position_after_incremental_buys else 0,
            note="ETH/POINTS live price is 1000; average cost should stay in a sane range after DCA and conditional buys.",
        )

        # Backtest correctness with hand-computable DCA data.
        dca_prices = [100, 110, 120]
        dca_backtest = trading.backtest_trading_bot(
            actor=alice,
            payload={
                "strategy": "dca",
                "market_symbol": "ETH/POINTS",
                "candles": make_candles(dca_prices),
                "initial_cash_points": 1000,
                "order_points": 100,
                "interval_candles": 1,
            },
        )
        expected_cash = 1000
        expected_units = 0
        for price in dca_prices:
            spend = 100
            fee = fee_points(spend, 0.3)
            net = spend - fee
            expected_cash -= spend
            expected_units += int((net * ASSET_SCALE) // price)
        expected_final = expected_cash + notional_points(expected_units, dca_prices[-1])
        ok(
            results,
            "DCA backtest result matches hand calculation",
            dca_backtest["trade_count"] == 3 and dca_backtest["final_value_points"] == expected_final,
            expected_final_value_points=expected_final,
            actual_final_value_points=dca_backtest["final_value_points"],
            backtest=dca_backtest,
        )

        workflow_backtest_config = {
            "version": 1,
            "strategy_kind": "workflow",
            "branches": [
                {
                    "id": "buy-low",
                    "logic": "AND",
                    "conditions": [{"type": "price_below", "value": 95}],
                    "actions": [{"type": "buy_amount", "amount_points": 100, "step": 1, "order_type": "market"}],
                    "priority": 10,
                },
                {
                    "id": "sell-high",
                    "logic": "AND",
                    "conditions": [{"type": "price_above", "value": 110}, {"type": "has_position", "value": True}],
                    "actions": [{"type": "close_all", "step": 1, "order_type": "market"}],
                    "priority": 100,
                },
            ],
        }
        workflow_backtest = trading.backtest_trading_bot(
            actor=alice,
            payload={
                "strategy": "workflow",
                "market_symbol": "ETH/POINTS",
                "workflow": workflow_backtest_config,
                "candles": make_candles([100, 90, 80, 120]),
                "initial_cash_points": 1000,
                "order_points": 100,
            },
        )
        ok(
            results,
            "workflow backtest triggers buy-low then close-all with positive final value",
            workflow_backtest["trade_count"] == 2
            and [row["side"] for row in workflow_backtest["trades"]] == ["buy", "sell"]
            and workflow_backtest["final_value_points"] > 1000,
            backtest=workflow_backtest,
        )

        # Grid bot live lifecycle: create initial limit orders, match one buy,
        # then scan the bot so it places the next sell level.
        fund_user(points, user_id=1, amount=20000, action_type="validation_grid_funding", actor=root)
        trading.place_order(
            actor=alice,
            market_symbol="ETH/POINTS",
            side="buy",
            order_type="market",
            quantity="1",
        )
        grid_bot = trading.create_grid_bot(
            actor=alice,
            payload={
                "name": "validation grid",
                "market_symbol": "ETH/POINTS",
                "lower_price_points": 800,
                "upper_price_points": 1200,
                "grid_count": 5,
                "order_amount_points": 100,
            },
        )
        prices.set("ETH/POINTS", 900)
        matched_grid = trading.match_open_limit_orders(actor={"username": "system", "role": "system"}, market_symbol="ETH/POINTS", limit=20)
        scanned_grid = trading.scan_grid_bots(actor=alice)
        ok(
            results,
            "grid bot creates orders, processes a fill, and places next counter order",
            grid_bot["ok"] is True
            and len(grid_bot["placed"]) == 4
            and any(row["side"] == "buy" and row["execution_price_points"] == 900 for row in matched_grid["matched"])
            and any(
                item["fills_processed"]
                and any(order["side"] == "sell" and order["price_points"] == 1000 for order in item["counter_orders_placed"])
                for item in scanned_grid["results"]
            ),
            grid_bot=grid_bot,
            matched=matched_grid,
            scanned=scanned_grid,
        )

        grid_backtest = trading.backtest_trading_bot(
            actor=alice,
            payload={
                "strategy": "grid",
                "market_symbol": "ETH/POINTS",
                "initial_cash_points": 1000,
                "lower_price_points": 80,
                "upper_price_points": 120,
                "grid_count": 5,
                "order_amount_points": 100,
                "candles": [
                    {"time": 1, "open_points": 100, "high_points": 100, "low_points": 100, "close_points": 100},
                    {"time": 2, "open_points": 100, "high_points": 100, "low_points": 90, "close_points": 90},
                    {"time": 3, "open_points": 90, "high_points": 110, "low_points": 90, "close_points": 110},
                    {"time": 4, "open_points": 110, "high_points": 120, "low_points": 80, "close_points": 100},
                ],
            },
        )
        ok(
            results,
            "grid backtest follows adjacent-level lifecycle and avoids same-candle churn",
            grid_backtest["strategy"] == "grid"
            and grid_backtest["trade_count"] == 7
            and grid_backtest["final_value_points"] == 1065
            and [row["side"] for row in grid_backtest["trades"]] == ["buy", "sell", "sell", "sell", "buy", "buy", "buy"]
            and all(not (row["index"] == 0 and row["price_points"] == 100) for row in grid_backtest["trades"]),
            backtest=grid_backtest,
        )

        # Margin liquidation with extreme move. First prove the market guard
        # rejects an abrupt live-price jump, then loosen it for deterministic
        # liquidation validation.
        prices.set("ETH/POINTS", 5000)
        expect_raises(
            results,
            "extreme live price jump is blocked by market guard",
            lambda: trading.place_order(actor=alice, market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.01"),
            "price jump",
        )
        trading.update_market(actor=root, symbol="ETH/POINTS", max_price_jump_percent=1000)
        fund_user(points, user_id=1, amount=5000, action_type="validation_margin_funding", actor=root)
        margin_long = trading.open_margin_position(
            actor=alice,
            market_symbol="ETH/POINTS",
            position_type="margin_long",
            quantity="1",
            collateral_points=1000,
        )
        long_wallet = points.get_wallet(1)
        long_available = int(long_wallet["points_balance"])
        if long_available > 5:
            burn_validation_spend(
                points,
                user_id=1,
                amount=long_available - 5,
                action_type="validation_cross_margin_spend",
                actor=root,
            )
        prices.set("ETH/POINTS", 3000)
        scan_long = trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=50)
        ok(
            results,
            "margin long is liquidated after extreme downside move",
            any(row["position_uuid"] == margin_long["position"]["position_uuid"] for row in scan_long["liquidated"]),
            scan=scan_long,
        )

        prices.set("ETH/POINTS", 5000)
        fund_user(points, user_id=1, amount=2500, action_type="validation_short_funding", actor=root)
        margin_short = trading.open_margin_position(
            actor=alice,
            market_symbol="ETH/POINTS",
            position_type="short",
            quantity="0.5",
            collateral_points=1500,
        )
        short_wallet = points.get_wallet(1)
        short_available = int(short_wallet["points_balance"])
        if short_available > 5:
            burn_validation_spend(
                points,
                user_id=1,
                amount=short_available - 5,
                action_type="validation_cross_margin_spend",
                actor=root,
            )
        prices.set("ETH/POINTS", 10000)
        scan_short = trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=50)
        ok(
            results,
            "short position is liquidated after extreme upside move",
            any(row["position_uuid"] == margin_short["position"]["position_uuid"] for row in scan_short["liquidated"]),
            scan=scan_short,
        )

        final_wallet = points.get_wallet(1)
        ok(results, "wallet never becomes negative after liquidation tests", final_wallet["points_balance"] >= 0, wallet=final_wallet)

        verification = trading.verify_state()
        ok(results, "trading state verification passes after all operations", verification["ok"] is True, verification=verification)
        chain = points.verify_chain()
        ok(
            results,
            "PointsChain verification and financial invariants pass after trading operations",
            chain["ok"] is True and chain.get("financial_ok") is True,
            verification=chain,
        )
        block = points.force_seal_block(actor=root, reason="exchange_validation")
        backups = points.list_ledger_backups(limit=5)
        ok(
            results,
            "PointsChain block seal keeps restorable ledger backups disabled",
            block["ok"] is True
            and block.get("backup") is None
            and block.get("backup_policy", "disabled_append_only_chain") == "disabled_append_only_chain"
            and backups == [],
            block=block,
            backups=backups,
        )

        # Safe mode must pause all exchange writes.
        conn = get_db()
        conn.execute(
            """
            INSERT OR REPLACE INTO points_chain_recovery_state
                (id, safe_mode, reason, verification_json, forensic_bundle_id, restore_plan_json, created_at, updated_at, restored_at)
            VALUES (1, 1, 'validation_tamper', '{"ok":false}', 'validation-bundle', '{}', datetime('now'), datetime('now'), NULL)
            """
        )
        conn.commit()
        conn.close()
        expect_raises(
            results,
            "PointsChain safe mode blocks new exchange orders",
            lambda: trading.place_order(actor=alice, market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.01"),
            "PointsChain safe mode active",
        )

    return results


def summarize(results):
    return {
        "ok": all(row["ok"] for row in results),
        "total": len(results),
        "passed": sum(1 for row in results if row["ok"]),
        "failed": sum(1 for row in results if not row["ok"]),
        "results": results,
        "price_source_notes": [
            {
                "name": "Coinbase Exchange",
                "use": "ticker fallback for BTC-USD / ETH-USD",
                "endpoint": "https://api.exchange.coinbase.com/products/{product_id}/ticker",
                "docs": "https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductticker/",
            },
            {
                "name": "Kraken",
                "use": "ticker / OHLC fallback",
                "endpoint": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
                "docs": "https://docs.kraken.com/api/docs/rest-api/get-ticker-information/",
            },
            {
                "name": "CoinGecko",
                "use": "aggregated reference price fallback, slower but exchange-independent",
                "endpoint": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_last_updated_at=true",
                "docs": "https://docs.coingecko.com/reference/simple-price",
            },
            {
                "name": "OKX",
                "use": "ticker / candlestick fallback",
                "endpoint": "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
                "docs": "https://www.okx.com/docs-v5/en",
            },
        ],
    }


def write_reports(summary, out_dir=REPORT_DIR):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    json_path = out_dir / f"trading_exchange_validation_{stamp}.json"
    md_path = out_dir / f"trading_exchange_validation_{stamp}.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Trading Exchange and Wallet Validation Report",
        "",
        f"- Result: {'PASS' if summary['ok'] else 'FAIL'}",
        f"- Checks: {summary['passed']}/{summary['total']} passed",
        "",
        "## Validated Areas",
        "",
        "- Spot buy/sell, fees, reserve pool, positions, and wallet non-negative invariant.",
        "- Abnormal inputs: oversell, zero quantity, negative quantity.",
        "- DCA bot, conditional automation bot, workflow bot, and grid bot lifecycle behavior.",
        "- DCA, workflow, and grid backtest outputs against deterministic expected values.",
        "- Margin long and short liquidation under controlled extreme price moves.",
        "- Trading state verification, PointsChain verification, block sealing, disabled ledger-backup policy.",
        "- PointsChain safe mode blocking new exchange writes.",
        "",
        "## Price Source Fallback Candidates",
        "",
    ]
    for source in summary["price_source_notes"]:
        lines.append(f"- {source['name']}: {source['use']}; endpoint `{source['endpoint']}`; docs {source['docs']}")
    lines.extend(["", "## Check Results", ""])
    for row in summary["results"]:
        lines.append(f"- [{'PASS' if row['ok'] else 'FAIL'}] {row['name']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="print JSON summary")
    parser.add_argument("--out", default=str(REPORT_DIR), help="report output directory")
    args = parser.parse_args()
    try:
        summary = summarize(validation_scenarios())
    except Exception as exc:
        summary = {
            "ok": False,
            "total": 0,
            "passed": 0,
            "failed": 1,
            "error": str(exc),
            "results": [],
            "price_source_notes": [],
        }
    json_path, md_path = write_reports(summary, args.out)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"reports: {json_path} {md_path}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
