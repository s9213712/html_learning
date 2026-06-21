import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Flask, jsonify

import routes.trading as trading_routes
from services.trading import btc_bridge
from routes.trading import register_trading_routes
from services.trading.btc_bridge import BtcTradeBridge


ROOT = Path(__file__).resolve().parents[3]


class _FakeBinanceResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _app(actor, check_user_rate_limit=None):
    app = Flask(__name__)
    app.testing = True

    def passthrough(fn):
        return fn

    def json_resp(payload, status=None):
        response = jsonify(payload)
        return (response, status) if status else response

    register_trading_routes(app, {
        "trading_service": object(),
        "get_current_user_ctx": lambda: actor,
        "json_resp": json_resp,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
        "check_user_rate_limit": check_user_rate_limit or (lambda *args, **kwargs: (False, {})),
    })
    return app


def _btc_signal_app(actor, project_dir, *, btc_trade_enabled=True):
    app = Flask(__name__)
    app.testing = True

    class FakeTradingService:
        def get_root_settings(self):
            return {
                "settings": {
                    "btc_trade_enabled": btc_trade_enabled,
                    "btc_trade_project_dir": str(project_dir or ""),
                    "btc_trade_repo_url": "https://github.com/s9213712/BTC_trade.git",
                    "btc_trade_branch": "strategy/v15b-plus",
                }
            }

    def passthrough(fn):
        return fn

    def json_resp(payload, status=None):
        response = jsonify(payload)
        return (response, status) if status else response

    register_trading_routes(app, {
        "trading_service": FakeTradingService(),
        "get_current_user_ctx": lambda: actor,
        "json_resp": json_resp,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
    })
    return app


def _backtest_app(actor):
    app = Flask(__name__)
    app.testing = True

    class FakeTradingService:
        def _validate_workflow(self, workflow):
            return workflow

        def backtest_trading_bot(self, *, actor, payload):
            candles = payload.get("candles") or []
            return {
                "ok": True,
                "market_symbol": payload.get("market_symbol"),
                "strategy": payload.get("strategy"),
                "candle_count": len(candles),
                "data_source": payload.get("data_source"),
                "provider_symbol": payload.get("provider_symbol"),
                "first_candle_time": candles[0].get("time_iso") if candles else "",
                "last_candle_time": candles[-1].get("time_iso") if candles else "",
                "trade_count": 0,
                "initial_cash_points": 1000,
                "final_value_points": 1000,
                "pnl_points": 0,
                "return_percent": 0,
                "max_drawdown_percent": 0,
                "trades": [],
                "equity_curve": [],
            }

    def passthrough(fn):
        return fn

    def json_resp(payload, status=None):
        response = jsonify(payload)
        return (response, status) if status else response

    register_trading_routes(app, {
        "trading_service": FakeTradingService(),
        "get_current_user_ctx": lambda: actor,
        "json_resp": json_resp,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
        "check_user_rate_limit": lambda *args, **kwargs: (False, {}),
        "audit": lambda *args, **kwargs: None,
    })
    return app


def _order_error_app(actor, exc):
    app = Flask(__name__)
    app.testing = True

    class FakeTradingService:
        def place_order(self, **kwargs):
            raise exc

    def passthrough(fn):
        return fn

    def json_resp(payload, status=None):
        response = jsonify(payload)
        return (response, status) if status else response

    register_trading_routes(app, {
        "trading_service": FakeTradingService(),
        "get_current_user_ctx": lambda: actor,
        "json_resp": json_resp,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
        "audit": lambda *args, **kwargs: None,
    })
    return app


def _root_price_fusion_status_app(actor, captured):
    app = Flask(__name__)
    app.testing = True

    class FakeTradingService:
        def get_root_price_fusion_status(self, *, market_symbol=""):
            captured["market_symbol"] = market_symbol
            normalized = {
                "BTC/USDT": "BTC/POINTS",
                "ETH/USDT": "ETH/POINTS",
            }.get(market_symbol, market_symbol or "BTC/POINTS")
            return {
                "market_symbol": normalized,
                "requested_market_symbol": market_symbol or "",
                "resolved_market_symbol": normalized,
                "display_market_symbol": normalized.replace("/POINTS", "/USDT"),
                "configured_source": "fused_weighted",
                "requested_mode": "auto_depth",
                "resolved_mode": "auto_depth",
                "resolved_source": "fused_weighted",
                "state": "healthy",
                "weights_sum_percent": 100.0,
                "providers_used": [{"source": "binance_public_api", "label": "Binance", "normalized_weight_percent": 100.0, "weight": 1.0, "depth_score": 1.0, "price_points": 100}],
                "excluded_providers": [],
                "degraded": False,
                "fallback_active": False,
                "conservative_mode": False,
                "message": "",
                "price_points": 100,
                "transport_state": {
                    "mode": "websocket",
                    "connected": True,
                    "fallback": False,
                    "stale": False,
                    "degraded": False,
                    "confidence": "high",
                    "provider_count": 4,
                    "last_update_at": "2026-05-05T00:00:00",
                    "exclusion_reason": "",
                    "message": "WebSocket provider input 正常運作。",
                },
                "connected": True,
                "fallback": False,
                "stale": False,
                "confidence": "high",
                "provider_count": 4,
                "last_update_at": "2026-05-05T00:00:00",
                "exclusion_reason": "",
            }

    def passthrough(fn):
        return fn

    def json_resp(payload, status=None):
        response = jsonify(payload)
        return (response, status) if status else response

    register_trading_routes(app, {
        "trading_service": FakeTradingService(),
        "get_current_user_ctx": lambda: actor,
        "json_resp": json_resp,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
        "check_user_rate_limit": lambda *args, **kwargs: (False, {}),
        "audit": lambda *args, **kwargs: None,
    })
    return app


