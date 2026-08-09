import csv
import io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Response, request

from services.management_plane import get_management_snapshot, management_job_start_payload, start_management_plane_job
from services.server.runtime import default_runtime_root
from services.governance.violation_fines import assert_user_feature_allowed
from services.trading.btc_bridge import (
    DEFAULT_BTC_TRADE_BRANCH,
    DEFAULT_BTC_TRADE_REPO_URL,
    BTC_TRADE_START_WAIT_SECONDS,
    btc_trade_setup,
    btc_trade_start_prediction_job,
    btc_trade_start_prediction_job_status,
    btc_trade_status,
    default_btc_trade_project_dir,
    expand_server_path,
)
from services.trading.catalog import (
    market_provider_id as catalog_market_provider_id,
    market_supports_btc_trade as catalog_market_supports_btc_trade,
    market_supports_reference_price as catalog_market_supports_reference_price,
    normalize_market_symbol as catalog_normalize_market_symbol,
)
from services.trading.trading_engine import BACKTEST_SEGMENT_CANDLES, MAX_BACKTEST_CANDLES, units_to_quantity


BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
COINBASE_CANDLES_URL_TEMPLATE = "https://api.exchange.coinbase.com/products/{product_id}/candles"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
GEMINI_CANDLES_URL_TEMPLATE = "https://api.gemini.com/v2/candles/{symbol}/{timeframe}"
BITSTAMP_OHLC_URL_TEMPLATE = "https://www.bitstamp.net/api/v2/ohlc/{pair}/"
USDT_TO_POINTS_RATE = 1
REFERENCE_PRICE_INTERVALS = {"5m", "15m", "1h", "4h", "1d"}
OKX_BAR_INTERVALS = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
COINBASE_GRANULARITY_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
KRAKEN_INTERVAL_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
GEMINI_TIMEFRAMES = {"5m": "5m", "15m": "15m", "1h": "1hr", "1d": "1day"}
BITSTAMP_STEP_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
INTERVAL_MILLISECONDS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
INTERVAL_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
BINANCE_MAX_CANDLES_PER_REQUEST = 1000
REFERENCE_PRICE_CACHE = {}
REFERENCE_PRICE_CACHE_TTL_SECONDS = 1.0
BACKTEST_PROVIDER_CANDLE_LIMIT = MAX_BACKTEST_CANDLES
WORKFLOW_ROOT = Path(__file__).resolve().parents[1] / "workflows"
WORKFLOW_SYSTEM_DIR = WORKFLOW_ROOT / "trading_bot"
WORKFLOW_TEMPLATE_BENCHMARK_PATH = WORKFLOW_ROOT / "trading_bot" / "benchmarks" / "workflow_template_benchmarks.json"
WORKFLOW_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def workflow_custom_root():
    runtime_root = Path(os.environ.get("HACKME_RUNTIME_DIR") or default_runtime_root())
    return runtime_root / "workflows" / "custom"


def workflow_custom_root_label():
    return "runtime/workflows/custom"


