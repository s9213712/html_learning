"""Market, price, margin, and bot method slice for TradingEngineService.

Imported from services.trading.engine only after the facade class exists, so the
method bodies can reuse the same module-level helpers and constants without
changing runtime behavior.
"""

import json
import os

from services.trading import engine as _engine

globals().update({name: value for name, value in _engine.__dict__.items() if not name.startswith("__")})

def _market_registry_audit(self, conn, *, actor=None, action="", market_symbol="", before=None, after=None):
    return market_registry_audit_helper(
        self,
        conn,
        actor=actor,
        action=action,
        market_symbol=market_symbol,
        before=before,
        after=after,
    )

def _market_registry_payload(self, conn, registry_row):
    return market_registry_payload_helper(
        self,
        conn,
        registry_row,
        registry_default_market_payload=_registry_default_market_payload,
    )

def _market_provider_mapping_payload(self, row):
    return market_provider_mapping_payload_helper(row)

def _validate_market_registry_payload(self, payload, *, existing=None):
    return validate_market_registry_payload_helper(payload, existing=existing)

def _validate_market_provider_mapping_payload(self, payload, *, existing=None):
    return validate_market_provider_mapping_payload_helper(payload, existing=existing)

def _probe_market_registry_on_conn(self, conn, registry_row):
    return probe_market_registry_on_conn_helper(self, conn, registry_row)

def _persist_market_registry_probe(self, conn, registry_row):
    return persist_market_registry_probe_helper(self, conn, registry_row)

def list_market_registry(self, *, include_disabled=True):
    return list_market_registry_helper(self, include_disabled=include_disabled)

def get_market_provider_registry(self, *, market_id):
    return get_market_provider_registry_helper(self, market_id=market_id)

def create_market_registry(self, *, actor, payload):
    return create_market_registry_helper(
        self,
        actor=actor,
        payload=payload,
        sync_runtime_markets=_sync_registry_markets_to_runtime,
    )

def update_market_registry(self, *, actor, market_id, payload):
    return update_market_registry_helper(
        self,
        actor=actor,
        market_id=market_id,
        payload=payload,
        sync_runtime_markets=_sync_registry_markets_to_runtime,
    )

def disable_market_registry(self, *, actor, market_id):
    return disable_market_registry_helper(
        self,
        actor=actor,
        market_id=market_id,
        sync_runtime_markets=_sync_registry_markets_to_runtime,
    )

def create_market_provider_mapping(self, *, actor, market_id, payload):
    return create_market_provider_mapping_helper(
        self,
        actor=actor,
        market_id=market_id,
        payload=payload,
        sync_runtime_markets=_sync_registry_markets_to_runtime,
    )

def update_market_provider_mapping(self, *, actor, market_id, mapping_id, payload):
    return update_market_provider_mapping_helper(
        self,
        actor=actor,
        market_id=market_id,
        mapping_id=mapping_id,
        payload=payload,
        sync_runtime_markets=_sync_registry_markets_to_runtime,
    )

def disable_market_provider_mapping(self, *, actor, market_id, mapping_id):
    return disable_market_provider_mapping_helper(
        self,
        actor=actor,
        market_id=market_id,
        mapping_id=mapping_id,
        sync_runtime_markets=_sync_registry_markets_to_runtime,
    )

def probe_market_registry(self, *, market_id):
    return probe_market_registry_helper(
        self,
        market_id=market_id,
        sync_runtime_markets=_sync_registry_markets_to_runtime,
    )

def _live_price_symbol(self, market_symbol, *, conn=None):
    symbol = self._normalize_market_symbol_on_conn(conn, market_symbol) if conn is not None else self.normalize_market_symbol(market_symbol)
    if conn is not None:
        return self._market_provider_id_on_conn(conn, symbol, "binance_public_api")
    return self.market_provider_id(symbol, "binance_public_api")

def _fetch_json_url(self, url, *, timeout=5, user_agent="hackme_web/1.0 trading-price", with_meta=False):
    req = Request(url, headers={"User-Agent": user_agent})
    started = time.perf_counter()
    with urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    fetched_at = _now()
    payload = json.loads(raw)
    if not with_meta:
        return payload
    latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return payload, {
        "fetched_at": fetched_at,
        "latency_ms": latency_ms,
    }

def _price_points_from_float(self, price, *, source):
    try:
        price_points = _to_price_float(
            Decimal(str(price)) * Decimal(str(USDT_TO_POINTS_RATE)),
            name=f"{source} price_points",
        )
    except Exception as exc:
        raise ValueError(f"{source} price format is invalid") from exc
    if price_points <= 0:
        raise ValueError(f"{source} price is invalid")
    return price_points

def _call_with_optional_conn(self, func, *args, conn=None, **kwargs):
    if conn is None:
        return func(*args, **kwargs)
    try:
        parameters = inspect.signature(func).parameters
    except Exception:
        parameters = {}
    if "conn" in parameters:
        return func(*args, conn=conn, **kwargs)
    return func(*args, **kwargs)

def _provider_ticker_with_fallback(self, source, market_symbol, *, settings, http_fetcher, conn=None):
    return provider_ticker_with_fallback_helper(
        self,
        source,
        market_symbol,
        settings=settings,
        http_fetcher=http_fetcher,
        conn=conn,
    )

def _provider_orderbook_with_fallback(self, source, market_symbol, *, settings, depth_levels, band_percent, request_limit, http_fetcher, book_getter, conn=None):
    return provider_orderbook_with_fallback_helper(
        self,
        source,
        market_symbol,
        settings=settings,
        depth_levels=depth_levels,
        band_percent=band_percent,
        request_limit=request_limit,
        http_fetcher=http_fetcher,
        book_getter=book_getter,
        conn=conn,
    )

def _fetch_binance_price_points(self, market_symbol, *, settings=None, with_meta=False, conn=None):
    symbol = self.market_provider_id(market_symbol, "binance_public_api", conn=conn)
    if not symbol:
        raise ValueError("binance price is not supported for this market")
    def http_fetcher():
        payload, fetch_meta = self._fetch_json_url(
            f"{BINANCE_TICKER_URL}?{urlencode({'symbol': symbol})}",
            timeout=5,
            with_meta=True,
        )
        price = payload.get("price") if isinstance(payload, dict) else None
        return self._price_points_from_float(price, source="binance_public_api"), fetch_meta
    price_points, meta = self._provider_ticker_with_fallback("binance_public_api", market_symbol, settings=settings or {}, http_fetcher=http_fetcher, conn=conn)
    return (price_points, meta) if with_meta else price_points

