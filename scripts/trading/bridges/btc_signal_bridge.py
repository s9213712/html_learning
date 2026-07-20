#!/usr/bin/env python3
"""Bridge BTC_trade runtime trade events into hackme_web simulated spot orders.

This script lives in hackme_web so the integration does not depend on a helper
file inside the external BTC_trade project. BTC_trade remains an optional data
source: configure its project path, then run this script after BTC_trade updates
its runtime files.
"""

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.trading.btc_bridge import BtcTradeBridge, btc_trade_status


def _default_btc_trade_dir():
    return os.environ.get("BTC_TRADE_DIR") or os.environ.get("HACKME_BTC_TRADE_DIR") or ""


def _progress(message: str) -> None:
    print(f"[btc-signal-bridge] {message}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description="BTC_trade -> hackme_web spot trading bridge")
    parser.add_argument("--btc-trade-dir", default=_default_btc_trade_dir(), help="BTC_trade project root")
    parser.add_argument("--hackme-dir", default=str(ROOT), help="hackme_web project root")
    parser.add_argument("--bridge-username", default=os.environ.get("BTC_TRADE_BRIDGE_USERNAME", "btc_bridge"))
    parser.add_argument("--market-symbol", default=os.environ.get("BTC_TRADE_BRIDGE_MARKET", "BTC/USDT"))
    parser.add_argument("--quantity-scale", type=float, default=float(os.environ.get("BTC_TRADE_BRIDGE_QUANTITY_SCALE", "1.0")))
    parser.add_argument("--min-btc-quantity", type=float, default=float(os.environ.get("BTC_TRADE_BRIDGE_MIN_BTC", "0.000001")))
    parser.add_argument("--status", action="store_true", help="print BTC_trade signal status and exit")
    parser.add_argument("--dry-run", action="store_true", help="read pending trade events without placing orders")
    args = parser.parse_args()

    if args.status:
        _progress(f"phase status started: btc_trade_dir={args.btc_trade_dir or '<unset>'}")
        print(json.dumps(btc_trade_status(args.btc_trade_dir), ensure_ascii=False, indent=2))
        _progress("phase result status: ok")
        return 0

    _progress(f"hackme dir: {args.hackme_dir}")
    _progress(f"btc_trade dir: {args.btc_trade_dir or '<unset>'}")
    _progress(f"market: {args.market_symbol} bridge_user={args.bridge_username} dry_run={bool(args.dry_run)}")
    bridge = BtcTradeBridge(
        hackme_dir=args.hackme_dir,
        btc_trade_dir=args.btc_trade_dir,
        bridge_username=args.bridge_username,
        market_symbol=args.market_symbol,
        quantity_scale=args.quantity_scale,
        min_btc_quantity=args.min_btc_quantity,
    )
    _progress("phase bridge run started")
    result = bridge.run(dry_run=args.dry_run)
    if result.get("ok"):
        _progress("phase result bridge run: ok")
    else:
        _progress("phase result bridge run: failed")
        _progress("failure hint: check BTC_trade runtime files, bridge username, market symbol, and hackme DB paths")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _progress(f"FAIL: {exc}")
        _progress("failure hint: run with --status first to verify BTC_trade discovery before placing bridge orders")
        raise