def register_trading_routes(app, deps):
    trading_service = deps["trading_service"]
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_client_ip = deps.get("get_client_ip", lambda: "")
    get_ua = deps.get("get_ua", lambda: "")
    json_resp = deps["json_resp"]
    require_csrf = deps["require_csrf"]
    require_csrf_safe = deps["require_csrf_safe"]
    check_user_rate_limit = deps.get("check_user_rate_limit", lambda *args, **kwargs: (False, {}))
    audit = deps.get("audit", lambda *args, **kwargs: None)
    role_rank = deps.get("role_rank", lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0))
    get_system_settings = deps.get("get_system_settings", lambda: {})
    get_runtime_server_mode = deps.get("get_runtime_server_mode", lambda: "production")
    get_db = deps.get("get_db") or getattr(trading_service, "get_db", None)
    background_queue_kick_lock = threading.Lock()
    background_queue_kick_active = False

    def trading_manual_price_allowed_in_current_mode():
        try:
            mode_payload = get_runtime_server_mode()
        except Exception:
            mode_payload = "production"
        if isinstance(mode_payload, dict):
            mode = str(mode_payload.get("current_mode") or mode_payload.get("mode") or "production")
        else:
            mode = str(mode_payload or "production")
        return mode.strip().lower() in {"dev_ready", "test", "internal_test", "superweak"}

    def actor_value(actor, key, default=None):
        if not actor:
            return default
        try:
            return actor[key]
        except Exception:
            return actor.get(key, default) if hasattr(actor, "get") else default

    def actor_or_401():
        actor = get_current_user_ctx()
        if not actor:
            return None, json_resp({"ok": False, "msg": "未登入"}, 401)
        return actor, None

    def root_or_403():
        actor, err = actor_or_401()
        if err:
            return None, err
        if actor_value(actor, "username") != "root":
            return None, json_resp({"ok": False, "msg": "只有 root 可執行此操作"}, 403)
        return actor, None

    def management_actor(actor):
        return {
            "id": actor_value(actor, "id"),
            "username": actor_value(actor, "username", ""),
            "role": actor_value(actor, "role", "user"),
        }

    def management_job_response(started, *, snapshot_key):
        return json_resp(
            management_job_start_payload(
                started["job"],
                snapshot_key=snapshot_key,
                created=bool(started.get("created")),
            ),
            202,
        )

    def kick_trading_background_queue(reason=""):
        nonlocal background_queue_kick_active
        with background_queue_kick_lock:
            if background_queue_kick_active:
                return False
            background_queue_kick_active = True

        def worker():
            nonlocal background_queue_kick_active
            try:
                owner = f"trading-bg-kick:{os.getpid()}"
                for _batch in range(20):
                    result = trading_service.run_due_background_jobs(
                        get_system_settings=get_system_settings,
                        get_runtime_server_mode=get_runtime_server_mode,
                        owner=owner,
                        job_keys=[],
                        queued_max_jobs=10,
                    )
                    queued_results = result.get("queued_results") or []
                    if not queued_results:
                        break
                    if all(row.get("queue_status") == "retry_wait" for row in queued_results):
                        break
            except Exception as exc:
                audit(
                    "TRADING_BACKGROUND_QUEUE_KICK_FAILED",
                    "0.0.0.0",
                    user="system",
                    success=False,
                    ua="background-queue-kick",
                    detail=json.dumps({"reason": reason, "error": str(exc)[:1000]}, ensure_ascii=False),
                )
            finally:
                with background_queue_kick_lock:
                    background_queue_kick_active = False

        thread = threading.Thread(target=worker, name="trading-background-queue-kick", daemon=True)
        thread.start()
        return True

    def manager_or_403():
        actor, err = actor_or_401()
        if err:
            return None, err
        role = "super_admin" if actor_value(actor, "username") == "root" else actor_value(actor, "role", "user")
        if role_rank(role) < role_rank("manager"):
            return None, json_resp({"ok": False, "msg": "需要管理員權限"}, 403)
        return actor, None

    def trading_restriction_response(actor, feature_key="trading_order"):
        if not get_db or not actor or actor_value(actor, "username") == "root":
            return None
        conn = get_db()
        try:
            allowed, msg, restrictions = assert_user_feature_allowed(conn, user_id=actor_value(actor, "id"), feature_key=feature_key)
            conn.commit()
            if allowed:
                return None
            return json_resp({"ok": False, "msg": msg, "code": "feature_restricted_by_violation_fine", "restrictions": restrictions}), 423
        finally:
            conn.close()

    def parse_json_body():
        try:
            data = request.get_json(force=True)
        except Exception:
            return None, json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}, 400)
        if not isinstance(data, dict):
            return None, json_resp({"ok": False, "msg": "請求內容格式錯誤"}, 400)
        return data, None

    def csv_download_response(filename, fieldnames, rows):
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
        return Response(
            "\ufeff" + buffer.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def table_columns(conn, table_name):
        try:
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        except Exception:
            return set()

    def workflow_user_slug(actor):
        raw = str(actor_value(actor, "username", "") or actor_value(actor, "id", "user")).strip()
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")
        return slug[:80] or "user"

    def workflow_template_slug(value, fallback="workflow"):
        raw = str(value or fallback).strip()
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")
        slug = slug[:80] or fallback
        if not WORKFLOW_SLUG_RE.match(slug):
            raise ValueError("workflow template id must use letters, numbers, dash, or underscore")
        return slug

    def normalize_market_symbol_for_route(value):
        normalizer = getattr(trading_service, "normalize_market_symbol", None)
        if callable(normalizer):
            return normalizer(value)
        return catalog_normalize_market_symbol(value)

    def market_provider_id_for_route(symbol, provider):
        resolver = getattr(trading_service, "market_provider_id", None)
        if callable(resolver):
            return resolver(symbol, provider)
        return catalog_market_provider_id(symbol, provider)

    def market_supports_reference_price_for_route(symbol):
        checker = getattr(trading_service, "market_supports_reference_price", None)
        if callable(checker):
            return checker(symbol)
        return catalog_market_supports_reference_price(symbol)

    def market_supports_btc_trade_for_route(symbol):
        checker = getattr(trading_service, "market_supports_btc_trade", None)
        if callable(checker):
            return checker(symbol)
        return catalog_market_supports_btc_trade(symbol)

    def relative_workflow_path(path):
        try:
            resolved = path.resolve()
            system_root = WORKFLOW_ROOT.resolve()
            if resolved == system_root or system_root in resolved.parents:
                return f"workflows/{resolved.relative_to(system_root).as_posix()}"
            custom_root = workflow_custom_root().resolve(strict=False)
            if resolved == custom_root or custom_root in resolved.parents:
                return f"{workflow_custom_root_label()}/{resolved.relative_to(custom_root).as_posix()}"
        except Exception:
            pass
        return path.name

    def load_workflow_template_file(path, *, scope):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"workflow template cannot be loaded: {path.name}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"workflow template must be an object: {path.name}")
        workflow = payload.get("workflow") if isinstance(payload.get("workflow"), dict) else payload
        validated = trading_service._validate_workflow(workflow)
        template_id = workflow_template_slug(payload.get("id") or validated.get("name") or path.stem, path.stem)
        return {
            "id": template_id,
            "label": str(payload.get("label") or validated.get("name") or template_id),
            "description": str(payload.get("description") or validated.get("description") or ""),
            "explanation": payload.get("explanation") if isinstance(payload.get("explanation"), dict) else {},
            "scope": scope,
            "source_path": relative_workflow_path(path),
            "workflow": validated,
        }

    def workflow_template_files_for(actor):
        files = []
        if WORKFLOW_SYSTEM_DIR.is_dir():
            files.extend(("system", path) for path in sorted(WORKFLOW_SYSTEM_DIR.glob("*.json")))
        if actor:
            custom_dir = workflow_custom_root() / workflow_user_slug(actor)
            if custom_dir.is_dir():
                files.extend(("custom", path) for path in sorted(custom_dir.glob("*.json")))
        return files

    def list_workflow_templates(actor):
        templates = []
        errors = []
        for scope, path in workflow_template_files_for(actor):
            try:
                templates.append(load_workflow_template_file(path, scope=scope))
            except Exception as exc:
                errors.append({"file": relative_workflow_path(path), "error": str(exc)})
        return templates, errors

    def service_error(exc):
        msg = str(exc) or exc.__class__.__name__
        status = 400
        lowered = msg.lower()
        if "conservative mode" in lowered or "價格降級暫停" in msg or "高風險交易" in msg or "風控級" in msg:
            return json_resp({"ok": False, "msg": msg}), 400
        if "decimal.invalidoperation" in lowered or "conversionsyntax" in lowered:
            msg = "交易數量格式錯誤，請輸入有效的數字"
            lowered = msg.lower()
        if "spot position" in lowered or "持倉不足" in lowered:
            status = 409
            msg = "現貨持倉不足，請降低賣出數量或確認可賣現貨。"
        elif lowered.startswith("margin open funds insufficient"):
            fields = dict(re.findall(r"(required|available|collateral|fee|max_collateral)=([0-9]+)", msg))
            required = fields.get("required", "-")
            available = fields.get("available", "-")
            collateral = fields.get("collateral", "-")
            fee = fields.get("fee", "-")
            max_collateral = fields.get("max_collateral", "0")
            msg = (
                "可用資金不足：開倉費需另扣，"
                f"實際預扣為保證金 {collateral} 點 + 開倉費 {fee} 點 = {required} 點；"
                f"目前可用 {available} 點，保證金最多可填 {max_collateral} 點。"
            )
            status = 409
        elif "insufficient" in lowered or "不足" in lowered:
            status = 409
            if msg.startswith("root 模擬交易資金不足"):
                msg = msg
            elif "root simulated trading points" in lowered:
                msg = "root 模擬交易資金不足，請降低保證金/數量，或在交易所重置 root 模擬資金"
            elif msg.startswith("交易資金不足"):
                msg = msg
            else:
                msg = "交易資金不足，請降低數量或補足可用積分"
        if "safe mode" in lowered:
            status = 423
        if lowered.startswith("collateral below minimum"):
            minimum = msg.rsplit(" ", 1)[-1]
            msg = f"保證金不足，至少需要 {minimum} 點"
            status = 400
        elif lowered.startswith("borrow trading is disabled"):
            msg = "進階交易尚未啟用，請由 root 到設定 > 計費 > 交易所參數開啟借貸交易"
            status = 403
        elif lowered.startswith("contract trading is disabled"):
            msg = "合約交易尚未啟用，請由 root 到設定 > 計費 > 交易所參數開啟 futures_enabled"
            status = 403
        elif lowered.startswith("bot_type must be"):
            msg = "交易機器人類型錯誤，請重新選擇定投或 Workflow 機器人"
            status = 400
        elif lowered.startswith("bot side must be"):
            msg = "交易機器人方向錯誤，請選擇買入或賣出"
            status = 400
        elif lowered.startswith("bot order_type must be"):
            msg = "交易機器人訂單類型錯誤，請選擇市價單或限價單"
            status = 400
        elif lowered.startswith("bot trigger_type must be"):
            msg = "交易機器人觸發條件錯誤，請重新設定價格條件"
            status = 400
        elif lowered.startswith("dca budget_points must be positive"):
            msg = "定投機器人每次投入點數必須大於 0"
            status = 400
        elif lowered.startswith("max_runs must be"):
            msg = "交易機器人最多執行次數必須是 -1（不限制）或 1 到 1000 之間的整數"
            status = 400
        elif lowered.startswith("cooldown_seconds must be"):
            msg = "交易機器人冷卻秒數必須是 0 到 86400 之間的整數"
            status = 400
        elif lowered.startswith("interval_hours must be"):
            msg = "定投間隔小時必須是 1 到 8760 之間的整數"
            status = 400
        elif lowered.startswith("price_upper_limit must be"):
            msg = "定投價格上限必須是大於 0 的有效價格"
            status = 400
        elif lowered.startswith("price_lower_limit must be"):
            msg = "定投價格下限必須是大於 0 的有效價格"
            status = 400
        elif lowered.startswith("price_lower_limit must not exceed price_upper_limit"):
            msg = "定投價格下限不可高於價格上限"
            status = 400
        elif lowered.startswith("max_daily_runs must be"):
            msg = "每日最大觸發次數必須是 1 到 100 之間的整數"
            status = 400
        elif lowered.startswith("workflow graph"):
            msg = f"Workflow 設定錯誤：{msg}"
            status = 400
        elif lowered.startswith("position_type must be"):
            msg = "進階交易類型錯誤，請選擇融資買入或借券放空"
            status = 400
        elif lowered.startswith("quantity must be"):
            msg = "交易數量必須是大於 0 的數字"
            status = 400
        elif lowered.startswith("collateral_points must be"):
            msg = "保證金必須是大於 0 的整數"
            status = 400
        if lowered.startswith("market not found"):
            msg = "交易市場不存在，請重新整理交易所參數後再試"
            status = 400
        elif "not found" in lowered:
            status = 404
        if "another user" in lowered or "another user's" in lowered:
            status = 403
        return json_resp({"ok": False, "msg": msg}), status

    def price_to_points(value):
        return round(float(value) * USDT_TO_POINTS_RATE, 8)

    def display_market_symbol(symbol):
        renderer = getattr(trading_service, "market_display_symbol", None)
        if callable(renderer):
            return renderer(symbol)
        normalized = normalize_market_symbol_for_route(symbol)
        if normalized.endswith("/POINTS"):
            return normalized[:-7] + "/USDT"
        return normalized

    def fetch_json_url(url, *, timeout=6, user_agent="hackme_web/1.0 reference-price-proxy"):
        req = Request(url, headers={"User-Agent": user_agent})
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def candle_payload(open_time_ms, open_price, high_price, low_price, close_price):
        open_price = float(open_price)
        high_price = float(high_price)
        low_price = float(low_price)
        close_price = float(close_price)
        if min(open_price, high_price, low_price, close_price) <= 0:
            return None
        return {
            "time": int(open_time_ms),
            "time_iso": datetime.fromtimestamp(int(open_time_ms) / 1000, tz=timezone.utc).isoformat(),
            "open_usdt": open_price,
            "high_usdt": high_price,
            "low_usdt": low_price,
            "close_usdt": close_price,
            "open_points": price_to_points(open_price),
            "high_points": price_to_points(high_price),
            "low_points": price_to_points(low_price),
            "close_points": price_to_points(close_price),
            "price_usdt": close_price,
            "price_points": price_to_points(close_price),
        }

    def fetch_binance_reference_candles(binance_symbol, interval, limit, *, start_ms=None, end_ms=None):
        interval_ms = INTERVAL_MILLISECONDS.get(interval)
        if not interval_ms:
            raise ValueError(f"fetch_binance_reference_candles: unsupported interval {interval!r}")
        raw_pages = []
        if start_ms is not None:
            # Forward pagination from start_ms
            cur_start = int(start_ms)
            remaining = limit
            while remaining > 0:
                page_limit = min(remaining, BINANCE_MAX_CANDLES_PER_REQUEST)
                query_data = {"symbol": binance_symbol, "interval": interval, "limit": page_limit, "startTime": cur_start}
                if end_ms is not None:
                    query_data["endTime"] = int(end_ms)
                payload = fetch_json_url(f"{BINANCE_KLINES_URL}?{urlencode(query_data)}")
                if not isinstance(payload, list) or not payload:
                    break
                raw_pages.extend(payload)
                remaining -= len(payload)
                if len(payload) < page_limit:
                    break
                cur_start = int(payload[-1][0]) + interval_ms
                if end_ms is not None and cur_start > int(end_ms):
                    break
        else:
            # Backward pagination from end_ms (or current time) to collect most-recent limit candles
            cur_end = int(end_ms) if end_ms is not None else None
            remaining = limit
            while remaining > 0:
                page_limit = min(remaining, BINANCE_MAX_CANDLES_PER_REQUEST)
                query_data = {"symbol": binance_symbol, "interval": interval, "limit": page_limit}
                if cur_end is not None:
                    query_data["endTime"] = cur_end
                payload = fetch_json_url(f"{BINANCE_KLINES_URL}?{urlencode(query_data)}")
                if not isinstance(payload, list) or not payload:
                    break
                raw_pages = payload + raw_pages   # prepend so order stays chronological
                remaining -= len(payload)
                if len(payload) < page_limit:
                    break
                # set end for the next (earlier) page to just before the oldest candle we have
                cur_end = int(payload[0][0]) - 1
        candles = []
        for item in raw_pages[-limit:]:
            try:
                candle = candle_payload(int(item[0]), item[1], item[2], item[3], item[4])
            except Exception:
                continue
            if candle:
                candles.append(candle)
        return {"source": "binance_public_api", "symbol": binance_symbol, "candles": candles}

    def fetch_okx_reference_candles(instrument, interval, limit):
        bar = OKX_BAR_INTERVALS.get(interval)
        if not instrument or not bar:
            raise ValueError("OKX 不支援此市場或週期")
        query = urlencode({"instId": instrument, "bar": bar, "limit": limit})
        payload = fetch_json_url(
            f"{OKX_CANDLES_URL}?{query}",
            user_agent="hackme_web/1.0 reference-price-proxy okx",
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("OKX 參考價格格式錯誤")
        candles = []
        for item in sorted(data, key=lambda row: int(float(row[0])) if isinstance(row, list) and row else 0)[-limit:]:
            try:
                candle = candle_payload(int(float(item[0])), item[1], item[2], item[3], item[4])
            except Exception:
                continue
            if candle:
                candles.append(candle)
        return {"source": "okx_public_api", "symbol": instrument, "candles": candles}

    def fetch_coinbase_reference_candles(product_id, interval, limit):
        granularity = COINBASE_GRANULARITY_SECONDS.get(interval)
        if not product_id or not granularity:
            raise ValueError("Coinbase 不支援此市場或週期")
        query = urlencode({"granularity": granularity})
        payload = fetch_json_url(
            f"{COINBASE_CANDLES_URL_TEMPLATE.format(product_id=product_id)}?{query}",
            user_agent="hackme_web/1.0 reference-price-proxy coinbase",
        )
        if not isinstance(payload, list):
            raise ValueError("Coinbase 參考價格格式錯誤")
        candles = []
        for item in sorted(payload, key=lambda row: int(float(row[0])) if isinstance(row, list) and row else 0)[-limit:]:
            try:
                candle = candle_payload(int(float(item[0])) * 1000, item[3], item[2], item[1], item[4])
            except Exception:
                continue
            if candle:
                candles.append(candle)
        return {"source": "coinbase_exchange", "symbol": product_id, "candles": candles}

    def fetch_kraken_reference_candles(pair, interval, limit):
        minutes = KRAKEN_INTERVAL_MINUTES.get(interval)
        if not pair or not minutes:
            raise ValueError("Kraken 不支援此市場或週期")
        query = urlencode({"pair": pair, "interval": minutes})
        payload = fetch_json_url(
            f"{KRAKEN_OHLC_URL}?{query}",
            user_agent="hackme_web/1.0 reference-price-proxy kraken",
        )
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError(f"Kraken 參考價格錯誤：{payload.get('error') if isinstance(payload, dict) else 'invalid payload'}")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        ohlc = []
        provider_symbol = pair
        for key, value in result.items():
            if key == "last":
                continue
            provider_symbol = key
            ohlc = value if isinstance(value, list) else []
            break
        candles = []
        for item in ohlc[-limit:]:
            try:
                candle = candle_payload(int(float(item[0])) * 1000, item[1], item[2], item[3], item[4])
            except Exception:
                continue
            if candle:
                candles.append(candle)
        return {"source": "kraken_public_api", "symbol": provider_symbol, "candles": candles}

    def fetch_gemini_reference_candles(symbol, interval, limit):
        timeframe = GEMINI_TIMEFRAMES.get(interval)
        if not symbol or not timeframe:
            raise ValueError("Gemini 不支援此市場或週期")
        payload = fetch_json_url(
            GEMINI_CANDLES_URL_TEMPLATE.format(symbol=symbol, timeframe=timeframe),
            user_agent="hackme_web/1.0 reference-price-proxy gemini",
        )
        if not isinstance(payload, list):
            raise ValueError("Gemini 參考價格格式錯誤")
        candles = []
        for item in sorted(payload, key=lambda row: int(float(row[0])) if isinstance(row, list) and row else 0)[-limit:]:
            try:
                candle = candle_payload(int(float(item[0])), item[1], item[2], item[3], item[4])
            except Exception:
                continue
            if candle:
                candles.append(candle)
        return {"source": "gemini_public_api", "symbol": symbol, "candles": candles}

    def fetch_bitstamp_reference_candles(pair, interval, limit):
        step = BITSTAMP_STEP_SECONDS.get(interval)
        if not pair or not step:
            raise ValueError("Bitstamp 不支援此市場或週期")
        query = urlencode({"step": step, "limit": limit})
        payload = fetch_json_url(
            f"{BITSTAMP_OHLC_URL_TEMPLATE.format(pair=pair)}?{query}",
            user_agent="hackme_web/1.0 reference-price-proxy bitstamp",
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        ohlc = data.get("ohlc") if isinstance(data, dict) else None
        if not isinstance(ohlc, list):
            raise ValueError("Bitstamp 參考價格格式錯誤")
        candles = []
        for item in sorted(ohlc, key=lambda row: int(float(row.get("timestamp", 0))) if isinstance(row, dict) else 0)[-limit:]:
            try:
                candle = candle_payload(int(float(item["timestamp"])) * 1000, item["open"], item["high"], item["low"], item["close"])
            except Exception:
                continue
            if candle:
                candles.append(candle)
        return {"source": "bitstamp_public_api", "symbol": pair, "candles": candles}

    def fetch_reference_candles_with_fallback(market_symbol, interval, limit):
        market_symbol = normalize_market_symbol_for_route(market_symbol)
        providers = [
            ("binance_public_api", lambda: fetch_binance_reference_candles(market_provider_id_for_route(market_symbol, "binance_public_api"), interval, limit)),
            ("okx_public_api", lambda: fetch_okx_reference_candles(market_provider_id_for_route(market_symbol, "okx_public_api"), interval, limit)),
            ("coinbase_exchange", lambda: fetch_coinbase_reference_candles(market_provider_id_for_route(market_symbol, "coinbase_exchange"), interval, limit)),
            ("kraken_public_api", lambda: fetch_kraken_reference_candles(market_provider_id_for_route(market_symbol, "kraken_public_api"), interval, limit)),
            ("gemini_public_api", lambda: fetch_gemini_reference_candles(market_provider_id_for_route(market_symbol, "gemini_public_api"), interval, limit)),
            ("bitstamp_public_api", lambda: fetch_bitstamp_reference_candles(market_provider_id_for_route(market_symbol, "bitstamp_public_api"), interval, limit)),
        ]
        errors = []
        primary_name, primary_provider = providers[0]
        try:
            result = primary_provider()
            if result.get("candles"):
                return result
            errors.append(f"{result.get('source', primary_name)}: no candles")
        except Exception as exc:
            errors.append(str(exc)[:160])

        fallback_providers = providers[1:]
        with ThreadPoolExecutor(max_workers=len(fallback_providers)) as executor:
            futures = {
                executor.submit(provider): provider_name
                for provider_name, provider in fallback_providers
            }
            for future in as_completed(futures):
                provider_name = futures[future]
                try:
                    result = future.result()
                    if result.get("candles"):
                        return result
                    errors.append(f"{result.get('source', provider_name)}: no candles")
                except Exception as exc:
                    errors.append(str(exc)[:160])
        raise ValueError("; ".join(errors) or "all reference price providers failed")

    def parse_time_ms(value):
        if value in (None, ""):
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            number = int(float(text))
            return number if number > 10_000_000_000 else number * 1000
        except Exception:
            pass
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            raise ValueError("backtest time must be ISO datetime or unix timestamp")

    def infer_backtest_candle_limit_from_window(data, interval, active_cap):
        start_ms = parse_time_ms(data.get("start_time"))
        end_ms = parse_time_ms(data.get("end_time"))
        if start_ms is None or end_ms is None:
            return None
        if end_ms <= start_ms:
            raise ValueError("backtest end_time must be after start_time")
        interval_ms = INTERVAL_MILLISECONDS.get(interval)
        if not interval_ms:
            return None
        import math as _math
        span_ms = max(interval_ms, end_ms - start_ms)
        return min(max(2, _math.ceil(span_ms / interval_ms)), active_cap)

    def fetch_reference_candles_for_backtest(data):
        market_symbol = normalize_market_symbol_for_route(data.get("market_symbol") or data.get("market") or "BTC/USDT")
        if not market_supports_reference_price_for_route(market_symbol):
            raise ValueError("unsupported backtest market")
        interval = str(data.get("timeframe") or data.get("interval") or "15m").strip()
        if interval not in REFERENCE_PRICE_INTERVALS:
            raise ValueError("unsupported backtest interval")
        mins_per_candle = INTERVAL_MINUTES.get(interval, 15)
        get_max_backtest_candles = getattr(trading_service, "get_max_backtest_candles", None)
        active_cap = int(get_max_backtest_candles()) if callable(get_max_backtest_candles) else MAX_BACKTEST_CANDLES
        max_days_for_interval = round(active_cap * mins_per_candle / 1440, 1)
        # Accept either candle count (limit/candle_limit) or human-readable days
        days_raw = data.get("days") or data.get("backtest_days")
        limit_raw = data.get("limit") or data.get("candle_limit")
        if days_raw is not None:
            try:
                days_val = float(days_raw)
            except Exception:
                raise ValueError("backtest days 格式錯誤，請輸入數字（例如 30）")
            if days_val <= 0:
                raise ValueError("backtest days 必須大於 0")
            import math as _math
            limit = min(_math.ceil(days_val * 1440 / mins_per_candle), active_cap)
        elif limit_raw is not None:
            try:
                limit = int(limit_raw)
            except Exception:
                raise ValueError("backtest candle limit 格式錯誤")
        else:
            inferred_limit = infer_backtest_candle_limit_from_window(data, interval, active_cap)
            limit = inferred_limit if inferred_limit is not None else min(500, active_cap)
        if limit > active_cap:
            raise ValueError(
                f"回測長度超過上限：{active_cap} 根 K 棒"
                f"（{interval} 間隔最多可回測 {max_days_for_interval} 天）"
            )
        download_limit = max(2, min(limit, BACKTEST_PROVIDER_CANDLE_LIMIT))
        start_ms = parse_time_ms(data.get("start_time"))
        end_ms = parse_time_ms(data.get("end_time"))
        if start_ms is not None or end_ms is not None:
            try:
                provider_result = fetch_binance_reference_candles(
                    market_provider_id_for_route(market_symbol, "binance_public_api"),
                    interval,
                    download_limit,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            except Exception:
                provider_result = fetch_reference_candles_with_fallback(market_symbol, interval, download_limit)
        else:
            provider_result = fetch_reference_candles_with_fallback(market_symbol, interval, download_limit)
        candles = provider_result.get("candles") or []
        if len(candles) < 2:
            raise ValueError("backtest price provider returned too few candles")
        actual_days = round(len(candles) * mins_per_candle / 1440, 1)
        return {
            "source": provider_result.get("source") or "unknown_price_provider",
            "symbol": provider_result.get("symbol") or market_symbol,
            "candles": candles,
            "requested_limit": limit,
            "download_limit": download_limit,
            "interval": interval,
            "mins_per_candle": mins_per_candle,
            "actual_candle_count": len(candles),
            "actual_days": actual_days,
            "max_backtest_candles": active_cap,
            "max_backtest_candles_per_batch": BACKTEST_SEGMENT_CANDLES,
            "max_backtest_days": max_days_for_interval,
        }

    def run_backtest_payload(actor, data, *, default_auto_fetch_reference=False):
        payload = dict(data or {})
        fetched_meta = {}
        candles = payload.get("candles")
        auto_fetch_reference = str(payload.get("auto_fetch_reference_candles") or "").strip().lower() in {"1", "true", "yes", "on"}
        if default_auto_fetch_reference and (not isinstance(candles, list) or not candles):
            auto_fetch_reference = True
            payload["auto_fetch_reference_candles"] = True
        if isinstance(candles, list) and candles and len(candles) < 2:
            raise ValueError("candles are required for backtest")
        if (not isinstance(candles, list) or not candles) and auto_fetch_reference:
            fetched = fetch_reference_candles_for_backtest(payload)
            payload["candles"] = fetched["candles"]
            payload["data_source"] = fetched["source"]
            payload["provider_symbol"] = fetched["symbol"]
            payload["requested_candle_limit"] = fetched.get("requested_limit")
            payload["download_candle_limit"] = fetched.get("download_limit")
            fetched_meta = {
                "interval": fetched.get("interval"),
                "mins_per_candle": fetched.get("mins_per_candle"),
                "actual_candle_count": fetched.get("actual_candle_count"),
                "actual_backtest_days": fetched.get("actual_days"),
                "max_backtest_candles": fetched.get("max_backtest_candles"),
                "max_backtest_candles_per_batch": fetched.get("max_backtest_candles_per_batch"),
                "max_backtest_days": fetched.get("max_backtest_days"),
            }
        result = trading_service.backtest_trading_bot(actor=actor, payload=payload)
        result["data_source"] = result.get("data_source") or payload.get("data_source") or ("browser_loaded_chart" if isinstance((payload or {}).get("candles"), list) else "")
        result["provider_symbol"] = result.get("provider_symbol") or payload.get("provider_symbol") or ""
        get_max_backtest_candles = getattr(trading_service, "get_max_backtest_candles", None)
        active_cap = int(get_max_backtest_candles()) if callable(get_max_backtest_candles) else MAX_BACKTEST_CANDLES
        result["max_backtest_candles"] = active_cap
        result["max_backtest_candles_per_batch"] = result.get("max_backtest_candles_per_batch") or fetched_meta.get("max_backtest_candles_per_batch") or BACKTEST_SEGMENT_CANDLES
        result["provider_candle_limit"] = BACKTEST_PROVIDER_CANDLE_LIMIT
        result["requested_candle_limit"] = payload.get("requested_candle_limit") or payload.get("candle_limit") or payload.get("limit") or len(payload.get("candles") or [])
        result["download_candle_limit"] = payload.get("download_candle_limit") or len(payload.get("candles") or [])
        interval_key = fetched_meta.get("interval") or str(payload.get("timeframe") or payload.get("interval") or "15m").strip()
        mins_per_candle = fetched_meta.get("mins_per_candle") or INTERVAL_MINUTES.get(interval_key, 15)
        n_candles = result.get("candle_count") or len(payload.get("candles") or [])
        result["interval"] = interval_key
        result["backtest_window_days"] = round(n_candles * mins_per_candle / 1440, 1)
        result["max_backtest_days"] = fetched_meta.get("max_backtest_days") or round(active_cap * mins_per_candle / 1440, 1)
        result["backtest_limits"] = {
            iv: {"max_candles": active_cap, "max_days": round(active_cap * m / 1440, 1)}
            for iv, m in INTERVAL_MINUTES.items()
        }
        return result

    @app.route("/api/trading/markets", methods=["GET"])
    @require_csrf_safe
    def trading_markets():
        actor, err = actor_or_401()
        if err:
            return err
        return json_resp({"ok": True, "markets": trading_service.list_markets()})

    @app.route("/api/trading/dashboard", methods=["GET"])
    @require_csrf_safe
    def trading_dashboard():
        actor, err = actor_or_401()
        if err:
            return err
        return json_resp({
            "ok": True,
            "trading": trading_service.user_dashboard(
                user_id=actor["id"],
                source_wallet_address=request.args.get("source_wallet_address") or "",
            ),
        })

    @app.route("/api/trading/asset-overview", methods=["GET"])
    @require_csrf_safe
    def trading_asset_overview():
        actor, err = actor_or_401()
        if err:
            return err
        overview = trading_service.user_asset_overview(user_id=actor["id"])
        return json_resp({"ok": True, "overview": overview, "trading": {"overview": overview}})

    @app.route("/api/admin/trading/asset-overview", methods=["GET"])
    @require_csrf_safe
    def admin_trading_asset_overview():
        actor, err = manager_or_403()
        if err:
            return err
        conn = trading_service.get_db()
        try:
            trading_service.ensure_schema(conn)
            accounts = conn.execute(
                """
                SELECT
                    COUNT(*) AS account_count,
                    COALESCE(SUM(balance_points), 0) AS total_available_points,
                    COALESCE(SUM(locked_points), 0) AS total_locked_points
                FROM trading_sim_accounts
                """
            ).fetchone()
            open_orders = conn.execute(
                "SELECT COUNT(*) AS count FROM trading_orders WHERE status IN ('open', 'partially_filled')"
            ).fetchone()
            open_positions = 0
            total_margin_principal = 0
            total_margin_collateral = 0
            total_margin_interest = 0
            if table_columns(conn, "trading_margin_positions"):
                margin = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS open_positions,
                        COALESCE(SUM(principal_points), 0) AS total_principal,
                        COALESCE(SUM(collateral_points), 0) AS total_collateral,
                        COALESCE(SUM(interest_points - interest_paid_points), 0) AS total_interest
                    FROM trading_margin_positions
                    WHERE status='open'
                    """
                ).fetchone()
                open_positions = int(margin["open_positions"] or 0)
                total_margin_principal = int(margin["total_principal"] or 0)
                total_margin_collateral = int(margin["total_collateral"] or 0)
                total_margin_interest = int(margin["total_interest"] or 0)
            market_count = 0
            low_confidence = 0
            try:
                markets = trading_service.list_markets()
                market_count = len(markets or [])
                for market in markets or []:
                    context = market.get("price_context") if isinstance(market, dict) else {}
                    confidence = str((context or {}).get("confidence") or market.get("price_confidence") or "").lower()
                    if confidence in {"low", "very_low", "untrusted", "unknown"}:
                        low_confidence += 1
            except Exception:
                market_count = 0
                low_confidence = 0
            return json_resp({
                "ok": True,
                "risk": {
                    "account_count": int(accounts["account_count"] or 0),
                    "total_available_points": int(accounts["total_available_points"] or 0),
                    "total_locked_points": int(accounts["total_locked_points"] or 0),
                    "open_order_count": int(open_orders["count"] or 0),
                    "open_margin_positions": open_positions,
                    "total_margin_principal_points": total_margin_principal,
                    "total_margin_collateral_points": total_margin_collateral,
                    "total_margin_interest_due_points": total_margin_interest,
                    "market_count": market_count,
                    "low_confidence_price_count": low_confidence,
                    "confidence_note": "價格可信度只作管理提示，不阻擋積分交易。",
                    "viewer_user_id": int(actor["id"]),
                },
            })
        finally:
            conn.close()

    @app.route("/api/trading/live-price", methods=["GET"])
    @require_csrf_safe
    def trading_live_price():
        actor, err = actor_or_401()
        if err:
            return err
        market_symbol = request.args.get("market") or request.args.get("market_symbol") or ""
        try:
            quote = trading_service.get_live_market_quote(market_symbol=market_symbol)
            return json_resp({"ok": True, **quote})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/history/export.csv", methods=["GET"])
    @require_csrf_safe
    def trading_history_export_csv():
        actor, err = actor_or_401()
        if err:
            return err
        user_id = int(actor["id"])
        conn = trading_service.get_db()
        rows = []
        try:
            trading_service.ensure_schema(conn)
            for row in conn.execute(
                """
                SELECT * FROM trading_orders
                WHERE user_id=?
                ORDER BY created_at DESC, id DESC
                LIMIT 10000
                """,
                (user_id,),
            ).fetchall():
                rows.append({
                    "record_type": "order",
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "market_symbol": row["market_symbol"],
                    "side": row["side"],
                    "order_type": row["order_type"],
                    "status": row["status"],
                    "quantity": units_to_quantity(row["quantity_units"]),
                    "filled_quantity": units_to_quantity(row["filled_quantity_units"]),
                    "limit_price_points": row["limit_price_points"],
                    "execution_price_points": row["execution_price_points"],
                    "notional_points": "",
                    "fee_points": row["fee_points"],
                    "frozen_points": row["frozen_points"],
                    "trial_frozen_points": row["trial_frozen_points"] if "trial_frozen_points" in row.keys() else "",
                    "chain_frozen_points": row["chain_frozen_points"] if "chain_frozen_points" in row.keys() else "",
                    "funding_mode": row["funding_mode"],
                    "execution_mode": row["execution_mode"],
                    "order_uuid": row["order_uuid"],
                    "reason": row["reason"],
                })
            pnl_by_fill = {
                row["fill_id"]: row
                for row in conn.execute(
                    "SELECT * FROM trading_spot_realized_pnl WHERE user_id=?",
                    (user_id,),
                ).fetchall()
            } if table_columns(conn, "trading_spot_realized_pnl") else {}
            fill_rows = conn.execute(
                """
                SELECT f.*, o.order_uuid
                FROM trading_fills f
                JOIN trading_orders o ON o.id=f.order_id
                WHERE f.user_id=?
                ORDER BY f.created_at DESC, f.id DESC
                LIMIT 10000
                """,
                (user_id,),
            ).fetchall()
            for row in fill_rows:
                pnl = pnl_by_fill.get(row["id"])
                rows.append({
                    "record_type": "fill",
                    "created_at": row["created_at"],
                    "market_symbol": row["market_symbol"],
                    "side": row["side"],
                    "quantity": units_to_quantity(row["quantity_units"]),
                    "price_points": row["price_points"],
                    "notional_points": row["notional_points"],
                    "fee_points": row["fee_points"],
                    "reserve_delta_points": row["reserve_delta_points"],
                    "trial_repaid_points": row["trial_repaid_points"] if "trial_repaid_points" in row.keys() else "",
                    "trial_profit_points": row["trial_profit_points"] if "trial_profit_points" in row.keys() else "",
                    "realized_pnl_points": pnl["net_pnl_points"] if pnl else "",
                    "gross_cost_points": pnl["gross_cost_points"] if pnl else "",
                    "gross_proceeds_points": pnl["gross_proceeds_points"] if pnl else "",
                    "buy_fee_estimate_points": pnl["buy_fee_estimate_points"] if pnl else "",
                    "sell_fee_points": pnl["sell_fee_points"] if pnl else "",
                    "funding_mode": row["funding_mode"],
                    "order_uuid": row["order_uuid"],
                    "fill_uuid": row["fill_uuid"],
                    "points_ledger_uuids": row["points_ledger_uuids_json"],
                })
            if table_columns(conn, "trading_margin_positions"):
                for row in conn.execute(
                    """
                    SELECT * FROM trading_margin_positions
                    WHERE user_id=?
                    ORDER BY opened_at DESC, id DESC
                    LIMIT 10000
                    """,
                    (user_id,),
                ).fetchall():
                    rows.append({
                        "record_type": "margin_position",
                        "created_at": row["opened_at"],
                        "updated_at": row["updated_at"],
                        "closed_at": row["closed_at"],
                        "market_symbol": row["market_symbol"],
                        "side": "margin_long" if row["position_type"] == "margin_long" else "short",
                        "status": row["status"],
                        "quantity": units_to_quantity(row["quantity_units"]),
                        "price_points": row["entry_price_points"],
                        "exit_price_points": row["exit_price_points"],
                        "principal_points": row["principal_points"],
                        "collateral_points": row["collateral_points"],
                        "fee_points": row["open_fee_points"],
                        "close_fee_points": row["close_fee_points"],
                        "interest_percent_daily": row["interest_percent_daily"],
                        "interest_points": row["interest_points"],
                        "interest_paid_points": row["interest_paid_points"],
                        "realized_pnl_points": row["realized_pnl_points"],
                        "position_uuid": row["position_uuid"],
                    })
            rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        finally:
            conn.close()
        fieldnames = [
            "record_type",
            "created_at",
            "updated_at",
            "closed_at",
            "market_symbol",
            "side",
            "order_type",
            "status",
            "quantity",
            "filled_quantity",
            "limit_price_points",
            "execution_price_points",
            "price_points",
            "exit_price_points",
            "notional_points",
            "fee_points",
            "close_fee_points",
            "frozen_points",
            "trial_frozen_points",
            "chain_frozen_points",
            "trial_repaid_points",
            "trial_profit_points",
            "reserve_delta_points",
            "principal_points",
            "collateral_points",
            "interest_percent_daily",
            "interest_points",
            "interest_paid_points",
            "realized_pnl_points",
            "gross_cost_points",
            "gross_proceeds_points",
            "buy_fee_estimate_points",
            "sell_fee_points",
            "funding_mode",
            "execution_mode",
            "order_uuid",
            "fill_uuid",
            "position_uuid",
            "points_ledger_uuids",
            "reason",
        ]
        audit("TRADING_HISTORY_CSV_EXPORTED", get_client_ip(), user=actor_value(actor, "username", ""), success=True, ua=get_ua(), detail=f"user_id={user_id},rows={len(rows)}")
        return csv_download_response(f"trading_history_{actor_value(actor, 'username', 'user')}.csv", fieldnames, rows)

    @app.route("/api/trading/btc-signal", methods=["GET"])
    @require_csrf_safe
    def trading_btc_signal():
        actor, err = actor_or_401()
        if err:
            return err
        market_symbol = normalize_market_symbol_for_route(request.args.get("market") or "BTC/USDT")
        if not market_supports_btc_trade_for_route(market_symbol):
            return json_resp({"ok": True, "available": False, "hidden": True, "msg": "僅 BTC/USDT 顯示 BTC_trade 信號"})
        try:
            settings = trading_service.get_root_settings().get("settings", {})
            if not settings.get("btc_trade_enabled"):
                return json_resp({"ok": True, "available": False, "hidden": True, "msg": "BTC_trade 信號目前未啟用"})
            project_dir = settings.get("btc_trade_project_dir") or str(default_btc_trade_project_dir(Path(__file__).resolve().parents[1]))
            status = btc_trade_status(project_dir)
        except Exception:
            return json_resp({"ok": True, "available": False, "hidden": True, "msg": "BTC_trade 信號暫不可用"})
        return json_resp({
            "ok": True,
            "available": bool(status.get("available")),
            "hidden": not bool(status.get("available")),
            "signal": status.get("signal") if status.get("available") else None,
            "msg": status.get("message") or "",
        })

    @app.route("/api/trading/workflow-templates", methods=["GET"])
    @require_csrf_safe
    def trading_workflow_templates():
        actor, err = actor_or_401()
        if err:
            return err
        templates, errors = list_workflow_templates(actor)
        return json_resp({
            "ok": not bool(errors),
            "templates": templates,
            "system": [item for item in templates if item.get("scope") == "system"],
            "custom": [item for item in templates if item.get("scope") == "custom"],
            "workflow_root": "workflows",
            "system_workflow_root": "workflows/trading_bot",
            "custom_workflow_root": workflow_custom_root_label(),
            "errors": errors,
        })

    @app.route("/api/trading/workflow-template-benchmarks", methods=["GET"])
    @require_csrf_safe
    def trading_workflow_template_benchmarks():
        actor, err = actor_or_401()
        if err:
            return err
        try:
            if not WORKFLOW_TEMPLATE_BENCHMARK_PATH.exists():
                return json_resp({
                    "ok": True,
                    "windows": [],
                    "load_error": "尚未提供 Workflow 歷史回測報告。",
                    "source_path": "workflows/trading_bot/benchmarks/workflow_template_benchmarks.json",
                })
            payload = json.loads(WORKFLOW_TEMPLATE_BENCHMARK_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("benchmark payload must be an object")
        except Exception as exc:
            return json_resp({
                "ok": True,
                "windows": [],
                "load_error": f"Workflow 歷史回測報告讀取失敗：{exc}",
                "source_path": "workflows/trading_bot/benchmarks/workflow_template_benchmarks.json",
            })
        payload["ok"] = True
        payload.setdefault("source_path", "workflows/trading_bot/benchmarks/workflow_template_benchmarks.json")
        return json_resp(payload)

    @app.route("/api/trading/workflow-templates/custom", methods=["POST"])
    @require_csrf
    def trading_workflow_templates_save_custom():
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            workflow = trading_service._validate_workflow(data.get("workflow") or data.get("workflow_json"))
            template_id = workflow_template_slug(data.get("id") or data.get("label") or workflow.get("name"), "custom_workflow")
            label = str(data.get("label") or workflow.get("name") or template_id).strip()[:120] or template_id
            description = str(data.get("description") or workflow.get("description") or "").strip()[:500]
            custom_dir = workflow_custom_root() / workflow_user_slug(actor)
            custom_dir.mkdir(parents=True, exist_ok=True)
            path = custom_dir / f"{template_id}.json"
            payload = {
                "id": template_id,
                "label": label,
                "description": description,
                "explanation": data.get("explanation") if isinstance(data.get("explanation"), dict) else {},
                "scope": "custom",
                "workflow": workflow,
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            item = load_workflow_template_file(path, scope="custom")
            audit(
                "TRADING_WORKFLOW_TEMPLATE_SAVED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"template_id={template_id}, path={relative_workflow_path(path)}",
            )
            return json_resp({
                "ok": True,
                "template": item,
                "custom_workflow_root": workflow_custom_root_label(),
                "msg": f"Workflow 自訂模板已儲存到 {workflow_custom_root_label()}",
            })
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/workflow-editor/backtest", methods=["POST"])
    @require_csrf
    def trading_workflow_editor_backtest():
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            workflow = data.get("workflow") or data.get("workflow_json")
            if not isinstance(workflow, dict):
                raise ValueError("請先完成 Workflow JSON 後再執行回測")
            validated = trading_service._validate_workflow(workflow)
            preview_payload = {
                "market_symbol": data.get("market_symbol") or data.get("market") or "BTC/USDT",
                "strategy": "workflow",
                "workflow_json": validated,
                "initial_cash_points": data.get("initial_cash_points") or 10000,
                "timeframe": data.get("timeframe") or data.get("interval") or "1h",
                "start_time": data.get("start_time") or "",
                "end_time": data.get("end_time") or "",
                "slippage_percent": data.get("slippage_percent") or 0,
                "candle_limit": data.get("candle_limit"),
                "auto_fetch_reference_candles": True,
            }
            result = run_backtest_payload(actor, preview_payload, default_auto_fetch_reference=True)
            audit(
                "TRADING_WORKFLOW_EDITOR_BACKTEST",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"market={result.get('market_symbol')}, strategy=workflow, trades={result.get('trade_count')}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/reference-prices", methods=["GET"])
    @require_csrf_safe
    def trading_reference_prices():
        actor, err = actor_or_401()
        if err:
            return err
        blocked, info = check_user_rate_limit(actor_value(actor, "id", 0), "trading_reference_prices", max_req=120, window_sec=60)
        if blocked:
            retry_after = int(info.get("retry_after", 60)) if isinstance(info, dict) else 60
            return json_resp({
                "ok": False,
                "msg": "參考價格查詢過於頻繁，請稍後再試",
                "retry_after": retry_after,
            }), 429
        market_symbol = normalize_market_symbol_for_route(request.args.get("market") or "BTC/USDT")
        binance_symbol = market_provider_id_for_route(market_symbol, "binance_public_api")
        if not binance_symbol or not market_supports_reference_price_for_route(market_symbol):
            return json_resp({"ok": False, "msg": "不支援的參考價格市場"}), 400
        interval = str(request.args.get("interval") or "15m").strip()
        if interval not in REFERENCE_PRICE_INTERVALS:
            return json_resp({"ok": False, "msg": "不支援的參考價格週期"}), 400
        latest_only = str(request.args.get("latest") or "").strip().lower() in {"1", "true", "yes"}
        try:
            limit = int(request.args.get("limit") or 60)
        except Exception:
            return json_resp({"ok": False, "msg": "參考價格筆數格式錯誤"}), 400
        limit = 1 if latest_only else max(12, min(limit, 96))
        cache_key = (binance_symbol, interval, limit, latest_only)
        now = time.monotonic()
        cached = REFERENCE_PRICE_CACHE.get(cache_key)
        if cached and now - cached["cached_at"] <= REFERENCE_PRICE_CACHE_TTL_SECONDS:
            return json_resp(cached["payload"])
        try:
            provider_result = fetch_reference_candles_with_fallback(market_symbol, interval, limit)
        except Exception as exc:
            if cached:
                fallback = dict(cached["payload"])
                fallback["source"] = f"{fallback.get('source') or 'reference_price'}_cached"
                fallback["price_type"] = "reference"
                fallback["confidence"] = "low"
                fallback["stale"] = True
                fallback["degraded"] = True
                fallback["provider_count"] = 1
                fallback["msg"] = "參考價格來源暫時無法讀取，已使用最後可用快取"
                fallback["cache_age_seconds"] = round(now - cached["cached_at"], 3)
                fallback["price_context"] = {
                    "price_type": "reference",
                    "source": fallback["source"],
                    "source_label": "參考價格快取",
                    "confidence": "low",
                    "stale": True,
                    "degraded": True,
                    "provider_count": 1,
                    "purpose": "展示 / 一般估值 / K 線 / 非風控參考",
                    "warning_message": fallback["msg"],
                }
                return json_resp(fallback)
            return json_resp({"ok": False, "msg": "參考價格讀取失敗", "detail": str(exc)[:240]}), 502
        candles = provider_result["candles"]
        result = {
            "ok": True,
            "source": provider_result["source"],
            "price_type": "reference",
            "confidence": "medium",
            "stale": False,
            "degraded": False,
            "provider_count": 1,
            "market": market_symbol,
            "display_market": display_market_symbol(market_symbol),
            "symbol": provider_result["symbol"],
            "interval": interval,
            "latest_only": latest_only,
            "usdt_to_points_rate": USDT_TO_POINTS_RATE,
            "candles": candles,
            "points": candles,
            "price_context": {
                "price_type": "reference",
                "source": provider_result["source"],
                "source_label": "參考價格來源",
                "confidence": "medium",
                "stale": False,
                "degraded": False,
                "provider_count": 1,
                "purpose": "展示 / 一般估值 / K 線 / 非風控參考",
                "warning_message": "",
            },
        }
        REFERENCE_PRICE_CACHE[cache_key] = {"cached_at": now, "payload": result}
        return json_resp(result)

    @app.route("/api/trading/orders", methods=["POST"])
    @require_csrf
    def trading_place_order():
        actor, err = actor_or_401()
        if err:
            return err
        restricted = trading_restriction_response(actor)
        if restricted:
            return restricted
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.place_order(
                actor=actor,
                market_symbol=data.get("market_symbol"),
                side=data.get("side"),
                order_type=data.get("order_type"),
                quantity=data.get("quantity"),
                limit_price_points=data.get("limit_price_points"),
                stop_loss_percent=data.get("stop_loss_percent"),
                take_profit_percent=data.get("take_profit_percent"),
                emergency_close=bool(data.get("emergency_close")),
                source_wallet_address=data.get("source_wallet_address") or "",
            )
            audit(
                "TRADING_ORDER_SUBMITTED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"order_uuid={result['order'].get('order_uuid')}, market={result['order'].get('market_symbol')}, side={result['order'].get('side')}, status={result['order'].get('status')}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/orders/<order_uuid>/cancel", methods=["POST"])
    @require_csrf
    def trading_cancel_order(order_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        try:
            result = trading_service.cancel_order(actor=actor, order_uuid=order_uuid)
            audit("TRADING_ORDER_CANCELLED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"order_uuid={order_uuid}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bots", methods=["GET"])
    @require_csrf_safe
    def trading_bots_list():
        actor, err = actor_or_401()
        if err:
            return err
        try:
            return json_resp(trading_service.list_trading_bots(actor=actor))
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bot-competition", methods=["GET"])
    @require_csrf_safe
    def trading_bot_competition():
        actor, err = actor_or_401()
        if err:
            return err
        try:
            week = str(request.args.get("week") or "").strip() or None
            return json_resp(trading_service.get_bot_competition(actor=actor, week=week))
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bots", methods=["POST"])
    @require_csrf
    def trading_bots_create():
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.save_trading_bot(actor=actor, payload=data)
            bot = result.get("bot") or {}
            if bot.get("bot_type") == "dca" and bot.get("enabled"):
                try:
                    result["initial_run"] = trading_service.run_trading_bot_once(actor=actor, bot_uuid=bot.get("bot_uuid"))
                except Exception as initial_exc:
                    result["initial_run"] = {
                        "ok": False,
                        "scanned": 1,
                        "triggered": [],
                        "skipped": [],
                        "failed": [{"bot_uuid": bot.get("bot_uuid"), "error": str(initial_exc)}],
                    }
                    result["msg"] = f"定投機器人已建立，但首次執行失敗：{initial_exc}"
            audit("TRADING_BOT_CREATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"bot_uuid={result['bot'].get('bot_uuid')}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bots/backtest", methods=["POST"])
    @require_csrf
    def trading_bots_backtest():
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = run_backtest_payload(actor, data)
            audit(
                "TRADING_BOT_BACKTEST",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"market={result.get('market_symbol')}, strategy={result.get('strategy')}, trades={result.get('trade_count')}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bots/<bot_uuid>/share", methods=["POST"])
    @require_csrf
    def trading_bots_share_parameters(bot_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.set_trading_bot_share_parameters(
                actor=actor,
                bot_uuid=bot_uuid,
                share_parameters=bool(data.get("share_parameters")),
            )
            audit(
                "TRADING_BOT_PARAMETER_SHARE_UPDATED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"bot_uuid={bot_uuid}, share_parameters={bool(data.get('share_parameters'))}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bots/<bot_uuid>", methods=["PUT"])
    @require_csrf
    def trading_bots_update(bot_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.save_trading_bot(actor=actor, payload=data, bot_uuid=bot_uuid)
            bot = result.get("bot") or {}
            if bot.get("bot_type") == "dca" and bot.get("enabled"):
                try:
                    result["initial_run"] = trading_service.run_trading_bot_once(actor=actor, bot_uuid=bot.get("bot_uuid"))
                except Exception as run_exc:
                    result["initial_run"] = {"ok": False, "scanned": 1, "triggered": [], "skipped": [], "failed": [{"bot_uuid": bot.get("bot_uuid"), "error": str(run_exc)}]}
            audit("TRADING_BOT_UPDATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"bot_uuid={bot_uuid}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bots/<bot_uuid>", methods=["DELETE"])
    @require_csrf
    def trading_bots_delete(bot_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        try:
            result = trading_service.delete_trading_bot(actor=actor, bot_uuid=bot_uuid)
            audit("TRADING_BOT_DELETED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"bot_uuid={bot_uuid}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bots/<bot_uuid>/increase-runs", methods=["POST"])
    @require_csrf
    def trading_bots_increase_runs(bot_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            delta = data.get("delta", 1)
            result = trading_service.increase_trading_bot_max_runs(actor=actor, bot_uuid=bot_uuid, delta=delta)
            audit(
                "TRADING_BOT_MAX_RUNS_INCREASED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"bot_uuid={bot_uuid}, delta={result.get('delta')}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bots/<bot_uuid>/budget", methods=["POST"])
    @require_csrf
    def trading_bots_budget(bot_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.adjust_trading_bot_budget(
                actor=actor,
                bot_uuid=bot_uuid,
                budget_points=data.get("budget_points") if "budget_points" in data else None,
                delta_points=data.get("delta_points") if "delta_points" in data else None,
            )
            audit(
                "TRADING_BOT_BUDGET_UPDATED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"bot_uuid={bot_uuid}, delta_points={result.get('delta_points')}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/bots/scan", methods=["POST"])
    @require_csrf
    def trading_bots_scan():
        actor, err = actor_or_401()
        if err:
            return err
        data = {}
        if request.data:
            data, err = parse_json_body()
            if err:
                return err
        try:
            result = trading_service.run_trading_bots(
                actor=actor,
                limit=data.get("limit", 50) if isinstance(data, dict) else 50,
            )
            audit(
                "TRADING_BOT_SCAN",
                get_client_ip(),
                user=actor["username"],
                success=bool(result.get("ok")),
                ua=get_ua(),
                detail=f"scanned={result.get('scanned')}, triggered={len(result.get('triggered') or [])}, failed={len(result.get('failed') or [])}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    # ── Grid Bot Routes ────────────────────────────────────────────────────

    @app.route("/api/trading/grid/preview", methods=["POST"])
    @require_csrf
    def trading_grid_preview():
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            return json_resp(trading_service.preview_grid_bot(actor=actor, payload=data))
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/grid-bots", methods=["GET"])
    @require_csrf_safe
    def trading_grid_bots_list():
        actor, err = actor_or_401()
        if err:
            return err
        try:
            return json_resp(trading_service.list_grid_bots(actor=actor))
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/grid-bots", methods=["POST"])
    @require_csrf
    def trading_grid_bots_create():
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.create_grid_bot(actor=actor, payload=data)
            audit("GRID_BOT_CREATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"bot_uuid={(result.get('bot') or {}).get('bot_uuid')}, placed={len(result.get('placed') or [])}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/grid-bots/<bot_uuid>/share", methods=["POST"])
    @require_csrf
    def trading_grid_bots_share_parameters(bot_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.set_grid_bot_share_parameters(
                actor=actor,
                bot_uuid=bot_uuid,
                share_parameters=bool(data.get("share_parameters")),
            )
            audit("GRID_BOT_PARAMETER_SHARE_UPDATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"bot_uuid={bot_uuid}, share_parameters={bool(data.get('share_parameters'))}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/grid-bots/<bot_uuid>/toggle", methods=["POST"])
    @require_csrf
    def trading_grid_bots_toggle(bot_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        enabled = bool(data.get("enabled", True))
        try:
            result = trading_service.toggle_grid_bot(actor=actor, bot_uuid=bot_uuid, enabled=enabled)
            audit("GRID_BOT_TOGGLED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"bot_uuid={bot_uuid}, enabled={enabled}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/grid-bots/<bot_uuid>", methods=["DELETE"])
    @require_csrf
    def trading_grid_bots_delete(bot_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        base_action = str(data.get("base_action") or "keep").strip().lower()
        try:
            result = trading_service.delete_grid_bot(actor=actor, bot_uuid=bot_uuid, base_action=base_action)
            audit("GRID_BOT_DELETED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"bot_uuid={bot_uuid}, base_action={base_action}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/grid-bots/scan", methods=["POST"])
    @require_csrf
    def trading_grid_bots_scan():
        actor, err = actor_or_401()
        if err:
            return err
        try:
            result = trading_service.scan_grid_bots(actor=actor)
            audit("GRID_BOT_SCAN", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"scanned={result.get('scanned')}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/margin/open", methods=["POST"])
    @require_csrf
    def trading_margin_open():
        actor, err = actor_or_401()
        if err:
            return err
        restricted = trading_restriction_response(actor)
        if restricted:
            return restricted
        data, err = parse_json_body()
        if err:
            return err
        idempotency_key = str(data.get("idempotency_key") or request.headers.get("Idempotency-Key") or "").strip()
        if not idempotency_key:
            return json_resp({"ok": False, "msg": "idempotency_key required"}), 400
        try:
            result = trading_service.open_margin_position(
                actor=actor,
                market_symbol=data.get("market_symbol"),
                position_type=data.get("position_type"),
                quantity=data.get("quantity"),
                collateral_points=data.get("collateral_points"),
                stop_loss_percent=data.get("stop_loss_percent"),
                take_profit_percent=data.get("take_profit_percent"),
                idempotency_key=idempotency_key,
            )
            audit("TRADING_MARGIN_POSITION_OPENED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"position_uuid={result['position'].get('position_uuid')}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/margin/<position_uuid>/close", methods=["POST"])
    @require_csrf
    def trading_margin_close(position_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        try:
            result = trading_service.close_margin_position(actor=actor, position_uuid=position_uuid)
            audit("TRADING_MARGIN_POSITION_CLOSED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"position_uuid={position_uuid}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/margin/<position_uuid>/collateral", methods=["POST"])
    @require_csrf
    def trading_margin_add_collateral(position_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.add_margin_collateral(
                actor=actor,
                position_uuid=position_uuid,
                amount_points=data.get("amount_points"),
                idempotency_key=data.get("idempotency_key"),
            )
            audit(
                "TRADING_MARGIN_COLLATERAL_ADDED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"position_uuid={position_uuid}, amount={data.get('amount_points')}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/trading/margin/<position_uuid>/collateral/withdraw", methods=["POST"])
    @require_csrf
    def trading_margin_withdraw_collateral(position_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.withdraw_margin_collateral(
                actor=actor,
                position_uuid=position_uuid,
                amount_points=data.get("amount_points"),
                idempotency_key=data.get("idempotency_key"),
            )
            audit(
                "TRADING_MARGIN_COLLATERAL_WITHDRAWN",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"position_uuid={position_uuid}, amount={data.get('amount_points')}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    def root_trading_snapshot_response(snapshot_key):
        snapshot = trading_service.get_root_trading_snapshot(snapshot_key=snapshot_key)
        if not snapshot.get("ok"):
            return json_resp({
                "ok": False,
                "msg": snapshot.get("msg") or "交易報表快照尚未產生",
                "snapshot": {
                    "snapshot_key": snapshot_key,
                    "missing": True,
                    "required_job_key": "sitewide_metrics_refresh",
                    "client_should_enqueue": True,
                },
            })
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
        response_payload = dict(payload)
        response_payload.setdefault("ok", True)
        response_payload["snapshot"] = {
            "snapshot_key": snapshot_key,
            "generated_at": snapshot.get("generated_at"),
            "source_job_key": snapshot.get("source_job_key"),
            "source_run_uuid": snapshot.get("source_run_uuid"),
            "snapshot_backed": True,
        }
        return json_resp(response_payload)

    @app.route("/api/root/trading/sitewide/refresh", methods=["POST"])
    @require_csrf
    def root_trading_sitewide_refresh():
        actor, err = root_or_403()
        if err:
            return err
        data = request.get_json(silent=True) if request.is_json else {}
        data = data if isinstance(data, dict) else {}
        actor_snapshot = management_actor(actor)
        reason = str(data.get("reason") or "root_manual_sitewide_refresh")

        def worker(progress):
            progress(stage="trading_sitewide_refresh", progress_percent=20, detail="refreshing trading sitewide snapshots")
            result = trading_service.refresh_root_trading_snapshots(
                source_job_key="root_manual_sitewide_refresh"
            )
            progress(stage="snapshot", progress_percent=90, detail="recording trading refresh result")
            return {"ok": True, "refresh": result, "reason": reason}

        def summary(payload):
            refresh = payload.get("refresh") if isinstance(payload.get("refresh"), dict) else {}
            return {
                "ok": bool(payload.get("ok", True)),
                "snapshot_key": "trading_sitewide_refresh",
                "snapshot_count": int(refresh.get("snapshot_count") or 0),
                "snapshot_keys": refresh.get("snapshot_keys") or [],
                "generated_at": refresh.get("generated_at"),
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="trading_sitewide_refresh",
            title="Trading sitewide refresh",
            snapshot_key="trading_sitewide_refresh",
            request_payload={"reason": reason},
            worker=worker,
            summary_builder=summary,
            reuse_recent_success_seconds=15,
            queue_class="trading_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        audit(
            "TRADING_SITEWIDE_SNAPSHOTS_REFRESH_QUEUED",
            get_client_ip(),
            user=actor_value(actor, "username", "root"),
            success=True,
            ua=get_ua(),
            detail=json.dumps({"job_uuid": started["job"].get("job_uuid"), "reason": reason}, ensure_ascii=False),
        )
        return management_job_response(started, snapshot_key="trading_sitewide_refresh")

    @app.route("/api/root/trading/sitewide/refresh/latest", methods=["GET"])
    @require_csrf_safe
    def root_trading_sitewide_refresh_latest():
        actor, err = root_or_403()
        if err:
            return err
        try:
            conn = get_db()
            try:
                sql_started = time.perf_counter()
                snapshot = get_management_snapshot(conn, snapshot_key="trading_sitewide_refresh", include_payload=True)
                request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
            finally:
                conn.close()
            if not snapshot.get("ok"):
                return json_resp({"ok": False, "snapshot": snapshot, "client_should_enqueue": True}, 404)
            payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
            return json_resp({
                "ok": True,
                "refresh": payload.get("refresh") or {},
                "snapshot": {
                    "snapshot_key": snapshot.get("snapshot_key"),
                    "generated_at": snapshot.get("generated_at"),
                    "source_job_uuid": snapshot.get("source_job_uuid"),
                    "summary": snapshot.get("summary") or {},
                    "snapshot_backed": True,
                },
            })
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/report", methods=["GET"])
    @require_csrf_safe
    def admin_trading_report():
        actor, err = root_or_403()
        if err:
            return err
        return root_trading_snapshot_response("root_report")

    @app.route("/api/root/trading/sitewide/pools", methods=["GET"])
    @require_csrf_safe
    def root_trading_sitewide_pools():
        actor, err = root_or_403()
        if err:
            return err
        return root_trading_snapshot_response("sitewide_pools")

    @app.route("/api/root/trading/sitewide/user-positions", methods=["GET"])
    @require_csrf_safe
    def root_trading_sitewide_user_positions():
        actor, err = root_or_403()
        if err:
            return err
        return root_trading_snapshot_response("sitewide_user_positions")

    @app.route("/api/root/trading/settings", methods=["GET"])
    @require_csrf_safe
    def root_trading_settings():
        actor, err = root_or_403()
        if err:
            return err
        return json_resp({"ok": True, **trading_service.get_root_settings()})

    @app.route("/api/root/trading/price-fusion-status", methods=["GET"])
    @require_csrf_safe
    def root_trading_price_fusion_status():
        actor, err = root_or_403()
        if err:
            return err
        market_symbol = request.args.get("market_symbol") or ""
        try:
            status = trading_service.get_root_price_fusion_status(market_symbol=market_symbol)
            return json_resp({"ok": True, "status": status})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/background/status", methods=["GET"])
    @require_csrf_safe
    def root_trading_background_status():
        actor, err = root_or_403()
        if err:
            return err
        try:
            limit = int(request.args.get("limit") or 20)
        except Exception:
            limit = 20
        try:
            return json_resp(trading_service.get_background_status(limit=limit))
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/background/run-once", methods=["POST"])
    @require_csrf
    def root_trading_background_run_once():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        job_key = str(data.get("job_key") or "").strip()
        if not job_key:
            return json_resp({"ok": False, "msg": "缺少 job_key"}, 400)
        if str(data.get("confirm") or "") != "RUN_TRADING_JOB_ONCE":
            return json_resp({"ok": False, "msg": "confirm 必須為 RUN_TRADING_JOB_ONCE"}, 400)
        try:
            result = trading_service.enqueue_background_job_once(
                job_key=job_key,
                requested_by=actor,
                force=True,
            )
            result["kick_started"] = kick_trading_background_queue(reason="root_run_once")
            result.setdefault("msg", "已排入交易背景佇列，系統會在背景處理。")
            audit(
                "TRADING_BACKGROUND_JOB_RUN_ONCE_ENQUEUED",
                get_client_ip(),
                user=actor_value(actor, "username", "root"),
                success=bool(result.get("ok")),
                ua=get_ua(),
                detail=json.dumps({
                    "job_key": job_key,
                    "queue_uuid": result.get("queue_uuid"),
                    "status": result.get("status"),
                }, ensure_ascii=False),
            )
            return json_resp(result, 202)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/background/pause", methods=["POST"])
    @require_csrf
    def root_trading_background_pause():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        reason = str(data.get("reason") or "paused_by_root").strip()[:500]
        job_key = str(data.get("job_key") or "").strip()
        try:
            keys = [job_key] if job_key else [row["job_key"] for row in trading_service.get_background_status().get("jobs", [])]
            results = [
                trading_service.set_background_job_enabled(
                    job_key=key,
                    enabled=False,
                    reason=reason,
                    actor=actor,
                )
                for key in keys
            ]
            audit(
                "TRADING_BACKGROUND_PAUSED",
                get_client_ip(),
                user=actor_value(actor, "username", "root"),
                success=True,
                ua=get_ua(),
                detail=json.dumps({"job_keys": keys, "reason": reason}, ensure_ascii=False),
            )
            return json_resp({"ok": True, "paused": results})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/background/resume", methods=["POST"])
    @require_csrf
    def root_trading_background_resume():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        job_key = str(data.get("job_key") or "").strip()
        try:
            keys = [job_key] if job_key else [row["job_key"] for row in trading_service.get_background_status().get("jobs", [])]
            results = [
                trading_service.set_background_job_enabled(
                    job_key=key,
                    enabled=True,
                    reason="",
                    actor=actor,
                )
                for key in keys
            ]
            audit(
                "TRADING_BACKGROUND_RESUMED",
                get_client_ip(),
                user=actor_value(actor, "username", "root"),
                success=True,
                ua=get_ua(),
                detail=json.dumps({"job_keys": keys}, ensure_ascii=False),
            )
            return json_resp({"ok": True, "resumed": results})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/bot-audit/dashboard", methods=["GET"])
    @require_csrf_safe
    def root_trading_bot_audit_dashboard():
        actor, err = root_or_403()
        if err:
            return err
        try:
            limit = int(request.args.get("limit") or 100)
        except Exception:
            limit = 100
        try:
            return json_resp({"ok": True, "dashboard": trading_service.get_bot_audit_dashboard(limit=limit)})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/bot-audit/run", methods=["POST"])
    @require_csrf
    def root_trading_bot_audit_run():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.run_due_bot_audits(
                actor=actor,
                limit=data.get("limit") or 0,
                force=bool(data.get("force")),
            )
            audit(
                "TRADING_BOT_AUDIT_MANUAL_RUN",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"audited={len(result.get('audited') or [])}, skipped={len(result.get('skipped') or [])}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/bot-competition/award", methods=["POST"])
    @require_csrf
    def root_trading_bot_competition_award():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.award_bot_competition_week(
                actor=actor,
                week=str(data.get("week") or "").strip() or None,
            )
            audit(
                "TRADING_BOT_COMPETITION_AWARDED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"week={result.get('week')}, awarded={len(result.get('awarded') or [])}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/settings", methods=["POST"])
    @require_csrf
    def root_trading_settings_update():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        if settings.get("price_source") == "manual_root" and not trading_manual_price_allowed_in_current_mode():
            return json_resp({"ok": False, "msg": "手動價格只允許在 dev_ready / test / internal_test / superweak 開發測試模式使用"}), 400
        try:
            result = trading_service.update_root_settings(
                actor=actor,
                settings=settings,
                markets=data.get("markets") if isinstance(data.get("markets"), list) else [],
            )
            audit("TRADING_SETTINGS_UPDATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail="root billing settings")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/markets", methods=["GET"])
    @require_csrf_safe
    def admin_trading_markets():
        actor, err = root_or_403()
        if err:
            return err
        include_disabled = str(request.args.get("include_disabled") or "1").strip().lower() not in {"0", "false", "no"}
        try:
            payload = trading_service.list_market_registry(include_disabled=include_disabled)
            return json_resp({"ok": True, **payload})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/markets", methods=["POST"])
    @require_csrf
    def admin_trading_markets_create():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.create_market_registry(actor=actor, payload=data)
            audit("TRADING_MARKET_REGISTRY_CREATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"symbol={result['market']['symbol']}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/markets/<int:market_id>", methods=["PUT"])
    @require_csrf
    def admin_trading_markets_update(market_id):
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.update_market_registry(actor=actor, market_id=market_id, payload=data)
            audit("TRADING_MARKET_REGISTRY_UPDATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"market_id={market_id}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/markets/<int:market_id>/disable", methods=["DELETE"])
    @require_csrf
    def admin_trading_markets_disable(market_id):
        actor, err = root_or_403()
        if err:
            return err
        try:
            result = trading_service.disable_market_registry(actor=actor, market_id=market_id)
            audit("TRADING_MARKET_REGISTRY_DISABLED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"market_id={market_id}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/markets/<int:market_id>/probe", methods=["POST"])
    @require_csrf
    def admin_trading_markets_probe(market_id):
        actor, err = root_or_403()
        if err:
            return err
        try:
            result = trading_service.probe_market_registry(market_id=market_id)
            audit("TRADING_MARKET_REGISTRY_PROBED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"market_id={market_id}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/markets/<int:market_id>/providers", methods=["GET"])
    @require_csrf_safe
    def admin_trading_market_providers(market_id):
        actor, err = root_or_403()
        if err:
            return err
        try:
            result = trading_service.get_market_provider_registry(market_id=market_id)
            return json_resp({"ok": True, **result})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/markets/<int:market_id>/providers", methods=["POST"])
    @require_csrf
    def admin_trading_market_providers_create(market_id):
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.create_market_provider_mapping(actor=actor, market_id=market_id, payload=data)
            audit("TRADING_MARKET_PROVIDER_CREATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"market_id={market_id}")
            return json_resp({"ok": True, **result})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/markets/<int:market_id>/providers/<int:mapping_id>", methods=["PUT"])
    @require_csrf
    def admin_trading_market_providers_update(market_id, mapping_id):
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.update_market_provider_mapping(actor=actor, market_id=market_id, mapping_id=mapping_id, payload=data)
            audit("TRADING_MARKET_PROVIDER_UPDATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"market_id={market_id},mapping_id={mapping_id}")
            return json_resp({"ok": True, **result})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/trading/markets/<int:market_id>/providers/<int:mapping_id>", methods=["DELETE"])
    @require_csrf
    def admin_trading_market_providers_disable(market_id, mapping_id):
        actor, err = root_or_403()
        if err:
            return err
        try:
            result = trading_service.disable_market_provider_mapping(actor=actor, market_id=market_id, mapping_id=mapping_id)
            audit("TRADING_MARKET_PROVIDER_DISABLED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"market_id={market_id},mapping_id={mapping_id}")
            return json_resp({"ok": True, **result})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/btc-trade/check", methods=["POST"])
    @require_csrf
    def root_trading_btc_trade_check():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        settings = trading_service.get_root_settings().get("settings", {})
        project_dir = data.get("project_dir") or settings.get("btc_trade_project_dir") or str(default_btc_trade_project_dir(Path(__file__).resolve().parents[1]))
        try:
            status = btc_trade_status(project_dir)
            root = expand_server_path(project_dir)
            if root:
                status["project_dir"] = str(root)
            return json_resp({"ok": True, "status": status})
        except Exception as exc:
            return json_resp({"ok": False, "msg": f"BTC_trade 狀態檢查失敗：{exc.__class__.__name__}"}), 400

    @app.route("/api/root/trading/btc-trade/setup", methods=["POST"])
    @require_csrf
    def root_trading_btc_trade_setup():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        settings = trading_service.get_root_settings().get("settings", {})
        project_dir = data.get("project_dir") or settings.get("btc_trade_project_dir") or str(default_btc_trade_project_dir(Path(__file__).resolve().parents[1]))
        repo_url = data.get("repo_url") or settings.get("btc_trade_repo_url") or DEFAULT_BTC_TRADE_REPO_URL
        branch = data.get("branch") or settings.get("btc_trade_branch") or DEFAULT_BTC_TRADE_BRANCH
        result = btc_trade_setup(
            project_dir,
            repo_url=repo_url,
            branch=branch,
            base_dir=Path(__file__).resolve().parents[1],
        )
        audit(
            "BTC_TRADE_SETUP",
            get_client_ip(),
            user=actor["username"],
            success=bool(result.get("ok")),
            ua=get_ua(),
            detail=f"project_dir={result.get('project_dir')}; branch={branch}",
        )
        payload = {
            "ok": True,
            "setup_ok": bool(result.get("ok")),
            "project_dir": result.get("project_dir"),
            "message": result.get("message") or "",
            "result": result,
        }
        return json_resp(payload)

    @app.route("/api/root/trading/btc-trade/start", methods=["POST"])
    @require_csrf
    def root_trading_btc_trade_start():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        settings = trading_service.get_root_settings().get("settings", {})
        project_dir = data.get("project_dir") or settings.get("btc_trade_project_dir") or str(default_btc_trade_project_dir(Path(__file__).resolve().parents[1]))
        repo_url = data.get("repo_url") or settings.get("btc_trade_repo_url") or DEFAULT_BTC_TRADE_REPO_URL
        branch = data.get("branch") or settings.get("btc_trade_branch") or DEFAULT_BTC_TRADE_BRANCH
        timeframe = str(data.get("timeframe") or "4h").strip().lower() or "4h"
        wait_seconds = int(data.get("wait_seconds") or BTC_TRADE_START_WAIT_SECONDS)
        job_result = btc_trade_start_prediction_job(
            project_dir,
            timeframe=timeframe,
            wait_seconds=wait_seconds,
            repo_url=repo_url,
            branch=branch,
            base_dir=Path(__file__).resolve().parents[1],
            setup_if_needed=True,
        )
        audit(
            "BTC_TRADE_START",
            get_client_ip(),
            user=actor["username"],
            success=bool(job_result.get("ok")),
            ua=get_ua(),
            detail=f"project_dir={project_dir}; timeframe={timeframe}; started={job_result.get('started')}",
        )
        payload = {
            "ok": bool(job_result.get("ok")),
            "start_ok": bool(job_result.get("job_ok", job_result.get("ok"))),
            "started": bool(job_result.get("started")),
            "project_dir": (job_result.get("job") or {}).get("project_dir"),
            "job": job_result.get("job"),
        }
        job = payload.get("job") or {}
        if not payload["start_ok"]:
            payload["msg"] = job.get("message") or "BTC_trade 一鍵啟動失敗"
            payload["message"] = payload["msg"]
        elif job_result.get("started"):
            payload["message"] = "BTC_trade 一鍵啟動已在背景開始執行，會自動下載/更新、安裝依賴、訓練並產生預測"
        else:
            payload["message"] = "BTC_trade 一鍵啟動已在背景執行中，沿用同一個工作"
        return json_resp(payload)

    @app.route("/api/root/trading/btc-trade/start-status", methods=["GET"])
    @require_csrf_safe
    def root_trading_btc_trade_start_status():
        actor, err = root_or_403()
        if err:
            return err
        job_id = (request.args.get("job_id") or "").strip()
        job = btc_trade_start_prediction_job_status(job_id)
        if not job:
            return json_resp({"ok": False, "msg": "找不到 BTC_trade 一鍵啟動工作"}, 404)
        job_ok = job.get("status") != "error"
        return json_resp({"ok": job_ok, "job_ok": job_ok, "msg": "" if job_ok else job.get("message") or "BTC_trade 一鍵啟動失敗", "job": job})

    @app.route("/api/root/trading/liquidations/scan", methods=["POST"])
    @require_csrf
    def root_trading_liquidations_scan():
        actor, err = root_or_403()
        if err:
            return err
        data = {}
        if request.data:
            data, err = parse_json_body()
            if err:
                return err
        try:
            result = trading_service.scan_margin_liquidations(
                actor=actor,
                limit=data.get("limit", 100) if isinstance(data, dict) else 100,
            )
            audit(
                "TRADING_LIQUIDATION_SCAN",
                get_client_ip(),
                user=actor["username"],
                success=bool(result.get("ok")),
                ua=get_ua(),
                detail=(
                    f"scanned={result.get('scanned')}, "
                    f"candidates={len(result.get('candidates') or [])}, "
                    f"liquidated={len(result.get('liquidated') or [])}, "
                    f"errors={len(result.get('errors') or [])}"
                ),
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/orders/match", methods=["POST"])
    @require_csrf
    def root_trading_orders_match():
        actor, err = root_or_403()
        if err:
            return err
        data = {}
        if request.data:
            data, err = parse_json_body()
            if err:
                return err
        try:
            result = trading_service.match_open_limit_orders(
                actor=actor,
                market_symbol=data.get("market_symbol") if isinstance(data, dict) else None,
                limit=data.get("limit", 200) if isinstance(data, dict) else 200,
            )
            audit(
                "TRADING_LIMIT_ORDER_MATCH_SCAN",
                get_client_ip(),
                user=actor["username"],
                success=bool(result.get("ok")),
                ua=get_ua(),
                detail=(
                    f"scanned={result.get('scanned')}, "
                    f"matched={len(result.get('matched') or [])}, "
                    f"errors={len(result.get('errors') or [])}"
                ),
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/markets/<path:symbol>", methods=["POST"])
    @require_csrf
    def root_trading_market_update(symbol):
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        if data.get("manual_price_points") is not None and not trading_manual_price_allowed_in_current_mode():
            return json_resp({"ok": False, "msg": "手動價格只允許在 dev_ready / test / internal_test 開發測試模式使用"}), 400
        try:
            result = trading_service.update_market(
                actor=actor,
                symbol=symbol,
                manual_price_points=data.get("manual_price_points"),
                max_price_jump_percent=data.get("max_price_jump_percent"),
                fee_rate_percent=data.get("fee_rate_percent"),
                min_order_points=data.get("min_order_points"),
                max_order_points=data.get("max_order_points"),
                enabled=data.get("enabled") if "enabled" in data else None,
                confirm_jump=bool(data.get("confirm_jump")),
            )
            audit("TRADING_MARKET_UPDATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"symbol={symbol}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/reserve/allocate", methods=["POST"])
    @require_csrf
    def root_trading_reserve_allocate():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.allocate_reserve(
                actor=actor,
                source_user_id=data.get("source_user_id"),
                amount_points=data.get("amount_points"),
                reason=data.get("reason"),
            )
            audit(
                "TRADING_RESERVE_ALLOCATED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"source_user_id={data.get('source_user_id')}, amount={data.get('amount_points')}, reason=ROOT_RESERVE_ALLOCATION",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/simulated-balance/reset", methods=["POST"])
    @require_csrf
    def root_trading_simulated_balance_reset():
        actor, err = root_or_403()
        if err:
            return err
        try:
            result = trading_service.reset_root_simulated_balance(actor=actor)
            audit(
                "TRADING_ROOT_SIM_BALANCE_RESET",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=(
                    f"balance={result.get('funding', {}).get('available_points')}, "
                    f"deleted={result.get('deleted', {})}"
                ),
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/contracts", methods=["POST"])
    @require_csrf
    def root_trading_contract_open():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = trading_service.open_root_contract_position(
                actor=actor,
                market_symbol=data.get("market_symbol"),
                side=data.get("side"),
                quantity=data.get("quantity"),
                leverage=data.get("leverage"),
                margin_points=data.get("margin_points"),
            )
            audit(
                "TRADING_ROOT_CONTRACT_OPENED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"position_uuid={result.get('position', {}).get('position_uuid')}, market={data.get('market_symbol')}, side={data.get('side')}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/contracts/<position_uuid>/close", methods=["POST"])
    @require_csrf
    def root_trading_contract_close(position_uuid):
        actor, err = root_or_403()
        if err:
            return err
        try:
            result = trading_service.close_root_contract_position(actor=actor, position_uuid=position_uuid)
            audit(
                "TRADING_ROOT_CONTRACT_CLOSED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"position_uuid={position_uuid}, pnl={result.get('pnl_points')}",
            )
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    def start_root_trading_verify_job(actor):
        actor_snapshot = management_actor(actor)

        def worker(progress):
            progress(stage="trading_verify", progress_percent=20, detail="verifying trading state off request path")
            verification = trading_service.verify_state()
            progress(stage="snapshot", progress_percent=90, detail="recording trading verification snapshot")
            return {"ok": bool(verification.get("ok", True)), "verification": verification}

        def summary(payload):
            verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
            errors = verification.get("errors") or []
            return {
                "ok": bool(payload.get("ok", True)),
                "snapshot_key": "trading_verify",
                "verification_ok": bool(verification.get("ok", True)),
                "error_count": len(errors) if isinstance(errors, list) else 0,
                "generated_at": payload.get("generated_at"),
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="trading_verify",
            title="Trading verify",
            snapshot_key="trading_verify",
            request_payload={},
            worker=worker,
            summary_builder=summary,
            reuse_recent_success_seconds=10,
            queue_class="trading_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        return started

    def load_root_trading_verify_snapshot():
        conn = get_db()
        try:
            sql_started = time.perf_counter()
            snapshot = get_management_snapshot(conn, snapshot_key="trading_verify", include_payload=True)
            request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        finally:
            conn.close()
        return snapshot

    def root_trading_verify_snapshot_response(*, missing_status=404):
        snapshot = load_root_trading_verify_snapshot()
        if not snapshot.get("ok"):
            return json_resp({
                "ok": False,
                "snapshot": snapshot,
                "client_should_enqueue": True,
            }, missing_status)
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
        return json_resp({
            "ok": bool(payload.get("ok", True)),
            "verification": payload.get("verification") or {},
            "snapshot": {
                "snapshot_key": snapshot.get("snapshot_key"),
                "generated_at": snapshot.get("generated_at"),
                "updated_at": snapshot.get("updated_at"),
                "source_job_uuid": snapshot.get("source_job_uuid"),
                "summary": snapshot.get("summary") or {},
                "snapshot_backed": True,
            },
        })

    @app.route("/api/root/trading/verify", methods=["GET"])
    @require_csrf_safe
    def root_trading_verify():
        actor, err = root_or_403()
        if err:
            return err
        refresh = str(request.args.get("refresh") or request.args.get("start_job") or "").strip().lower() in {"1", "true", "yes", "on"}
        if not refresh:
            snapshot = load_root_trading_verify_snapshot()
            if snapshot.get("ok"):
                payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
                return json_resp({
                    "ok": bool(payload.get("ok", True)),
                    "verification": payload.get("verification") or {},
                    "snapshot": {
                        "snapshot_key": snapshot.get("snapshot_key"),
                        "generated_at": snapshot.get("generated_at"),
                        "updated_at": snapshot.get("updated_at"),
                        "source_job_uuid": snapshot.get("source_job_uuid"),
                        "summary": snapshot.get("summary") or {},
                        "snapshot_backed": True,
                    },
                })
        try:
            started = start_root_trading_verify_job(actor)
            return management_job_response(started, snapshot_key="trading_verify")
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/verify/jobs", methods=["POST"])
    @require_csrf
    def root_trading_verify_start_job():
        actor, err = root_or_403()
        if err:
            return err
        try:
            started = start_root_trading_verify_job(actor)
            return management_job_response(started, snapshot_key="trading_verify")
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/trading/verify/latest", methods=["GET"])
    @require_csrf_safe
    def root_trading_verify_latest():
        actor, err = root_or_403()
        if err:
            return err
        return root_trading_verify_snapshot_response()