def _live_price_app(actor, captured):
    app = Flask(__name__)
    app.testing = True

    class FakeTradingService:
        def get_live_market_quote(self, *, market_symbol=""):
            captured["market_symbol"] = market_symbol
            defaulted = not bool(market_symbol)
            resolved_symbol = {
                "BTC/USDT": "BTC/POINTS",
                "ETH/USDT": "ETH/POINTS",
            }.get(market_symbol, market_symbol or "BTC/POINTS")
            return {
                "market": {
                    "symbol": resolved_symbol,
                    "manual_price_points": 81234,
                    "price_source": "fused_weighted",
                    "fee_rate_percent": 0.1,
                    "reference_price_points": 81234,
                    "risk_grade_price_points": 81198,
                },
                "requested_market_symbol": market_symbol or "",
                "resolved_market_symbol": resolved_symbol,
                "display_market_symbol": resolved_symbol.replace("/POINTS", "/USDT"),
                "refresh_interval_ms": 2000,
                "server_time": "2026-05-04T00:00:00",
                "price_type": "reference",
                "source": "fused_weighted",
                "confidence": "high" if not defaulted else "low",
                "stale": defaulted,
                "degraded": defaulted,
                "provider_count": 4 if not defaulted else 1,
                "connected": not defaulted,
                "fallback": defaulted,
                "last_update_at": "2026-05-05T00:00:00",
                "exclusion_reason": "provider_count_low" if defaulted else "",
                "price_health": "fallback" if defaulted else "healthy",
                "fallback_reason": "orderbook unavailable" if defaulted else "",
                "excluded_sources": ["okx_public_api"] if defaulted else [],
                "warnings": [{"code": "provider_count_low", "message": "可用來源不足", "severity": "critical"}] if defaulted else [],
                "high_risk_blocked": defaulted,
                "high_risk_block_reason": "目前不是正常 fused price" if defaulted else "",
                "risk_grade_usable": not defaulted,
                "defaulted_market": defaulted,
                "transport_state": {
                    "mode": "mixed" if defaulted else "websocket",
                    "connected": not defaulted,
                    "fallback": defaulted,
                    "stale": defaulted,
                    "degraded": defaulted,
                    "confidence": "low" if defaulted else "high",
                    "provider_count": 1 if defaulted else 4,
                    "last_update_at": "2026-05-05T00:00:00",
                    "exclusion_reason": "provider_count_low" if defaulted else "",
                    "message": "部分 provider 已切回 HTTP polling，請視為可審計降級狀態。" if defaulted else "WebSocket provider input 正常運作。",
                },
                "reference_price_context": {
                    "price_type": "reference",
                    "source": "fused_weighted",
                    "source_label": "融合參考價",
                    "confidence": "high" if not defaulted else "low",
                    "stale": defaulted,
                    "degraded": defaulted,
                    "provider_count": 4 if not defaulted else 1,
                    "purpose": "展示 / 一般估值 / K 線 / 非風控參考",
                    "warning_message": "目前使用最後健康快取" if defaulted else "",
                    "risk_grade_usable": False,
                },
                "risk_grade_price_context": {
                    "price_type": "risk_grade",
                    "source": "fused_weighted",
                    "source_label": "風控級融合價",
                    "confidence": "high" if not defaulted else "low",
                    "stale": defaulted,
                    "degraded": defaulted,
                    "provider_count": 4 if not defaulted else 1,
                    "purpose": "融資 / 強平 / 保證金 / PnL / bot 風控 / 交易限制",
                    "warning_message": "目前不是正常 fused price" if defaulted else "",
                    "high_risk_blocked": defaulted,
                    "risk_grade_usable": not defaulted,
                },
            }

    def passthrough(fn):
        return fn

    def json_resp(payload, status=None):
        response = jsonify(payload)
        return (response, status) if status else response

    register_trading_routes(app, {
        "trading_service": FakeTradingService(),
        "get_current_user_ctx": lambda: actor,
        "json_resp": json_resp,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
        "check_user_rate_limit": lambda *args, **kwargs: (False, {}),
        "audit": lambda *args, **kwargs: None,
    })
    return app