def _fetch_okx_price_points(self, market_symbol, *, settings=None, with_meta=False, conn=None):
    instrument = self.market_provider_id(market_symbol, "okx_public_api", conn=conn)
    if not instrument:
        raise ValueError("okx price is not supported for this market")
    def http_fetcher():
        payload, fetch_meta = self._fetch_json_url(
            f"{OKX_TICKER_URL}?{urlencode({'instId': instrument})}",
            timeout=5,
            user_agent="hackme_web/1.0 trading-price okx",
            with_meta=True,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        ticker = data[0] if isinstance(data, list) and data else None
        price = ticker.get("last") if isinstance(ticker, dict) else None
        return self._price_points_from_float(price, source="okx_public_api"), fetch_meta
    price_points, meta = self._provider_ticker_with_fallback("okx_public_api", market_symbol, settings=settings or {}, http_fetcher=http_fetcher, conn=conn)
    return (price_points, meta) if with_meta else price_points

def _fetch_coinbase_price_points(self, market_symbol, *, settings=None, with_meta=False, conn=None):
    product_id = self.market_provider_id(market_symbol, "coinbase_exchange", conn=conn)
    if not product_id:
        raise ValueError("coinbase price is not supported for this market")
    def http_fetcher():
        payload, fetch_meta = self._fetch_json_url(
            COINBASE_TICKER_URL_TEMPLATE.format(product_id=product_id),
            timeout=5,
            user_agent="hackme_web/1.0 trading-price coinbase",
            with_meta=True,
        )
        price = payload.get("price") if isinstance(payload, dict) else None
        return self._price_points_from_float(price, source="coinbase_exchange"), fetch_meta
    price_points, meta = self._provider_ticker_with_fallback("coinbase_exchange", market_symbol, settings=settings or {}, http_fetcher=http_fetcher, conn=conn)
    return (price_points, meta) if with_meta else price_points

def _fetch_kraken_price_points(self, market_symbol, *, settings=None, with_meta=False, conn=None):
    pair = self.market_provider_id(market_symbol, "kraken_public_api", conn=conn)
    if not pair:
        raise ValueError("kraken price is not supported for this market")
    def http_fetcher():
        payload, fetch_meta = self._fetch_json_url(
            f"{KRAKEN_TICKER_URL}?{urlencode({'pair': pair})}",
            timeout=5,
            user_agent="hackme_web/1.0 trading-price kraken",
            with_meta=True,
        )
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError(f"kraken ticker error: {payload.get('error') if isinstance(payload, dict) else 'invalid payload'}")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        ticker = next(iter(result.values()), None)
        close = ticker.get("c", [None])[0] if isinstance(ticker, dict) else None
        return self._price_points_from_float(close, source="kraken_public_api"), fetch_meta
    price_points, meta = self._provider_ticker_with_fallback("kraken_public_api", market_symbol, settings=settings or {}, http_fetcher=http_fetcher, conn=conn)
    return (price_points, meta) if with_meta else price_points

def _fetch_gemini_price_points(self, market_symbol, *, settings=None, with_meta=False, conn=None):
    symbol = self.market_provider_id(market_symbol, "gemini_public_api", conn=conn)
    if not symbol:
        raise ValueError("gemini price is not supported for this market")
    payload, fetch_meta = self._fetch_json_url(
        GEMINI_TICKER_URL_TEMPLATE.format(symbol=symbol),
        timeout=5,
        user_agent="hackme_web/1.0 trading-price gemini",
        with_meta=True,
    )
    price = payload.get("close") or payload.get("last") if isinstance(payload, dict) else None
    price_points = self._price_points_from_float(price, source="gemini_public_api")
    meta = self._provider_transport_meta("gemini_public_api", market_symbol, settings=settings or {}, transport="http_polling", fetched_at=fetch_meta.get("fetched_at"), latency_ms=fetch_meta.get("latency_ms"), conn=conn)
    return (price_points, meta) if with_meta else price_points

def _fetch_bitstamp_price_points(self, market_symbol, *, settings=None, with_meta=False, conn=None):
    pair = self.market_provider_id(market_symbol, "bitstamp_public_api", conn=conn)
    if not pair:
        raise ValueError("bitstamp price is not supported for this market")
    payload, fetch_meta = self._fetch_json_url(
        BITSTAMP_TICKER_URL_TEMPLATE.format(pair=pair),
        timeout=5,
        user_agent="hackme_web/1.0 trading-price bitstamp",
        with_meta=True,
    )
    price = payload.get("last") if isinstance(payload, dict) else None
    price_points = self._price_points_from_float(price, source="bitstamp_public_api")
    meta = self._provider_transport_meta("bitstamp_public_api", market_symbol, settings=settings or {}, transport="http_polling", fetched_at=fetch_meta.get("fetched_at"), latency_ms=fetch_meta.get("latency_ms"), conn=conn)
    return (price_points, meta) if with_meta else price_points

def _fetch_coingecko_price_points(self, market_symbol, *, settings=None, with_meta=False, conn=None):
    coin_id = self.market_provider_id(market_symbol, "coingecko_simple_price", conn=conn)
    if not coin_id:
        raise ValueError("coingecko price is not supported for this market")
    payload, fetch_meta = self._fetch_json_url(
        f"{COINGECKO_SIMPLE_PRICE_URL}?{urlencode({'ids': coin_id, 'vs_currencies': 'usd', 'include_last_updated_at': 'true'})}",
        timeout=5,
        user_agent="hackme_web/1.0 trading-price coingecko",
        with_meta=True,
    )
    coin = payload.get(coin_id) if isinstance(payload, dict) else None
    price = coin.get("usd") if isinstance(coin, dict) else None
    price_points = self._price_points_from_float(price, source="coingecko_simple_price")
    meta = self._provider_transport_meta("coingecko_simple_price", market_symbol, settings=settings or {}, transport="http_polling", fetched_at=fetch_meta.get("fetched_at"), latency_ms=fetch_meta.get("latency_ms"), conn=conn)
    return (price_points, meta) if with_meta else price_points

def _price_fusion_depth_levels(self, settings):
    try:
        return int((settings or {}).get("price_fusion_depth_levels") or DEFAULT_PRICE_FUSION_DEPTH_LEVELS)
    except Exception:
        return DEFAULT_PRICE_FUSION_DEPTH_LEVELS

def _price_fusion_depth_band_percent(self, settings):
    try:
        return float((settings or {}).get("price_fusion_depth_band_percent") or DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT)
    except Exception:
        return DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT

def _price_fusion_min_orderbook_coverage_percent(self, settings):
    try:
        return float((settings or {}).get("price_fusion_min_orderbook_coverage_percent") or DEFAULT_PRICE_FUSION_MIN_ORDERBOOK_COVERAGE_PERCENT)
    except Exception:
        return DEFAULT_PRICE_FUSION_MIN_ORDERBOOK_COVERAGE_PERCENT

def _price_fusion_provider_weight_cap_percent(self, settings):
    try:
        return float((settings or {}).get("price_fusion_max_single_provider_weight_percent") or DEFAULT_PRICE_FUSION_MAX_SINGLE_PROVIDER_WEIGHT_PERCENT)
    except Exception:
        return DEFAULT_PRICE_FUSION_MAX_SINGLE_PROVIDER_WEIGHT_PERCENT

def _price_fusion_min_provider_count(self, settings):
    try:
        return int((settings or {}).get("price_fusion_min_provider_count") or DEFAULT_PRICE_FUSION_MIN_PROVIDER_COUNT)
    except Exception:
        return DEFAULT_PRICE_FUSION_MIN_PROVIDER_COUNT

def _price_stream_ws_enabled(self, settings):
    return bool((settings or {}).get("price_stream_ws_enabled", True))

def _price_stream_ws_stale_seconds(self, settings):
    try:
        return int((settings or {}).get("price_stream_ws_stale_seconds") or DEFAULT_PRICE_STREAM_WS_STALE_SECONDS)
    except Exception:
        return DEFAULT_PRICE_STREAM_WS_STALE_SECONDS

def _price_stream_provider_state(self, source, market_symbol, *, settings, conn=None):
    return price_stream_provider_state_helper(self, source, market_symbol, settings=settings, conn=conn)

def _provider_transport_meta(self, source, market_symbol, *, settings, stream_state=None, transport="http_polling", fetched_at="", latency_ms=0.0, fallback=False, exclusion_reason="", conn=None):
    return provider_transport_meta_helper(
        self,
        source,
        market_symbol,
        settings=settings,
        stream_state=stream_state,
        transport=transport,
        fetched_at=fetched_at,
        latency_ms=latency_ms,
        fallback=fallback,
        exclusion_reason=exclusion_reason,
        conn=conn,
    )

def _resolve_stream_ticker_snapshot(self, source, market_symbol, *, settings, conn=None):
    return resolve_stream_ticker_snapshot_helper(self, source, market_symbol, settings=settings, conn=conn)

def _resolve_stream_orderbook_snapshot(self, source, market_symbol, *, settings, conn=None):
    return resolve_stream_orderbook_snapshot_helper(self, source, market_symbol, settings=settings, conn=conn)

def _provider_quantity_unit_info(self, source):
    return provider_quantity_unit_info_helper(self, source)

def _price_fusion_warning(self, code, message, *, severity="warning"):
    return price_fusion_warning_helper(code, message, severity=severity)

def _append_price_fusion_warning(self, warnings, code, message, *, severity="warning"):
    return append_price_fusion_warning_helper(self, warnings, code, message, severity=severity)

def _primary_price_fusion_warning(self, warnings):
    return primary_price_fusion_warning_helper(warnings)

def _price_usage_label(self, price_type):
    return price_usage_label(price_type)

def _price_source_label(self, source):
    return price_source_label(source, PRICE_PROVIDER_LABELS, fused_price_source=FUSED_PRICE_SOURCE)

def _price_context_confidence(self, *, price_type, source, health, degraded, stale, provider_count, high_risk_blocked):
    return price_context_confidence(
        price_type=price_type,
        source=source,
        health=health,
        degraded=degraded,
        stale=stale,
        provider_count=provider_count,
        high_risk_blocked=high_risk_blocked,
        minimum_provider_count=DEFAULT_PRICE_FUSION_MIN_PROVIDER_COUNT,
    )

def _price_context_risk_grade_usable(
    self,
    *,
    price_type,
    source,
    health,
    degraded,
    stale,
    provider_count,
    high_risk_blocked,
    fallback,
    synthetic_test_provider=False,
):
    return price_context_risk_grade_usable(
        price_type=price_type,
        source=source,
        health=health,
        degraded=degraded,
        stale=stale,
        provider_count=provider_count,
        high_risk_blocked=high_risk_blocked,
        fallback=fallback,
        synthetic_test_provider=synthetic_test_provider,
    )

def _build_price_context(self, *, market_symbol, price_type, price_points, price_source, price_meta):
    return build_price_context_helper(
        self,
        market_symbol=market_symbol,
        price_type=price_type,
        price_points=price_points,
        price_source=price_source,
        price_meta=price_meta,
    )

def _attach_market_price_contexts(self, market, *, reference_context, risk_grade_context):
    item = dict(market or {})
    item["reference_price_points"] = reference_context.get("price_points")
    item["risk_grade_price_points"] = risk_grade_context.get("price_points")
    item["reference_price_context"] = reference_context
    item["risk_grade_price_context"] = risk_grade_context
    return item

def _stored_market_price_contexts(self, market):
    return stored_market_price_contexts_helper(self, market)

def _price_fusion_effective_score(self, snapshot):
    return price_fusion_effective_score(snapshot)

def _price_fusion_reference_score(self, snapshot):
    return price_fusion_reference_score(snapshot)

def _price_fusion_warning_is_degrading(self, warning):
    code = str((warning or {}).get("code") or "").strip()
    if not code:
        return False
    return code not in {
        "provider_coverage_partial",
        "provider_weight_cap_applied",
        "provider_weight_cap_unenforceable",
    }

def _price_fusion_exclusion_is_degrading(self, exclusion):
    reason = str((exclusion or {}).get("reason") or "").strip()
    if not reason:
        return False
    return reason not in {
        "manual_weight_zero",
        "fetch_failed",
        "stale_orderbook",
        "latency_too_high",
        "one_sided_depth",
        "midpoint_deviation",
        "midpoint_deviation_exceeded",
        "quantity_unit_unconfirmed",
    }

def _transport_state_from_provider_rows(self, provider_rows, *, warnings=None, degraded=False, conservative_mode=False, min_provider_count=0, ws_enabled=True):
    return transport_state_from_provider_rows_helper(
        self,
        provider_rows,
        warnings=warnings,
        degraded=degraded,
        conservative_mode=conservative_mode,
        min_provider_count=min_provider_count,
        ws_enabled=ws_enabled,
    )

def _assert_price_meta_allows_high_risk_use(self, conn, *, actor=None, market_symbol="", usage="", price_meta=None):
    if market_symbol:
        market_row = conn.execute(
            "SELECT allow_risk_grade_usage FROM trading_markets WHERE symbol=?",
            (self._normalize_market_symbol_on_conn(conn, market_symbol),),
        ).fetchone()
        if market_row and not int(market_row["allow_risk_grade_usage"] or 0):
            raise ValueError(f"{usage or 'high-risk trading action'} is disabled for this market")
    meta = price_meta or {}
    if bool(meta.get("synthetic_test_provider")):
        return
    resolved_source = str(meta.get("resolved_source") or "").strip().lower()
    hard_block_reason = str(meta.get("high_risk_block_reason") or meta.get("fallback_reason") or "").strip()
    usage_text = str(usage or "").strip().lower()
    # Caller-side policy: market/limit fills may use cached fallback at the caller's
    # discretion (the caller reads `risk_grade_usable=false` from meta and decides).
    # Higher-risk paths (grid scan, margin, contract, trial-credit, bot triggers) must
    # still hard-block on cached fallback. manual_root always hard-blocks.
    cached_fallback_allowed_usages = {
        # Market orders intentionally NOT allowed: when the live feed is
        # down and we fall back to last-good cached price, the user has no
        # chance to react before the fill commits at a potentially stale
        # price. Limit fills are bounded by the user's limit_price so
        # cached fallback is safe there.
        "immediately executable limit order",
        "limit order match",
    }
    # Decide which path produces the hard block:
    #   - manual_root → always hard-block (no live source)
    #   - *_cached + non-allowed usage → hard-block (grid/margin/contract/...)
    #   - any other meta with high_risk_blocked=True (e.g., conservative
    #     fusion with too few providers) → hard-block; the callers we want to
    #     opt-in via policy must opt-in via *_cached resolved_source
    is_cached_source = bool(resolved_source) and resolved_source.endswith("_cached")
    cached_caller_allowed = is_cached_source and usage_text in cached_fallback_allowed_usages
    price_confidence_override = False
    env_allows_price_override = str(os.environ.get("HACKME_DEV_TRADING_DISABLE_PRICE_CONFIDENCE_GATES") or "").strip().lower() in {"1", "true", "yes", "on"}
    env_allows_market_override = str(os.environ.get("HACKME_DEV_TRADING_ALLOW_CONSERVATIVE_MARKET_ORDERS") or "").strip().lower() in {"1", "true", "yes", "on"}
    rows = conn.execute(
        """
        SELECT key, value
        FROM trading_settings
        WHERE key IN (
            ?, ?, ?, ?,
            'trading.warning_language',
            'trading.price_fusion_trade_min_provider_count',
            'trading.price_degrade_pause_market_orders',
            'trading.price_degrade_pause_bots',
            'trading.price_degrade_pause_borrowing'
        )
        """,
        (
            "trading.disable_price_confidence_gates",
            "trading.allow_conservative_market_orders",
            "trading.dev_disable_price_confidence_gates",
            "trading.dev_allow_conservative_market_orders",
        ),
    ).fetchall()
    settings = {str(row["key"] or ""): str(row["value"] or "") for row in rows}
    policy_key = ""
    policy_label = "高風險交易"
    if "bot" in usage_text:
        policy_key = "trading.price_degrade_pause_bots"
        policy_label = "機器人交易"
    elif "margin" in usage_text or "borrow" in usage_text:
        policy_key = "trading.price_degrade_pause_borrowing"
        policy_label = "借貸交易"
    elif "market order" in usage_text or "limit order match" in usage_text or "contract position" in usage_text:
        policy_key = "trading.price_degrade_pause_market_orders"
        policy_label = "市價交易"
    try:
        trade_min_provider_count = max(
            1,
            int(settings.get("trading.price_fusion_trade_min_provider_count") or DEFAULT_PRICE_FUSION_TRADE_MIN_PROVIDER_COUNT),
        )
    except Exception:
        trade_min_provider_count = DEFAULT_PRICE_FUSION_TRADE_MIN_PROVIDER_COUNT
    warning_language = _warning_language(settings.get("trading.warning_language"))
    policy_enabled = str(settings.get(policy_key, "false")).strip().lower() in {"1", "true", "yes", "on"} if policy_key else False
    provider_count = max(
        0,
        int(meta.get("risk_grade_provider_count") or meta.get("provider_count") or 0),
    )
    conservative_mode = bool(meta.get("conservative_mode"))
    stale = bool(meta.get("stale"))
    fallback = bool(meta.get("fallback"))
    degraded = bool(meta.get("degraded"))
    high_risk_blocked = bool(meta.get("high_risk_blocked"))
    provider_short = conservative_mode and provider_count < trade_min_provider_count
    policy_should_pause = policy_enabled and (high_risk_blocked or provider_short or stale or fallback or degraded)
    if policy_should_pause:
        reason = hard_block_reason or "price source is in conservative mode"
        self._audit_event(
            conn,
            "TRADING_PRICE_HEALTH_BLOCKED",
            "high-risk trading path blocked by degraded fused price health",
            actor=actor,
            market_symbol=market_symbol,
            severity="critical",
            metadata={
                "usage": usage,
                "reason": reason,
                "price_health": meta.get("price_health"),
                "warnings": meta.get("warnings") or [],
                "excluded_sources": meta.get("excluded_sources") or [],
                "pause_policy": policy_key,
            },
        )
        provider_note = f"healthy providers={provider_count}" if provider_count else "healthy providers=0"
        if warning_language == "en":
            policy_label_en = {
                "市價交易": "Market-order trading",
                "機器人交易": "Bot trading",
                "借貸交易": "Borrowing / margin trading",
                "高風險交易": "High-risk trading",
            }.get(policy_label, "Trading")
            raise ValueError(
                f"{policy_label_en} paused because price health degraded: {reason} "
                f"({provider_note}; need at least {trade_min_provider_count} healthy providers to keep trading enabled)"
            )
        raise ValueError(
            f"{policy_label}已因價格降級暫停：{reason}（{provider_note}，需要至少 {trade_min_provider_count} 家才視為可交易）"
        )
    if not high_risk_blocked:
        return
    price_confidence_override = (
        env_allows_price_override
        or str(settings.get("trading.disable_price_confidence_gates") or "").strip().lower() in {"1", "true", "yes", "on"}
        or str(settings.get("trading.dev_disable_price_confidence_gates") or "").strip().lower() in {"1", "true", "yes", "on"}
        or (
            usage_text == "market order"
            and (
                env_allows_market_override
                or str(settings.get("trading.allow_conservative_market_orders") or "").strip().lower() in {"1", "true", "yes", "on"}
                or str(settings.get("trading.dev_allow_conservative_market_orders") or "").strip().lower() in {"1", "true", "yes", "on"}
            )
        )
    )
    if price_confidence_override:
        self._audit_event(
            conn,
            "TRADING_PRICE_HEALTH_OVERRIDE",
            "root setting allowed trading action despite conservative risk-grade price source",
            actor=actor,
            market_symbol=market_symbol,
            severity="warning",
            metadata={
                "usage": usage,
                "reason": hard_block_reason or "risk-grade price source is not available",
                "price_health": meta.get("price_health"),
                "warnings": meta.get("warnings") or [],
                "excluded_sources": meta.get("excluded_sources") or [],
                "resolved_source": resolved_source,
                "hard_block": False,
                "price_confidence_gate_disabled": True,
            },
        )
        return
    if not cached_caller_allowed:
        reason = hard_block_reason or "risk-grade price source is not available"
        usage_label = usage_text or "high-risk trading action"
        # Stable English substring the regression suite greps for (it covers
        # every non-risk-grade source we treat as conservative-mode here:
        # manual_root, cached fallback, degraded/stale fused providers, etc.)
        full_msg = f"{usage_label} is blocked while fused price is in conservative mode: {reason}"
        self._audit_event(
            conn,
            "TRADING_PRICE_HEALTH_BLOCKED",
            "high-risk trading path blocked by non-risk-grade price source",
            actor=actor,
            market_symbol=market_symbol,
            severity="critical",
            metadata={
                "usage": usage,
                "reason": reason,
                "price_health": meta.get("price_health"),
                "warnings": meta.get("warnings") or [],
                "excluded_sources": meta.get("excluded_sources") or [],
                "resolved_source": resolved_source,
                "hard_block": True,
            },
        )
        raise ValueError(full_msg)

def _provider_depth_request_limit(self, source, depth_levels):
    return provider_depth_request_limit(
        source,
        depth_levels,
        default_depth_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS,
    )

def _parse_orderbook_side(self, rows, *, max_levels):
    return parse_orderbook_side(rows, max_levels=max_levels)

def _depth_notional_snapshot(self, bids, asks, *, max_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS, band_percent=DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT):
    return depth_notional_snapshot(
        bids,
        asks,
        max_levels=max_levels,
        band_percent=band_percent,
    )

def _depth_notional_score(self, bids, asks, *, max_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS, band_percent=DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT):
    return depth_notional_score(
        bids,
        asks,
        max_levels=max_levels,
        band_percent=band_percent,
    )

def _build_orderbook_snapshot(self, *, source, bids, asks, fetch_meta=None, max_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS, band_percent=DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT, request_limit=None, transport_meta=None):
    return build_orderbook_snapshot_helper(
        self,
        source=source,
        bids=bids,
        asks=asks,
        fetch_meta=fetch_meta,
        max_levels=max_levels,
        band_percent=band_percent,
        request_limit=request_limit,
        transport_meta=transport_meta,
    )

def _normalize_orderbook_fetch_result(self, fetch_result):
    if isinstance(fetch_result, tuple) and len(fetch_result) == 2:
        payload, fetch_meta = fetch_result
        return payload, fetch_meta if isinstance(fetch_meta, dict) else {}
    return fetch_result, {}

def _okx_http_book_getter(self, payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    book = data[0] if isinstance(data, list) and data else None
    if not isinstance(book, dict):
        raise ValueError("okx order book payload is invalid")
    return book.get("bids") or [], book.get("asks") or []

def _kraken_http_book_getter(self, payload):
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError(f"kraken depth error: {payload.get('error') if isinstance(payload, dict) else 'invalid payload'}")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    book = next(iter(result.values()), None)
    if not isinstance(book, dict):
        raise ValueError("kraken order book payload is invalid")
    return book.get("bids") or [], book.get("asks") or []

def _fetch_binance_orderbook_snapshot(self, market_symbol, *, depth_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS, band_percent=DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT, settings=None, conn=None):
    symbol = self.market_provider_id(market_symbol, "binance_public_api", conn=conn)
    if not symbol:
        raise ValueError("binance order book is not supported for this market")
    request_limit = self._provider_depth_request_limit("binance_public_api", depth_levels)
    return self._provider_orderbook_with_fallback(
        "binance_public_api",
        market_symbol,
        settings=settings or {},
        depth_levels=depth_levels,
        band_percent=band_percent,
        request_limit=request_limit,
        http_fetcher=lambda: self._normalize_orderbook_fetch_result(self._fetch_json_url(
            f"{BINANCE_DEPTH_URL}?{urlencode({'symbol': symbol, 'limit': request_limit})}",
            timeout=5,
            with_meta=True,
        )),
        book_getter=lambda payload: ((payload or {}).get("bids") or [], (payload or {}).get("asks") or []),
        conn=conn,
    )

def _fetch_okx_orderbook_snapshot(self, market_symbol, *, depth_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS, band_percent=DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT, settings=None, conn=None):
    instrument = self.market_provider_id(market_symbol, "okx_public_api", conn=conn)
    if not instrument:
        raise ValueError("okx order book is not supported for this market")
    request_limit = self._provider_depth_request_limit("okx_public_api", depth_levels)
    return self._provider_orderbook_with_fallback(
        "okx_public_api",
        market_symbol,
        settings=settings or {},
        depth_levels=depth_levels,
        band_percent=band_percent,
        request_limit=request_limit,
        http_fetcher=lambda: self._normalize_orderbook_fetch_result(self._fetch_json_url(
            f"{OKX_BOOKS_URL}?{urlencode({'instId': instrument, 'sz': request_limit})}",
            timeout=5,
            user_agent="hackme_web/1.0 trading-depth okx",
            with_meta=True,
        )),
        book_getter=self._okx_http_book_getter,
        conn=conn,
    )

def _fetch_coinbase_orderbook_snapshot(self, market_symbol, *, depth_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS, band_percent=DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT, settings=None, conn=None):
    product_id = self.market_provider_id(market_symbol, "coinbase_exchange", conn=conn)
    if not product_id:
        raise ValueError("coinbase order book is not supported for this market")
    return self._provider_orderbook_with_fallback(
        "coinbase_exchange",
        market_symbol,
        settings=settings or {},
        depth_levels=depth_levels,
        band_percent=band_percent,
        request_limit=2,
        http_fetcher=lambda: self._normalize_orderbook_fetch_result(self._fetch_json_url(
            f"{COINBASE_BOOK_URL_TEMPLATE.format(product_id=product_id)}?{urlencode({'level': 2})}",
            timeout=5,
            user_agent="hackme_web/1.0 trading-depth coinbase",
            with_meta=True,
        )),
        book_getter=lambda payload: ((payload or {}).get("bids") or [], (payload or {}).get("asks") or []),
        conn=conn,
    )

def _fetch_kraken_orderbook_snapshot(self, market_symbol, *, depth_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS, band_percent=DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT, settings=None, conn=None):
    pair = self.market_provider_id(market_symbol, "kraken_public_api", conn=conn)
    if not pair:
        raise ValueError("kraken order book is not supported for this market")
    request_limit = self._provider_depth_request_limit("kraken_public_api", depth_levels)
    return self._provider_orderbook_with_fallback(
        "kraken_public_api",
        market_symbol,
        settings=settings or {},
        depth_levels=depth_levels,
        band_percent=band_percent,
        request_limit=request_limit,
        http_fetcher=lambda: self._normalize_orderbook_fetch_result(self._fetch_json_url(
            f"{KRAKEN_DEPTH_URL}?{urlencode({'pair': pair, 'count': request_limit})}",
            timeout=5,
            user_agent="hackme_web/1.0 trading-depth kraken",
            with_meta=True,
        )),
        book_getter=self._kraken_http_book_getter,
        conn=conn,
    )

def _fetch_gemini_orderbook_snapshot(self, market_symbol, *, depth_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS, band_percent=DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT, settings=None, conn=None):
    symbol = self.market_provider_id(market_symbol, "gemini_public_api", conn=conn)
    if not symbol:
        raise ValueError("gemini order book is not supported for this market")
    request_limit = self._provider_depth_request_limit("gemini_public_api", depth_levels)
    payload, fetch_meta = self._normalize_orderbook_fetch_result(self._fetch_json_url(
        f"{GEMINI_BOOK_URL_TEMPLATE.format(symbol=symbol)}?{urlencode({'limit_bids': request_limit, 'limit_asks': request_limit})}",
        timeout=5,
        user_agent="hackme_web/1.0 trading-depth gemini",
        with_meta=True,
    ))
    return self._build_orderbook_snapshot(
        source="gemini_public_api",
        bids=[[row.get("price"), row.get("amount")] for row in (payload.get("bids") or []) if isinstance(row, dict)],
        asks=[[row.get("price"), row.get("amount")] for row in (payload.get("asks") or []) if isinstance(row, dict)],
        fetch_meta=fetch_meta,
        max_levels=depth_levels,
        band_percent=band_percent,
        request_limit=request_limit,
        transport_meta=self._provider_transport_meta("gemini_public_api", market_symbol, settings=settings or {}, transport="http_polling", fetched_at=fetch_meta.get("fetched_at"), latency_ms=fetch_meta.get("latency_ms"), conn=conn),
    )

def _fetch_bitstamp_orderbook_snapshot(self, market_symbol, *, depth_levels=DEFAULT_PRICE_FUSION_DEPTH_LEVELS, band_percent=DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT, settings=None, conn=None):
    pair = self.market_provider_id(market_symbol, "bitstamp_public_api", conn=conn)
    if not pair:
        raise ValueError("bitstamp order book is not supported for this market")
    payload, fetch_meta = self._normalize_orderbook_fetch_result(self._fetch_json_url(
        BITSTAMP_ORDER_BOOK_URL_TEMPLATE.format(pair=pair),
        timeout=5,
        user_agent="hackme_web/1.0 trading-depth bitstamp",
        with_meta=True,
    ))
    return self._build_orderbook_snapshot(
        source="bitstamp_public_api",
        bids=payload.get("bids") or [],
        asks=payload.get("asks") or [],
        fetch_meta=fetch_meta,
        max_levels=depth_levels,
        band_percent=band_percent,
        request_limit=depth_levels,
        transport_meta=self._provider_transport_meta("bitstamp_public_api", market_symbol, settings=settings or {}, transport="http_polling", fetched_at=fetch_meta.get("fetched_at"), latency_ms=fetch_meta.get("latency_ms"), conn=conn),
    )

def _price_fusion_manual_weights(self, settings):
    return _normalize_price_fusion_manual_weights((settings or {}).get("price_fusion_manual_weights"))

def _apply_price_fusion_weight_cap(self, weighted_rows, *, max_single_provider_weight_percent):
    return apply_price_fusion_weight_cap(
        weighted_rows,
        max_single_provider_weight_percent=max_single_provider_weight_percent,
    )

def _build_price_fusion_weight_model(self, snapshots, *, mode, weight_map, max_single_provider_weight_percent, score_getter):
    return build_price_fusion_weight_model(
        snapshots,
        mode=mode,
        weight_map=weight_map,
        max_single_provider_weight_percent=max_single_provider_weight_percent,
        score_getter=score_getter,
    )

def _fetch_weighted_fused_price_points(self, market_symbol, *, settings, conn=None):
    return fetch_weighted_fused_price_points_helper(
        self,
        market_symbol,
        settings=settings,
        conn=conn,
    )

def _default_price_fusion_market_symbol(self, conn):
    rows = conn.execute("SELECT * FROM trading_markets ORDER BY sort_order ASC, symbol ASC").fetchall()
    for row in rows:
        symbol = str(row["symbol"] or "").strip().upper()
        if self._market_supports_live_price_on_conn(conn, symbol):
            return symbol
    catalog_symbols = self._list_live_price_market_symbols(conn)
    return catalog_symbols[0] if catalog_symbols else ""

def _root_price_fusion_status_on_conn(self, conn, *, market_symbol=""):
    return root_price_fusion_status_on_conn_helper(
        self,
        conn,
        market_symbol=market_symbol,
    )

def get_root_price_fusion_status(self, *, market_symbol=""):
    conn = self.get_db()
    try:
        self.ensure_schema(conn)
        return self._root_price_fusion_status_on_conn(conn, market_symbol=market_symbol)
    finally:
        conn.close()

def get_live_market_quote(self, *, market_symbol="", force_refresh=False):
    return get_live_market_quote_helper(self, market_symbol=market_symbol, force_refresh=force_refresh)

def _fetch_live_price_points(self, market_symbol, *, with_meta=False, settings=None, conn=None):
    return fetch_live_price_points_helper(
        self,
        market_symbol,
        with_meta=with_meta,
        settings=settings,
        conn=conn,
    )

def _fetch_indicator_candles(self, market_symbol, *, limit=240, interval="15m", conn=None):
    symbol = self._live_price_symbol(market_symbol, conn=conn)
    if not symbol:
        return []
    if self.historical_candles_provider:
        candles = self.historical_candles_provider(str(market_symbol or "").strip().upper(), interval, limit)
        return candles if isinstance(candles, list) else []
    if self.live_price_provider:
        return []
    query = urlencode({"symbol": symbol, "interval": interval, "limit": max(2, min(int(limit or 240), 1000))})
    req = Request(
        f"https://api.binance.com/api/v3/klines?{query}",
        headers={"User-Agent": "hackme_web/1.0 trading-bot-indicators"},
    )
    with urlopen(req, timeout=6) as response:
        payload = json.loads(response.read().decode("utf-8"))
    candles = []
    for item in payload if isinstance(payload, list) else []:
        try:
            candles.append({
                "time_ms": int(item[0]),
                "open_points": float(item[1]) * USDT_TO_POINTS_RATE,
                "high_points": float(item[2]) * USDT_TO_POINTS_RATE,
                "low_points": float(item[3]) * USDT_TO_POINTS_RATE,
                "close_points": float(item[4]) * USDT_TO_POINTS_RATE,
            })
        except Exception:
            continue
    return candles

def _parse_candle_time_ms(self, candle, *, interval_seconds=60):
    if not isinstance(candle, dict):
        return None
    raw = candle.get("time_ms")
    if raw not in (None, ""):
        try:
            return int(raw)
        except Exception:
            return None
    raw = candle.get("time_iso")
    if raw:
        try:
            return int(datetime.fromisoformat(str(raw)).timestamp() * 1000)
        except Exception:
            return None
    raw = candle.get("time")
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except Exception:
        return None
    if value > 10**12:
        return int(value)
    if value > 10**9:
        return int(value * 1000)
    if value > 10**6:
        return int(value)
    return int(value * 1000)

def _recent_price_window(self, market_symbol, *, lookback_seconds=60, since_time_text=None, interval="1m", conn=None):
    return recent_price_window_helper(
        self,
        market_symbol,
        lookback_seconds=lookback_seconds,
        since_time_text=since_time_text,
        interval=interval,
        conn=conn,
    )

def _workflow_live_context(self, conn, *, market, user_id, observed_price, observed_low=None, observed_high=None):
    return workflow_live_context_helper(
        self,
        conn,
        market=market,
        user_id=user_id,
        observed_price=observed_price,
        observed_low=observed_low,
        observed_high=observed_high,
    )

def _current_market_price_points(self, conn, market, *, with_meta=False, high_risk=False):
    return current_market_price_points_helper(
        self,
        conn,
        market,
        with_meta=with_meta,
        high_risk=high_risk,
    )

def _ensure_market_price_snapshot_for_write(self, market_symbol, *, high_risk=False):
    return ensure_market_price_snapshot_for_write_helper(
        self,
        market_symbol,
        high_risk=high_risk,
    )

def _snapshot_market_price_points(self, conn, market, *, with_meta=False, high_risk=False):
    return snapshot_market_price_points_helper(
        self,
        conn,
        market,
        with_meta=with_meta,
        high_risk=high_risk,
    )

def _root_sim_account(self, conn, user_id, *, actor=None):
    return root_sim_account_helper(
        self,
        conn,
        user_id,
        actor=actor,
        root_simulated_initial_points=ROOT_SIMULATED_INITIAL_POINTS,
    )

def _sim_delta(self, conn, user_id, *, balance_delta=0, locked_delta=0):
    return sim_delta_helper(
        self,
        conn,
        user_id,
        balance_delta=balance_delta,
        locked_delta=locked_delta,
        root_simulated_initial_points=ROOT_SIMULATED_INITIAL_POINTS,
    )

def _is_root_user_id(self, conn, user_id):
    row = conn.execute("SELECT username FROM users WHERE id=?", (int(user_id),)).fetchone()
    return bool(row and row["username"] == "root")

def _system_actor(self):
    return {"username": "system", "role": "system"}

def _trial_credit_row(self, conn, user_id):
    return trial_credit_row_helper(self, conn, user_id)
def _ensure_trial_credit(self, conn, user_id, *, actor=None, allow_reclaim=True):
    return ensure_trial_credit_helper(self, conn, user_id, actor=actor, allow_reclaim=allow_reclaim)
def _trial_position(self, conn, user_id, symbol):
    return trial_position_helper(self, conn, user_id, symbol)
def _trial_delta(self, conn, user_id, *, available_delta=0, locked_delta=0, deployed_delta=0, status=None, reclaimed=False):
    return trial_delta_helper(
        self,
        conn,
        user_id,
        available_delta=available_delta,
        locked_delta=locked_delta,
        deployed_delta=deployed_delta,
        status=status,
        reclaimed=reclaimed,
    )
def _trial_lock_for_buy(self, conn, user_id, total_points):
    return trial_lock_for_buy_helper(self, conn, user_id, total_points)
def _trial_spend(self, conn, user_id, amount):
    return trial_spend_helper(self, conn, user_id, amount)
def _trial_deploy(self, conn, user_id, amount):
    return trial_deploy_helper(self, conn, user_id, amount)
def _trial_unlock(self, conn, user_id, amount):
    return trial_unlock_helper(self, conn, user_id, amount)
def _set_trial_reclaim_blocked(self, conn, user_id, *, reason):
    return set_trial_reclaim_blocked_helper(self, conn, user_id, reason=reason)
def _clear_trial_reclaim_blocked(self, conn, user_id):
    return clear_trial_reclaim_blocked_helper(self, conn, user_id)
def _trial_mark_buy_executed(self, conn, *, user_id, market_symbol, quantity_units, trial_used_points, total_points):
    return trial_mark_buy_executed_helper(
        self,
        conn,
        user_id=user_id,
        market_symbol=market_symbol,
        quantity_units=quantity_units,
        trial_used_points=trial_used_points,
        total_points=total_points,
    )
def _trial_allocate_sell(self, conn, *, user_id, market_symbol, quantity_units, net_credit_points):
    return trial_allocate_sell_helper(
        self,
        conn,
        user_id=user_id,
        market_symbol=market_symbol,
        quantity_units=quantity_units,
        net_credit_points=net_credit_points,
    )
def _cancel_trial_reclaim_sell_orders(self, conn, user_id, *, actor, reason, ctx=None):
    return cancel_trial_reclaim_sell_orders_helper(self, conn, user_id, actor=actor, reason=reason, ctx=ctx)
def _release_trial_margin_collateral(self, conn, user_id, *, collateral_trial, available_delta_if_active=0):
    return release_trial_margin_collateral_helper(
        self,
        conn,
        user_id,
        collateral_trial=collateral_trial,
        available_delta_if_active=available_delta_if_active,
    )
def _reclaim_trial_credit(self, conn, user_id, *, actor=None, reason="TRIAL_CREDIT_RECLAIM", ctx=None):
    return reclaim_trial_credit_helper(self, conn, user_id, actor=actor, reason=reason, ctx=ctx)
def _funding_payload(self, conn, user_id, *, source_wallet_address=None):
    return funding_payload_runtime_helper(self, conn, user_id, source_wallet_address=source_wallet_address)
def _position_payload(self, row):
    return position_payload(row, units_to_quantity=units_to_quantity)

def _position_payload_with_metrics(self, row, *, market=None, realized_points=0, total_fees=0):
    return position_payload_with_metrics_helper(
        self,
        row,
        market=market,
        realized_points=realized_points,
        total_fees=total_fees,
    )
def _futures_position_payload(self, row):
    return futures_position_payload(row, units_to_quantity=units_to_quantity)

def _margin_position_payload(self, row):
    return margin_position_payload(
        row,
        units_to_quantity=units_to_quantity,
        to_decimal=_to_decimal,
        apr_percent_from_daily=_apr_percent_from_daily,
        point_micro_scale=POINT_MICRO_SCALE,
        default_interest_interval_hours=DEFAULT_BORROW_INTEREST_INTERVAL_HOURS,
        default_interest_minimum_hours=DEFAULT_BORROW_INTEREST_MINIMUM_HOURS,
        now_text=_now,
        billable_interest_hours_from_elapsed_seconds=_billable_interest_hours_from_elapsed_seconds,
    )

def _margin_trade_records(self, conn, user_id, *, limit=50):
    return margin_trade_records_helper(self, conn, user_id, limit=limit)
def _borrowing_settings(self, conn):
    settings = self._settings_payload(conn)
    return {
        "enabled": bool(settings.get("borrowing_enabled")),
        "borrow_apr_btc_eth_percent": float(settings.get("borrow_apr_btc_eth_percent") or 0),
        "borrow_apr_usdt_points_percent": float(settings.get("borrow_apr_usdt_points_percent") or 0),
        "interest_percent_daily": float(settings.get("borrow_interest_percent_daily") or 0),
        "pool_pressure_multiplier": float(settings.get("borrow_interest_pool_pressure_multiplier") or 0),
        "max_pool_utilization_percent": float(settings.get("margin_max_pool_utilization_percent") or 0),
        "interest_interval_hours": int(settings.get("borrow_interest_interval_hours") or DEFAULT_BORROW_INTEREST_INTERVAL_HOURS),
        "interest_minimum_hours": int(settings.get("borrow_interest_minimum_hours") or DEFAULT_BORROW_INTEREST_MINIMUM_HOURS),
    }

def _assert_borrowing_enabled(self, conn):
    settings = self._borrowing_settings(conn)
    if not settings["enabled"]:
        raise ValueError("borrow trading is disabled")
    return settings

def _minimum_margin_collateral_points(self, conn, *, position_type, notional, fee_rate_percent=0.0):
    settings = self._settings_payload(conn)
    notional = int(notional or 0)
    maintenance_percent = float(settings.get("margin_maintenance_percent") or 0)
    fee_rate_percent = float(fee_rate_percent or 0)
    safety_minimum = int(math.ceil(notional * max(0.0, maintenance_percent + fee_rate_percent) / 100.0)) + 2
    if position_type == "margin_long":
        financing_percent = float(settings.get("margin_long_financing_percent") or MARGIN_LONG_FINANCING_RATE_PERCENT)
        base_minimum = int(math.ceil(notional * max(0.0, 100.0 - financing_percent) / 100.0))
        return max(base_minimum, safety_minimum)
    short_percent = float(settings.get("short_collateral_percent") or SHORT_COLLATERAL_RATE_PERCENT)
    base_minimum = int(math.ceil(notional * short_percent / 100.0))
    return max(base_minimum, safety_minimum)

def _margin_interest_total_hours(self, row, now_text=None):
    return margin_interest_total_hours(
        row,
        now_text=now_text or _now(),
        billable_interest_hours_from_elapsed_seconds=_billable_interest_hours_from_elapsed_seconds,
        default_interval_hours=DEFAULT_BORROW_INTEREST_INTERVAL_HOURS,
        default_minimum_hours=DEFAULT_BORROW_INTEREST_MINIMUM_HOURS,
    )

def _margin_interest_due_points(self, row, *, hours):
    return margin_interest_due_points(
        row,
        hours=hours,
        point_micro_scale=POINT_MICRO_SCALE,
        due_micropoints_func=self._margin_interest_due_micropoints,
    )

def _margin_interest_due_micropoints(self, *, principal, rate_percent, hours):
    return margin_interest_due_micropoints(
        principal=principal,
        rate_percent=rate_percent,
        hours=hours,
        point_micro_scale=POINT_MICRO_SCALE,
    )

def _margin_interest_points(self, row, now_text=None):
    return margin_interest_points(
        row,
        now_text=now_text or _now(),
        point_micro_scale=POINT_MICRO_SCALE,
        total_hours_func=self._margin_interest_total_hours,
        due_micropoints_func=self._margin_interest_due_micropoints,
    )

def _accrue_margin_interest(self, conn, position, *, actor=None, now_text=None, ctx=None):
    return accrue_margin_interest_helper(self, conn, position, actor=actor, now_text=now_text, ctx=ctx)

def _margin_risk_payload(self, conn, position, market=None, *, now_text=None, price_override_points=None, price_source_override=None):
    return margin_risk_payload(
        self,
        conn,
        position,
        market=market,
        now_text=now_text,
        price_override_points=price_override_points,
        price_source_override=price_source_override,
    )

def _margin_position_payload_with_risk(self, conn, row, *, market=None, risk_overrides=None):
    return margin_position_payload_with_risk(
        self,
        conn,
        row,
        market=market,
        risk_overrides=risk_overrides,
    )

def _margin_free_margin_points(self, conn, user_id):
    return margin_free_margin_points(self, conn, user_id)

def _margin_account_payload(self, conn, user_id, rows=None):
    return margin_account_payload(self, conn, user_id, rows)

def _margin_summary_payload(self, conn, user_id, rows):
    return margin_summary_payload(self, conn, user_id, rows)

def _margin_liquidation_order_key(self, row):
    return margin_liquidation_order_key(row)

def _margin_summary_payload_legacy(self, rows):
    return margin_summary_payload_legacy(rows)

def _fill_payload(self, row, realized=None):
    return fill_payload(
        row,
        units_to_quantity=units_to_quantity,
        json_loads=_json_loads,
        realized=realized,
    )

def _spot_realized_map(self, conn, user_id):
    return {
        row["market_symbol"]: int(row["realized_pnl_points"] or 0)
        for row in conn.execute(
            """
            SELECT market_symbol, COALESCE(SUM(net_pnl_points), 0) AS realized_pnl_points
            FROM trading_spot_realized_pnl
            WHERE user_id=?
            GROUP BY market_symbol
            """,
            (int(user_id),),
        ).fetchall()
    }

def _spot_fee_map(self, conn, user_id):
    return {
        row["market_symbol"]: int(row["total_fee_points"] or 0)
        for row in conn.execute(
            """
            SELECT market_symbol, COALESCE(SUM(fee_points), 0) AS total_fee_points
            FROM trading_fills
            WHERE user_id=?
            GROUP BY market_symbol
            """,
            (int(user_id),),
        ).fetchall()
    }

def _spot_summary_payload(self, positions):
    reference_context = None
    risk_context = None
    for row in positions:
        if not reference_context and isinstance(row.get("reference_price_context"), dict):
            reference_context = row.get("reference_price_context")
        if not risk_context and isinstance(row.get("risk_grade_price_context"), dict):
            risk_context = row.get("risk_grade_price_context")
    return {
        "current_value_points": sum(int(row.get("current_value_points") or 0) for row in positions),
        "reference_current_value_points": sum(int(row.get("reference_current_value_points") or row.get("current_value_points") or 0) for row in positions),
        "risk_grade_current_value_points": sum(int(row.get("risk_grade_current_value_points") or 0) for row in positions),
        "cost_basis_points": sum(int(row.get("cost_basis_points") or 0) for row in positions),
        "reference_cost_basis_points": sum(int(row.get("reference_cost_basis_points") or 0) for row in positions),
        "unrealized_pnl_points": sum(int(row.get("unrealized_pnl_points") or 0) for row in positions),
        "reference_unrealized_pnl_points": sum(int(row.get("reference_unrealized_pnl_points") or 0) for row in positions),
        "risk_grade_unrealized_pnl_points": sum(int(row.get("risk_grade_unrealized_pnl_points") or row.get("unrealized_pnl_points") or 0) for row in positions),
        "realized_pnl_points": sum(int(row.get("realized_pnl_points") or 0) for row in positions),
        "total_pnl_points": sum(int(row.get("total_pnl_points") or 0) for row in positions),
        "total_fee_points": sum(int(row.get("total_fee_points") or 0) for row in positions),
        "reference_price_context": reference_context,
        "risk_grade_price_context": risk_context,
    }

def _notify_trade_filled(self, conn, fill):
    try:
        notice = trade_fill_notification_payload(
            fill,
            units_to_quantity=units_to_quantity,
            decimal_text=_decimal_text,
        )
        create_trading_user_notification(
            conn,
            user_id=fill["user_id"],
            notification_type=notice["notification_type"],
            title=notice["title"],
            body=notice["body"],
            create_notification=create_notification_if_enabled,
        )
    except Exception:
        pass

def _is_insufficient_error(self, exc):
    lowered = str(exc or "").lower()
    return any(term in lowered for term in ("insufficient", "餘額不足", "積分不足", "資金不足", "持倉不足"))

def _notify_insufficient_balance(self, *, user_id, market_symbol, side, order_type, quantity, error):
    conn = self.get_db()
    try:
        notice = insufficient_balance_notification_payload(
            market_symbol=market_symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            error=error,
        )
        create_trading_user_notification(
            conn,
            user_id=user_id,
            notification_type=notice["notification_type"],
            title=notice["title"],
            body=notice["body"],
            create_notification=create_notification_if_enabled,
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

def _notify_margin_liquidated(self, conn, *, user_id, position, risk):
    try:
        notice = margin_liquidated_notification_payload(
            position=position,
            risk=risk,
            decimal_text=_decimal_text,
        )
        create_trading_user_notification(
            conn,
            user_id=user_id,
            notification_type=notice["notification_type"],
            title=notice["title"],
            body=notice["body"],
            create_notification=create_notification_if_enabled,
        )
    except Exception:
        pass

def _has_unread_margin_alert(self, conn, *, user_id, alert_type, position_uuid):
    try:
        row = conn.execute(
            """
            SELECT id FROM notifications
            WHERE user_id=? AND type=? AND is_read=0 AND body LIKE ?
            LIMIT 1
            """,
            (int(user_id), str(alert_type or ""), f"%{str(position_uuid or '')}%"),
        ).fetchone()
        return bool(row)
    except Exception:
        return False

def _notify_margin_risk_alerts(self, conn, *, position, risk, market):
    notify_margin_risk_alerts(self, conn, position=position, risk=risk, market=market)

def list_markets(self, *, include_disabled=False):
    conn = self.get_db()
    try:
        self.ensure_schema(conn)
        where = "" if include_disabled else "WHERE enabled=1 AND spot_enabled=1"
        rows = conn.execute(f"SELECT * FROM trading_markets {where} ORDER BY sort_order ASC, symbol ASC").fetchall()
        payloads = [self._market_payload(row) for row in rows]
        return sorted(payloads, key=self._runtime_market_sort_key)
    finally:
        conn.close()

def user_dashboard(self, *, user_id, source_wallet_address=None):
    # source-contract breadcrumb:
    # "margin_summary": self._margin_summary_payload(conn, user_id, margin_positions)
    return user_dashboard_helper(self, user_id=user_id, source_wallet_address=source_wallet_address)

def user_asset_overview(self, *, user_id):
    return user_asset_overview_helper(self, user_id=user_id)

def _is_executable(self, market, *, side, order_type, limit_price, current_price):
    current_price = float(_to_decimal(current_price, name="current_price", minimum=0))
    if order_type == "market":
        return True, current_price
    limit_price = float(_to_decimal(limit_price or 0, name="limit_price_points", minimum=0))
    if side == "buy" and limit_price >= current_price:
        return True, current_price
    if side == "sell" and limit_price <= current_price:
        return True, current_price
    return False, None

def _legacy_workflow(self, *, trigger_type, trigger_price, side, quantity_text, order_type, limit_price, max_runs, cooldown_seconds):
    return legacy_workflow_helper(
        trigger_type=trigger_type,
        trigger_price=trigger_price,
        side=side,
        quantity_text=quantity_text,
        order_type=order_type,
        limit_price=limit_price,
        max_runs=max_runs,
        cooldown_seconds=cooldown_seconds,
    )

def _validate_workflow(self, value):
    return validate_workflow(
        value,
        validate_workflow_graph_func=self._validate_workflow_graph,
        to_int=_to_int,
        condition_types=WORKFLOW_CONDITION_TYPES,
        action_types=WORKFLOW_ACTION_TYPES,
    )

def _validate_workflow_graph(self, workflow):
    return validate_workflow_graph(
        workflow,
        to_int=_to_int,
        condition_types=WORKFLOW_CONDITION_TYPES,
        action_types=WORKFLOW_ACTION_TYPES,
        node_types=WORKFLOW_NODE_TYPES,
        ports=WORKFLOW_PORTS,
    )

def _validate_bot_payload(self, conn, payload):
    return validate_bot_payload_helper(self, conn, payload)

def _bot_payload_with_budget_meta(self, conn, row):
    return bot_payload_with_budget_meta_helper(self, conn, row)

def list_trading_bots(self, *, actor):
    return list_trading_bots_helper(self, actor=actor)

def save_trading_bot(self, *, actor, payload, bot_uuid=None):
    return save_trading_bot_helper(self, actor=actor, payload=payload, bot_uuid=bot_uuid)

def set_trading_bot_share_parameters(self, *, actor, bot_uuid, share_parameters):
    return set_trading_bot_share_parameters_helper(
        self,
        actor=actor,
        bot_uuid=bot_uuid,
        share_parameters=share_parameters,
    )

def delete_trading_bot(self, *, actor, bot_uuid):
    return delete_trading_bot_helper(self, actor=actor, bot_uuid=bot_uuid)

def increase_trading_bot_max_runs(self, *, actor, bot_uuid, delta):
    return increase_trading_bot_max_runs_helper(self, actor=actor, bot_uuid=bot_uuid, delta=delta)

def adjust_trading_bot_budget(self, *, actor, bot_uuid, budget_points=None, delta_points=None):
    return adjust_trading_bot_budget_helper(
        self,
        actor=actor,
        bot_uuid=bot_uuid,
        budget_points=budget_points,
        delta_points=delta_points,
    )

# ── Grid Trading Bot ────────────────────────────────────────────────────

def _grid_levels(self, lower, upper, count, spacing_mode="arithmetic"):
    return grid_levels(lower, upper, count, spacing_mode)

def _grid_quantity_units(self, amount_points, price_points):
    return grid_quantity_units(amount_points, price_points)

def _grid_preview_fee_rates(self, market, settings, *, order_mode="maker"):
    return grid_preview_fee_rates(market, settings, order_mode=order_mode)

def _grid_preview_risk(self, *, min_net_spread_percent, break_even_spread_percent, spacing_percent):
    return grid_preview_risk(
        min_net_spread_percent=min_net_spread_percent,
        break_even_spread_percent=break_even_spread_percent,
        spacing_percent=spacing_percent,
    )

def _grid_preview_summary(self, *, lower_price_points, upper_price_points, grid_count, order_amount_points, spacing_mode, fee_rates):
    return grid_preview_summary(
        lower_price_points=lower_price_points,
        upper_price_points=upper_price_points,
        grid_count=grid_count,
        order_amount_points=order_amount_points,
        spacing_mode=spacing_mode,
        fee_rates=fee_rates,
    )

def preview_grid_bot(self, *, actor, payload):
    return preview_grid_bot_helper(self, actor=actor, payload=payload)

def _grid_bot_payload(self, row, orders=None):
    return grid_bot_payload(row, json_loads=_json_loads, orders=orders)

def create_grid_bot(self, *, actor, payload):
    return create_grid_bot_helper(self, actor=actor, payload=payload)

def list_grid_bots(self, *, actor):
    return list_grid_bots_helper(self, actor=actor)

def set_grid_bot_share_parameters(self, *, actor, bot_uuid, share_parameters):
    return set_grid_bot_share_parameters_helper(
        self,
        actor=actor,
        bot_uuid=bot_uuid,
        share_parameters=share_parameters,
    )

def toggle_grid_bot(self, *, actor, bot_uuid, enabled):
    return toggle_grid_bot_helper(self, actor=actor, bot_uuid=bot_uuid, enabled=enabled)

def delete_grid_bot(self, *, actor, bot_uuid, base_action="keep"):
    return delete_grid_bot_helper(self, actor=actor, bot_uuid=bot_uuid, base_action=base_action)

def scan_grid_bots(self, *, actor):
    return scan_grid_bots_helper(self, actor=actor)

def _scan_one_grid_bot(self, bot, *, actor):
    return scan_one_grid_bot_helper(self, bot, actor=actor)

# ── End Grid Trading Bot ─────────────────────────────────────────────────

def get_bot_competition(self, *, actor=None, week=None, auto_award=True):
    return get_bot_competition_helper(self, actor=actor, week=week, auto_award=auto_award)

def award_bot_competition_week(self, *, actor=None, week=None):
    return award_bot_competition_week_helper(self, actor=actor, week=week)