def test_trading_reference_prices_proxy_maps_usdt_markets_to_binance(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _FakeBinanceResponse([
            [1714500000000, "60000", "61000", "59000", "60500.5"],
            [1714503600000, "60500", "62000", "60400", "61800.25"],
        ])

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.get("/api/trading/reference-prices?market=BTC/USDT&interval=1h&limit=48")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["source"] == "binance_public_api"
    assert payload["price_type"] == "reference"
    assert payload["confidence"] == "medium"
    assert payload["stale"] is False
    assert payload["degraded"] is False
    assert payload["provider_count"] == 1
    assert payload["market"] == "BTC/POINTS"
    assert payload["display_market"] == "BTC/USDT"
    assert payload["display_market"] == "BTC/USDT"
    assert payload["symbol"] == "BTCUSDT"
    assert payload["usdt_to_points_rate"] == 1
    assert payload["candles"][0]["open_usdt"] == 60000
    assert payload["candles"][0]["high_usdt"] == 61000
    assert payload["candles"][0]["low_usdt"] == 59000
    assert payload["candles"][0]["close_usdt"] == 60500.5
    assert payload["candles"][0]["close_points"] == 60500.5
    assert payload["points"][0]["price_points"] == 60500.5
    assert payload["price_context"]["price_type"] == "reference"
    assert payload["price_context"]["source"] == "binance_public_api"
    assert "symbol=BTCUSDT" in captured["url"]
    assert "interval=1h" in captured["url"]
    assert captured["timeout"] == 6


def test_backtest_downloads_historical_candles_when_browser_did_not_send_any(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        return _FakeBinanceResponse([
            [1714500000000, "60000", "61000", "59000", "60500"],
            [1714500900000, "60500", "62000", "60400", "61800"],
        ])

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _backtest_app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.post("/api/trading/bots/backtest", json={
        "market_symbol": "BTC/USDT",
        "strategy": "dca",
        "auto_fetch_reference_candles": True,
        "timeframe": "15m",
        "candle_limit": 2,
        "start_time": "2024-05-01T00:00:00+00:00",
        "end_time": "2024-05-01T00:30:00+00:00",
    })

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["data_source"] == "binance_public_api"
    assert payload["provider_symbol"] == "BTCUSDT"
    assert payload["candle_count"] == 2
    assert payload["max_backtest_candles"] == trading_routes.MAX_BACKTEST_CANDLES
    assert payload["provider_candle_limit"] == trading_routes.BACKTEST_PROVIDER_CANDLE_LIMIT
    assert payload["requested_candle_limit"] == 2
    assert payload["download_candle_limit"] == 2
    assert "api.binance.com/api/v3/klines" in captured["url"]
    assert "interval=15m" in captured["url"]
    assert "startTime=" in captured["url"]
    assert "endTime=" in captured["url"]


def test_workflow_editor_backtest_preview_downloads_historical_candles(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        return _FakeBinanceResponse([
            [1714500000000, "60000", "61000", "59000", "60500"],
            [1714500900000, "60500", "62000", "60400", "61800"],
        ])

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _backtest_app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.post("/api/trading/workflow-editor/backtest", json={
        "market_symbol": "BTC/USDT",
        "timeframe": "15m",
        "initial_cash_points": 5000,
        "start_time": "2024-05-01T00:00:00+00:00",
        "end_time": "2024-05-01T00:30:00+00:00",
        "workflow_json": {
            "version": 2,
            "strategy_kind": "workflow_graph",
            "source": "workflow_editor",
            "name": "Preview Workflow",
            "start_node_id": "start",
            "nodes": [
                {"id": "start", "type": "start", "label": "開始", "x": 0, "y": 0},
                {"id": "hold", "type": "action", "label": "不動作", "x": 200, "y": 0, "action": {"type": "hold", "step": 1, "order_type": "market"}},
            ],
            "edges": [
                {"id": "e1", "from": "start", "from_port": "out", "to": "hold", "to_port": "in"}
            ],
        },
    })

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["strategy"] == "workflow"
    assert payload["data_source"] == "binance_public_api"
    assert payload["provider_symbol"] == "BTCUSDT"
    assert payload["candle_count"] == 2
    assert payload["requested_candle_limit"] == 2
    assert payload["download_candle_limit"] == 2
    assert "api.binance.com/api/v3/klines" in captured["url"]
    assert "interval=15m" in captured["url"]


def test_workflow_editor_backtest_preview_requires_workflow_json():
    client = _backtest_app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.post("/api/trading/workflow-editor/backtest", json={
        "market_symbol": "BTC/USDT",
        "timeframe": "1h",
    })

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert "Workflow JSON" in payload["msg"]


def test_backtest_download_supports_full_year_hourly_window(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    requested_limits = []

    def fake_urlopen(request, timeout=0):
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        limit = int(query["limit"][0])
        requested_limits.append(limit)
        start_ms = int(query.get("startTime", ["1714500000000"])[0])
        candles = []
        for idx in range(limit):
            open_time = start_ms + idx * trading_routes.INTERVAL_MILLISECONDS["1h"]
            price = 60000 + (idx % 24)
            candles.append([open_time, str(price), str(price + 10), str(price - 10), str(price + 5)])
        return _FakeBinanceResponse(candles)

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _backtest_app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.post("/api/trading/bots/backtest", json={
        "market_symbol": "BTC/USDT",
        "strategy": "dca",
        "auto_fetch_reference_candles": True,
        "timeframe": "1h",
        "candle_limit": 8784,
        "start_time": "2024-01-01T00:00:00+00:00",
        "end_time": "2024-12-31T23:00:00+00:00",
    })

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["candle_count"] == 8784
    assert payload["max_backtest_candles"] == trading_routes.MAX_BACKTEST_CANDLES
    assert payload["max_backtest_candles_per_batch"] == trading_routes.BACKTEST_SEGMENT_CANDLES
    assert payload["provider_candle_limit"] == trading_routes.BACKTEST_PROVIDER_CANDLE_LIMIT
    assert requested_limits[:3] == [1000, 1000, 1000]
    assert requested_limits[-1] == 784


def test_backtest_download_supports_ranges_above_single_execution_batch(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    requested_limits = []

    def fake_urlopen(request, timeout=0):
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        limit = int(query["limit"][0])
        requested_limits.append(limit)
        start_ms = int(query.get("startTime", ["1714500000000"])[0])
        candles = []
        for idx in range(limit):
            open_time = start_ms + idx * trading_routes.INTERVAL_MILLISECONDS["1h"]
            price = 60000 + (idx % 24)
            candles.append([open_time, str(price), str(price + 10), str(price - 10), str(price + 5)])
        return _FakeBinanceResponse(candles)

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _backtest_app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.post("/api/trading/bots/backtest", json={
        "market_symbol": "BTC/USDT",
        "strategy": "dca",
        "auto_fetch_reference_candles": True,
        "timeframe": "1h",
        "candle_limit": 12000,
        "start_time": "2024-01-01T00:00:00+00:00",
        "end_time": "2025-05-14T23:00:00+00:00",
    })

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["candle_count"] == 12000
    assert payload["max_backtest_candles"] == trading_routes.MAX_BACKTEST_CANDLES
    assert payload["max_backtest_candles_per_batch"] == trading_routes.BACKTEST_SEGMENT_CANDLES
    assert payload["provider_candle_limit"] == trading_routes.BACKTEST_PROVIDER_CANDLE_LIMIT
    assert requested_limits[:3] == [1000, 1000, 1000]
    assert requested_limits[-1] == 1000
    assert len(requested_limits) == 12


def test_backtest_does_not_silently_replace_isolated_single_candle(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()

    def should_not_fetch(request, timeout=0):
        raise AssertionError("historical price provider should not be called for isolated single-candle payloads")

    monkeypatch.setattr(trading_routes, "urlopen", should_not_fetch)
    client = _backtest_app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.post("/api/trading/bots/backtest", json={
        "market_symbol": "BTC/USDT",
        "strategy": "dca",
        "auto_fetch_reference_candles": True,
        "candles": [{"time_iso": "2024-05-01T00:00:00+00:00", "close_points": 60000}],
    })

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert "candles" in payload["msg"].lower()


def test_trading_order_invalid_decimal_is_sanitized_for_user():
    client = _order_error_app(
        {"id": 1, "username": "alice", "role": "user"},
        ValueError("[<class 'decimal.InvalidOperation'>]"),
    ).test_client()

    response = client.post("/api/trading/orders", json={
        "market_symbol": "BTC/POINTS",
        "side": "buy",
        "order_type": "market",
        "quantity": "NaN",
    })

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["msg"] == "交易數量格式錯誤，請輸入有效的數字"


def test_trading_order_conservative_mode_message_is_not_misclassified_as_insufficient_funds():
    client = _order_error_app(
        {"id": 1, "username": "alice", "role": "user"},
        ValueError("市價交易已因價格降級暫停：目前可用來源數不足，只能提供 degraded reference price（healthy providers=0，需要至少 2 家才視為可交易）"),
    ).test_client()

    response = client.post("/api/trading/orders", json={
        "market_symbol": "ETH/POINTS",
        "side": "buy",
        "order_type": "market",
        "quantity": "0.01",
    })

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert "價格降級暫停" in payload["msg"]
    assert "交易資金不足" not in payload["msg"]


def test_btc_trade_signal_hidden_when_project_missing(tmp_path):
    client = _btc_signal_app({"id": 1, "username": "alice", "role": "user"}, tmp_path / "missing").test_client()

    response = client.get("/api/trading/btc-signal?market=BTC/USDT")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["available"] is False
    assert payload["hidden"] is True


def test_btc_trade_signal_hidden_when_disabled(tmp_path):
    client = _btc_signal_app(
        {"id": 1, "username": "alice", "role": "user"},
        tmp_path / "BTC_trade",
        btc_trade_enabled=False,
    ).test_client()

    response = client.get("/api/trading/btc-signal?market=BTC/USDT")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["available"] is False
    assert payload["hidden"] is True
    assert "未啟用" in payload["msg"]


def test_btc_trade_signal_reads_latest_report(tmp_path):
    project = tmp_path / "BTC_trade"
    runtime = project / "runtime"
    runtime.mkdir(parents=True)
    (project / "hourly_check.py").write_text("# test", encoding="utf-8")
    (project / "update_data.py").write_text("# test", encoding="utf-8")
    (project / "retrain_models.py").write_text("# test", encoding="utf-8")
    (project / "backtest_report.py").write_text("# test", encoding="utf-8")
    (runtime / "report_log_4h.jsonl").write_text(
        "\n".join([
            json.dumps({"bar_ts": "old", "signal_ok": False, "current_price": 1}),
            json.dumps({
                "generated_at": "2026-05-02T12:02:01.706619",
                "bar_ts": "2026-05-02T00:00:00",
                "strategy_version": "V15b+",
                "report_title": "BTC 週期報告",
                "signal_ok": True,
                "ml_ok": True,
                "position": "LONG",
                "current_price": 77059.5,
                "fear_greed": 39,
                "capital": 9784.28,
                "btc": 0.05,
                "total_equity": 10020.5,
                "total_pnl_pct": 0.2,
                "entry_checks": {"MA50": True},
                "ml_status": {"situation": "三多", "blocked": False},
                "report_text": "full report",
                "telegram_text": "short report",
                "timeframe": "4h",
            }),
        ]),
        encoding="utf-8",
    )
    (runtime / "portfolio_state_4h.json").write_text(json.dumps({"position": "LONG", "btc": 0.1}), encoding="utf-8")
    (runtime / "trade_log_4h.json").write_text(json.dumps([{"action": "ENTRY", "timestamp": "2026-05-02"}]), encoding="utf-8")
    client = _btc_signal_app({"id": 1, "username": "alice", "role": "user"}, project).test_client()

    response = client.get("/api/trading/btc-signal?market=BTC/POINTS")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["available"] is True
    assert payload["hidden"] is False
    assert payload["signal"]["current_price"] == 77059.5
    assert payload["signal"]["signal_ok"] is True
    assert payload["signal"]["strategy_version"] == "V15b+"
    assert payload["signal"]["report_title"] == "BTC 週期報告"
    assert payload["signal"]["fear_greed"] == 39
    assert payload["signal"]["capital"] == 9784.28
    assert payload["signal"]["btc"] == 0.05
    assert payload["signal"]["total_equity"] == 10020.5
    assert payload["signal"]["report_text"] == "full report"
    assert payload["signal"]["telegram_text"] == "short report"
    assert payload["signal"]["ml_status"]["situation"] == "三多"
    assert payload["signal"]["portfolio"]["position"] == "LONG"
    assert payload["signal"]["last_trade"]["action"] == "ENTRY"
    assert payload["signal"]["prediction_interval_seconds"] == 14400
    assert payload["signal"]["next_prediction_at"]
    assert payload["signal"]["next_prediction_seconds"] >= 0


def test_root_btc_trade_check_reports_initialization_needed(tmp_path):
    client = _btc_signal_app({"id": 1, "username": "root", "role": "super_admin"}, tmp_path / "BTC_trade").test_client()

    response = client.post("/api/root/trading/btc-trade/check", json={"project_dir": str(tmp_path / "BTC_trade")})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["status"]["available"] is False
    assert payload["status"]["needs_initialization"] is True
    assert "hourly_check" in payload["status"]["missing"]


def test_root_btc_trade_setup_failure_is_nonfatal(monkeypatch, tmp_path):
    def fake_setup(project_dir, **kwargs):
        return {
            "ok": False,
            "project_dir": str(project_dir),
            "message": "build failed",
            "steps": [{"label": "下載 BTC_trade", "ok": False}],
            "status": {"available": False},
        }

    monkeypatch.setattr(trading_routes, "btc_trade_setup", fake_setup)
    client = _btc_signal_app({"id": 1, "username": "root", "role": "super_admin"}, tmp_path / "BTC_trade").test_client()

    response = client.post("/api/root/trading/btc-trade/setup", json={
        "project_dir": str(tmp_path / "BTC_trade"),
        "repo_url": "https://github.com/s9213712/BTC_trade.git",
        "branch": "strategy/v15b-plus",
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["setup_ok"] is False
    assert payload["message"] == "build failed"


def test_root_btc_trade_setup_rejects_bad_repo_without_500(tmp_path):
    client = _btc_signal_app({"id": 1, "username": "root", "role": "super_admin"}, tmp_path / "BTC_trade").test_client()

    response = client.post("/api/root/trading/btc-trade/setup", json={
        "project_dir": str(tmp_path / "BTC_trade"),
        "repo_url": "file:///tmp/BTC_trade.git",
        "branch": "strategy/v15b-plus",
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["setup_ok"] is False
    assert "建置失敗" in payload["message"]


def test_root_btc_trade_start_returns_background_job(monkeypatch, tmp_path):
    captured = {}

    def fake_start(
        project_dir,
        *,
        timeframe="4h",
        wait_seconds=0,
        repo_url=None,
        branch=None,
        base_dir=None,
        setup_if_needed=False,
    ):
        captured.update({
            "repo_url": repo_url,
            "branch": branch,
            "base_dir": base_dir,
            "setup_if_needed": setup_if_needed,
        })
        return {
            "ok": True,
            "started": True,
            "job": {
                "job_id": "job-123",
                "project_dir": str(project_dir),
                "timeframe": timeframe,
                "status": "queued",
                "message": "已建立背景工作",
                "steps": [],
                "result": None,
            },
        }

    monkeypatch.setattr(trading_routes, "btc_trade_start_prediction_job", fake_start)
    client = _btc_signal_app({"id": 1, "username": "root", "role": "super_admin"}, tmp_path / "BTC_trade").test_client()

    response = client.post("/api/root/trading/btc-trade/start", json={
        "project_dir": str(tmp_path / "BTC_trade"),
        "repo_url": "https://github.com/s9213712/BTC_trade.git",
        "branch": "strategy/v15b-plus",
        "timeframe": "4h",
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["start_ok"] is True
    assert payload["started"] is True
    assert payload["job"]["job_id"] == "job-123"
    assert captured["repo_url"] == "https://github.com/s9213712/BTC_trade.git"
    assert captured["branch"] == "strategy/v15b-plus"
    assert captured["setup_if_needed"] is True
    assert captured["base_dir"] == ROOT


def test_root_btc_trade_start_surfaces_immediate_job_failure(monkeypatch, tmp_path):
    def fake_start(
        project_dir,
        *,
        timeframe="4h",
        wait_seconds=0,
        repo_url=None,
        branch=None,
        base_dir=None,
        setup_if_needed=False,
    ):
        return {
            "ok": False,
            "job_ok": False,
            "started": True,
            "job": {
                "job_id": "job-error",
                "project_dir": str(project_dir),
                "timeframe": timeframe,
                "status": "error",
                "message": "BTC_trade 建置失敗：ValueError",
                "steps": [{"label": "自動下載/安裝 BTC_trade", "ok": False, "message": "repo rejected"}],
                "result": {"ok": False},
            },
        }

    monkeypatch.setattr(trading_routes, "btc_trade_start_prediction_job", fake_start)
    client = _btc_signal_app({"id": 1, "username": "root", "role": "super_admin"}, tmp_path / "BTC_trade").test_client()

    response = client.post("/api/root/trading/btc-trade/start", json={
        "project_dir": str(tmp_path / "BTC_trade"),
        "repo_url": "https://127.0.0.1:1/nope.git",
        "branch": "main",
        "timeframe": "4h",
        "wait_seconds": 1,
    })
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is False
    assert payload["start_ok"] is False
    assert payload["job"]["status"] == "error"
    assert "建置失敗" in payload["msg"]


def test_btc_trade_start_pipeline_auto_sets_up_missing_project(monkeypatch, tmp_path):
    project = tmp_path / "BTC_trade"
    calls = {"setup": 0, "commands": []}

    def fake_setup(project_dir, **kwargs):
        calls["setup"] += 1
        root = btc_bridge.expand_server_path(project_dir)
        root.mkdir(parents=True, exist_ok=True)
        for name in ("update_data.py", "retrain_models.py", "hourly_check.py", "backtest_report.py"):
            (root / name).write_text("# test\n", encoding="utf-8")
        return {
            "ok": True,
            "project_dir": str(root),
            "repo_url": kwargs.get("repo_url"),
            "branch": kwargs.get("branch"),
            "steps": [{"label": "下載 BTC_trade", "ok": True, "message": "ok"}],
            "status": btc_bridge.btc_trade_status(root),
            "message": "BTC_trade 建置完成",
        }

    def fake_run_step(command, *, cwd, timeout):
        calls["commands"].append(command)
        root = btc_bridge.expand_server_path(cwd)
        script = command[1] if len(command) > 1 else command[0]
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        if script == "update_data.py":
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "btc_4h.csv").write_text(f"timestamp,close\n{now_iso},96000\n", encoding="utf-8")
        elif script == "retrain_models.py":
            models = root / "models"
            models.mkdir(parents=True, exist_ok=True)
            (models / "btc_model.pkl").write_text("model", encoding="utf-8")
        elif script == "hourly_check.py":
            runtime = root / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "report_log_4h.jsonl").write_text(json.dumps({
                "generated_at": now_iso,
                "bar_ts": now_iso,
                "strategy_version": "test",
                "signal_ok": True,
                "timeframe": "4h",
            }) + "\n", encoding="utf-8")
        return {"command": " ".join(command), "ok": True, "returncode": 0, "seconds": 0}

    monkeypatch.setattr(btc_bridge, "btc_trade_setup", fake_setup)
    monkeypatch.setattr(btc_bridge, "_run_step", fake_run_step)

    result = btc_bridge.btc_trade_start_prediction_pipeline(
        project,
        timeframe="4h",
        wait_seconds=1,
        setup_if_needed=True,
        repo_url="https://github.com/s9213712/BTC_trade.git",
        branch="strategy/v15b-plus",
    )

    assert result["ok"] is True
    assert calls["setup"] == 1
    assert any(step["label"] == "自動下載/安裝 BTC_trade" for step in result["steps"])
    assert ["update_data.py", "retrain_models.py", "hourly_check.py"] == [cmd[1] for cmd in calls["commands"]]
    assert result["actions"]["data_updated"] is True
    assert result["actions"]["model_retrained"] is True
    assert result["actions"]["prediction_refreshed"] is True


def test_root_btc_trade_start_status_returns_job(monkeypatch, tmp_path):
    monkeypatch.setattr(trading_routes, "btc_trade_start_prediction_job_status", lambda job_id: {
        "job_id": job_id,
        "project_dir": str(tmp_path / "BTC_trade"),
        "timeframe": "4h",
        "status": "running",
        "message": "重訓中",
        "steps": [{"label": "重訓 BTC_trade 模型", "ok": True, "message": "正在執行"}],
        "result": None,
    })
    client = _btc_signal_app({"id": 1, "username": "root", "role": "super_admin"}, tmp_path / "BTC_trade").test_client()

    response = client.get("/api/root/trading/btc-trade/start-status?job_id=job-456")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["job"]["status"] == "running"
    assert payload["job"]["job_id"] == "job-456"


def test_root_btc_trade_start_status_marks_error_job_not_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(trading_routes, "btc_trade_start_prediction_job_status", lambda job_id: {
        "job_id": job_id,
        "project_dir": str(tmp_path / "BTC_trade"),
        "timeframe": "4h",
        "status": "error",
        "message": "BTC_trade 建置失敗：ValueError",
        "steps": [{"label": "自動下載/安裝 BTC_trade", "ok": False, "message": "repo rejected"}],
        "result": {"ok": False},
    })
    client = _btc_signal_app({"id": 1, "username": "root", "role": "super_admin"}, tmp_path / "BTC_trade").test_client()

    response = client.get("/api/root/trading/btc-trade/start-status?job_id=job-error")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is False
    assert payload["job_ok"] is False
    assert payload["job"]["status"] == "error"
    assert "建置失敗" in payload["msg"]


def test_btc_trade_status_reports_artifact_freshness(tmp_path):
    project = tmp_path / "BTC_trade"
    data_dir = project / "data"
    runtime = project / "runtime"
    models = project / "models"
    data_dir.mkdir(parents=True)
    runtime.mkdir(parents=True)
    models.mkdir(parents=True)
    now = time.time()
    data_ts = int(now - 2 * 60 * 60)
    model_ts = data_ts + 60
    report_ts = model_ts + 60
    data_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(data_ts))
    report_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(report_ts))
    for name in ("hourly_check.py", "update_data.py", "retrain_models.py", "backtest_report.py"):
        (project / name).write_text("# test\n", encoding="utf-8")
    (data_dir / "btc_4h.csv").write_text(f"timestamp,close\n{data_iso},96000\n", encoding="utf-8")
    (models / "btc_model.pkl").write_text("model", encoding="utf-8")
    (runtime / "report_log_4h.jsonl").write_text(json.dumps({
        "generated_at": report_iso,
        "bar_ts": data_iso,
        "signal_ok": True,
        "current_price": 96000,
        "timeframe": "4h",
    }), encoding="utf-8")
    os.utime(data_dir / "btc_4h.csv", (data_ts, data_ts))
    os.utime(models / "btc_model.pkl", (model_ts, model_ts))
    os.utime(runtime / "report_log_4h.jsonl", (report_ts, report_ts))

    status = btc_bridge.btc_trade_status(project)

    assert status["available"] is True
    assert status["artifacts"]["data"]["needs_update"] is False
    assert status["artifacts"]["models"]["needs_retrain"] is False
    assert status["artifacts"]["prediction"]["needs_refresh"] is False


def test_btc_trade_bridge_script_lives_in_hackme_project():
    script = trading_routes.__file__.rsplit("/routes/", 1)[0] + "/scripts/trading/bridges/btc_signal_bridge.py"
    text = open(script, encoding="utf-8").read()

    assert "BtcTradeBridge" in text
    assert "--btc-trade-dir" in text
    assert "/NN/BTC_trade/btc_signal_bridge.py" not in text


def test_btc_trade_bridge_dry_run_does_not_advance_state(tmp_path):
    project = tmp_path / "BTC_trade"
    runtime = project / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "trade_log_4h.json").write_text(
        json.dumps([{"action": "ENTRY", "btc": 0.01, "timestamp": "2026-05-02T00:00:00"}]),
        encoding="utf-8",
    )
    bridge = BtcTradeBridge(
        hackme_dir=tmp_path / "hackme",
        btc_trade_dir=project,
        state_path=runtime / "bridge_state.json",
    )

    result = bridge.run(dry_run=True)

    assert result["ok"] is True
    assert result["orders"][0]["dry_run"] is True
    assert not (runtime / "bridge_state.json").exists()


def test_btc_trade_bridge_defaults_runtime_files_to_runtime_subdir(monkeypatch, tmp_path):
    hackme_dir = tmp_path / "hackme"
    monkeypatch.delenv("HACKME_RUNTIME_DIR", raising=False)
    bridge = BtcTradeBridge(
        hackme_dir=hackme_dir,
        btc_trade_dir=tmp_path / "BTC_trade",
    )

    assert bridge.runtime_root == hackme_dir / "runtime"
    assert bridge.db_path == hackme_dir / "runtime" / "database" / "database.db"
    assert bridge.chain_seed_path == hackme_dir / "runtime" / ".chain_seed"


def test_btc_trade_bridge_honors_explicit_runtime_root(monkeypatch, tmp_path):
    runtime_root = tmp_path / "isolated-runtime"
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(runtime_root))
    bridge = BtcTradeBridge(
        hackme_dir=tmp_path / "hackme",
        btc_trade_dir=tmp_path / "BTC_trade",
    )

    assert bridge.runtime_root == runtime_root
    assert bridge.db_path == runtime_root / "database" / "database.db"
    assert bridge.chain_seed_path == runtime_root / ".chain_seed"


def test_trading_reference_prices_defaults_to_15m_chart_interval(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        return _FakeBinanceResponse([
            [1714500000000, "60000", "61000", "59000", "60500.5"],
        ])

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.get("/api/trading/reference-prices?market=BTC/USDT")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["interval"] == "15m"
    assert "interval=15m" in captured["url"]


def test_trading_reference_prices_uses_short_server_side_cache(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    calls = {"count": 0}

    def fake_urlopen(request, timeout=0):
        calls["count"] += 1
        return _FakeBinanceResponse([
            [1714500000000, "60000", "61000", "59000", "60500.5"],
        ])

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _app({"id": 1, "username": "alice", "role": "user"}).test_client()

    first = client.get("/api/trading/reference-prices?market=BTC/USDT&interval=5m")
    second = client.get("/api/trading/reference-prices?market=BTC/USDT&interval=5m")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["count"] == 1


def test_trading_reference_prices_falls_back_to_last_good_cache(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    clock = {"value": 100.0}

    def fake_monotonic():
        return clock["value"]

    def ok_urlopen(request, timeout=0):
        return _FakeBinanceResponse([
            [1714500000000, "60000", "61000", "59000", "60500.5"],
        ])

    monkeypatch.setattr(trading_routes.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(trading_routes, "urlopen", ok_urlopen)
    client = _app({"id": 1, "username": "alice", "role": "user"}).test_client()

    first = client.get("/api/trading/reference-prices?market=BTC/USDT&interval=15m&limit=24")
    assert first.status_code == 200
    assert first.get_json()["source"] == "binance_public_api"

    clock["value"] = 200.0

    def failing_urlopen(*args, **kwargs):
        raise OSError("binance down")

    monkeypatch.setattr(trading_routes, "urlopen", failing_urlopen)
    second = client.get("/api/trading/reference-prices?market=BTC/USDT&interval=15m&limit=24")

    assert second.status_code == 200
    payload = second.get_json()
    assert payload["ok"] is True
    assert payload["source"] == "binance_public_api_cached"
    assert payload["stale"] is True
    assert payload["candles"][0]["close_points"] == 60500.5


def test_trading_reference_prices_falls_back_to_coinbase_candles(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    urls = []

    def fake_urlopen(request, timeout=0):
        urls.append(request.full_url)
        if "api.binance.com" in request.full_url:
            raise OSError("binance down")
        return _FakeBinanceResponse([
            [1714503600, 60400, 62000, 60500, 61800.25, 12.5],
            [1714500000, 59000, 61000, 60000, 60500.5, 10.5],
        ])

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.get("/api/trading/reference-prices?market=BTC/USDT&interval=15m&limit=24")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["source"] == "coinbase_exchange"
    assert payload["symbol"] == "BTC-USD"
    assert payload["candles"][0]["open_usdt"] == 60000
    assert payload["candles"][0]["close_points"] == 60500.5
    assert any("api.binance.com" in url for url in urls)
    assert any("api.exchange.coinbase.com/products/BTC-USD/candles" in url for url in urls)


def test_trading_reference_prices_walks_public_fallback_chain_to_bitstamp(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    urls = []

    def fake_urlopen(request, timeout=0):
        urls.append(request.full_url)
        if "bitstamp.net" in request.full_url:
            return _FakeBinanceResponse({
                "data": {
                    "ohlc": [
                        {
                            "timestamp": "1714500000",
                            "open": "60000",
                            "high": "61000",
                            "low": "59000",
                            "close": "60500.5",
                        },
                        {
                            "timestamp": "1714500900",
                            "open": "60500",
                            "high": "62000",
                            "low": "60400",
                            "close": "61800.25",
                        },
                    ]
                }
            })
        raise OSError("provider unavailable")

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.get("/api/trading/reference-prices?market=BTC/USDT&interval=15m&limit=24")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["source"] == "bitstamp_public_api"
    assert payload["symbol"] == "btcusd"
    assert payload["candles"][0]["open_usdt"] == 60000
    assert payload["candles"][1]["close_points"] == 61800.25
    expected_hosts = (
        "api.binance.com",
        "okx.com",
        "api.exchange.coinbase.com",
        "api.kraken.com",
        "api.gemini.com",
        "bitstamp.net",
    )
    seen_hosts = [host for host in expected_hosts if any(host in url for url in urls)]
    assert seen_hosts == list(expected_hosts)


def test_trading_reference_prices_rejects_too_short_chart_intervals(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    called = {"value": False}

    def fake_urlopen(*args, **kwargs):
        called["value"] = True
        raise AssertionError("network should not be called")

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _app({"id": 1, "username": "alice", "role": "user"}).test_client()

    for interval in ("1s", "1m"):
        response = client.get(f"/api/trading/reference-prices?market=BTC/USDT&interval={interval}")
        assert response.status_code == 400
        assert response.get_json()["ok"] is False
    assert called["value"] is False


def test_trading_reference_prices_latest_mode_fetches_one_candle(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        return _FakeBinanceResponse([
            [1714500000000, "60000", "61000", "59000", "60500.5"],
        ])

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.get("/api/trading/reference-prices?market=BTC/USDT&interval=15m&limit=96&latest=1")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["latest_only"] is True
    assert len(payload["candles"]) == 1
    assert "limit=1" in captured["url"]
    assert "interval=15m" in captured["url"]


def test_trading_reference_prices_rejects_unsupported_market_without_network(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    called = {"value": False}

    def fake_urlopen(*args, **kwargs):
        called["value"] = True
        raise AssertionError("network should not be called")

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    client = _app({"id": 1, "username": "alice", "role": "user"}).test_client()

    response = client.get("/api/trading/reference-prices?market=DOGE/POINTS")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert called["value"] is False


def test_trading_reference_prices_rate_limits_before_binance_proxy(monkeypatch):
    trading_routes.REFERENCE_PRICE_CACHE.clear()
    called = {"value": False}

    def fake_urlopen(*args, **kwargs):
        called["value"] = True
        raise AssertionError("network should not be called after rate limit")

    monkeypatch.setattr(trading_routes, "urlopen", fake_urlopen)
    seen = {}

    def fake_rate_limit(user_id, action, max_req, window_sec):
        seen.update({"user_id": user_id, "action": action, "max_req": max_req, "window_sec": window_sec})
        return True, {"retry_after": 17}

    client = _app({"id": 7, "username": "alice", "role": "user"}, check_user_rate_limit=fake_rate_limit).test_client()

    response = client.get("/api/trading/reference-prices?market=BTC/USDT")

    assert response.status_code == 429
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["retry_after"] == 17
    assert seen == {"user_id": 7, "action": "trading_reference_prices", "max_req": 120, "window_sec": 60}
    assert called["value"] is False


def test_root_trading_routes_reject_manual_price_cheat_controls():
    class FakeTradingService:
        def update_root_settings(self, **kwargs):
            raise AssertionError("manual price source should be rejected before service call")

        def update_market(self, **kwargs):
            raise AssertionError("manual market price should be rejected before service call")

    app = Flask(__name__)
    app.testing = True

    def passthrough(fn):
        return fn

    def json_resp(payload, status=None):
        response = jsonify(payload)
        return (response, status) if status else response

    register_trading_routes(app, {
        "trading_service": FakeTradingService(),
        "get_current_user_ctx": lambda: {"id": 1, "username": "root", "role": "super_admin"},
        "json_resp": json_resp,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
    })
    client = app.test_client()

    source_response = client.post("/api/root/trading/settings", json={"settings": {"price_source": "manual_root"}})
    price_response = client.post("/api/root/trading/markets/BTC%2FUSDT", json={"manual_price_points": 1})

    assert source_response.status_code == 400
    assert source_response.get_json()["ok"] is False
    assert price_response.status_code == 400
    assert price_response.get_json()["ok"] is False


def test_root_trading_routes_accept_fused_price_settings_payload():
    captured = {}

    class FakeTradingService:
        def get_root_settings(self):
            return {"settings": {}}

        def update_root_settings(self, *, actor, settings, markets):
            captured["actor"] = actor
            captured["settings"] = settings
            captured["markets"] = markets
            return {"ok": True, "settings": settings, "markets": markets}

    app = Flask(__name__)
    app.testing = True

    def passthrough(fn):
        return fn

    def json_resp(payload, status=None):
        response = jsonify(payload)
        return (response, status) if status else response

    register_trading_routes(app, {
        "trading_service": FakeTradingService(),
        "get_current_user_ctx": lambda: {"id": 1, "username": "root", "role": "super_admin"},
        "json_resp": json_resp,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_ua": lambda: "pytest",
    })
    client = app.test_client()

    response = client.post("/api/root/trading/settings", json={
        "settings": {
            "price_source": "fused_weighted",
            "price_fusion_mode": "manual_weights",
            "price_fusion_manual_weights": {
                "binance_public_api": 2,
                "okx_public_api": 1,
                "coinbase_exchange": 0,
                "kraken_public_api": 0,
                "gemini_public_api": 0,
                "bitstamp_public_api": 0,
            },
        },
        "markets": [],
    })

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert captured["actor"]["username"] == "root"
    assert captured["settings"]["price_source"] == "fused_weighted"
    assert captured["settings"]["price_fusion_mode"] == "manual_weights"
    assert captured["settings"]["price_fusion_manual_weights"]["binance_public_api"] == 2


def test_root_price_fusion_status_route_passes_selected_market_symbol():
    captured = {}
    client = _root_price_fusion_status_app({"id": 1, "username": "root", "role": "super_admin"}, captured).test_client()

    response = client.get("/api/root/trading/price-fusion-status?market_symbol=ETH/POINTS")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"]["market_symbol"] == "ETH/POINTS"
    assert payload["status"]["requested_market_symbol"] == "ETH/POINTS"
    assert payload["status"]["resolved_market_symbol"] == "ETH/POINTS"
    assert payload["status"]["display_market_symbol"] == "ETH/USDT"
    assert payload["status"]["connected"] is True
    assert payload["status"]["fallback"] is False
    assert payload["status"]["stale"] is False
    assert payload["status"]["confidence"] == "high"
    assert payload["status"]["provider_count"] == 4
    assert payload["status"]["last_update_at"] == "2026-05-05T00:00:00"
    assert payload["status"]["exclusion_reason"] == ""
    assert payload["status"]["transport_state"]["connected"] is True
    assert payload["status"]["transport_state"]["fallback"] is False
    assert captured["market_symbol"] == "ETH/POINTS"


def test_root_price_fusion_status_route_normalizes_display_market_symbol():
    captured = {}
    client = _root_price_fusion_status_app({"id": 1, "username": "root", "role": "super_admin"}, captured).test_client()

    response = client.get("/api/root/trading/price-fusion-status?market_symbol=ETH/USDT")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    payload = response.get_json()["status"]
    assert payload["requested_market_symbol"] == "ETH/USDT"
    assert payload["resolved_market_symbol"] == "ETH/POINTS"
    assert payload["display_market_symbol"] == "ETH/USDT"
    assert payload["transport_state"]["provider_count"] == 4
    assert captured["market_symbol"] == "ETH/USDT"


def test_trading_live_price_route_returns_selected_market_quote():
    captured = {}
    client = _live_price_app({"id": 1, "username": "alice", "role": "user"}, captured).test_client()

    response = client.get("/api/trading/live-price?market=BTC/POINTS")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["market"]["symbol"] == "BTC/POINTS"
    assert payload["market"]["manual_price_points"] == 81234
    assert payload["market"]["price_source"] == "fused_weighted"
    assert payload["price_health"] == "healthy"
    assert payload["price_type"] == "reference"
    assert payload["source"] == "fused_weighted"
    assert payload["confidence"] == "high"
    assert payload["stale"] is False
    assert payload["degraded"] is False
    assert payload["provider_count"] == 4
    assert payload["connected"] is True
    assert payload["fallback"] is False
    assert payload["last_update_at"] == "2026-05-05T00:00:00"
    assert payload["exclusion_reason"] == ""
    assert payload["transport_state"]["connected"] is True
    assert payload["transport_state"]["fallback"] is False
    assert payload["fallback_reason"] == ""
    assert payload["excluded_sources"] == []
    assert payload["warnings"] == []
    assert payload["high_risk_blocked"] is False
    assert payload["risk_grade_usable"] is True
    assert payload["high_risk_block_reason"] == ""
    assert payload["defaulted_market"] is False
    assert payload["requested_market_symbol"] == "BTC/POINTS"
    assert payload["resolved_market_symbol"] == "BTC/POINTS"
    assert payload["display_market_symbol"] == "BTC/USDT"
    assert payload["reference_price_context"]["price_type"] == "reference"
    assert payload["risk_grade_price_context"]["price_type"] == "risk_grade"
    assert payload["risk_grade_price_context"]["purpose"].startswith("融資 / 強平")
    assert payload["refresh_interval_ms"] == 2000
    assert captured["market_symbol"] == "BTC/POINTS"


def test_trading_live_price_route_normalizes_display_market_symbol():
    captured = {}
    client = _live_price_app({"id": 1, "username": "alice", "role": "user"}, captured).test_client()

    response = client.get("/api/trading/live-price?market=BTC/USDT")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    payload = response.get_json()
    assert payload["requested_market_symbol"] == "BTC/USDT"
    assert payload["resolved_market_symbol"] == "BTC/POINTS"
    assert payload["display_market_symbol"] == "BTC/USDT"
    assert captured["market_symbol"] == "BTC/USDT"


def test_trading_live_price_route_marks_defaulted_market_when_missing():
    captured = {}
    client = _live_price_app({"id": 1, "username": "alice", "role": "user"}, captured).test_client()

    response = client.get("/api/trading/live-price")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["market"]["symbol"] == "BTC/POINTS"
    assert payload["price_health"] == "fallback"
    assert payload["fallback_reason"] == "orderbook unavailable"
    assert payload["excluded_sources"] == ["okx_public_api"]
    assert payload["warnings"][0]["code"] == "provider_count_low"
    assert payload["high_risk_blocked"] is True
    assert payload["risk_grade_usable"] is False
    assert payload["high_risk_block_reason"] == "目前不是正常 fused price"
    assert payload["defaulted_market"] is True
    assert payload["stale"] is True
    assert payload["degraded"] is True
    assert payload["provider_count"] == 1
    assert payload["connected"] is False
    assert payload["fallback"] is True
    assert payload["last_update_at"] == "2026-05-05T00:00:00"
    assert payload["exclusion_reason"] == "provider_count_low"
    assert payload["transport_state"]["fallback"] is True
    assert payload["transport_state"]["stale"] is True
    assert payload["risk_grade_price_context"]["high_risk_blocked"] is True
    assert payload["risk_grade_price_context"]["risk_grade_usable"] is False
    assert captured["market_symbol"] == ""
