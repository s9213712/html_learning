import hashlib
import inspect
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from services.core.sqlite_safe import table_columns
from services.system.notifications import create_notification_if_enabled, create_root_notification_if_enabled
from services.points_chain import (
    DISPLAY_CURRENCY,
    actor_value,
    compute_ledger_hash,
    create_official_hot_wallet,
    is_pc0_internal_address,
    metadata_hash,
    normalize_currency_type,
    public_account_id,
    utc_now,
    _metadata_json_checked,
)
from services.points_chain.economy_layer import (
    DEFAULT_ECONOMY_POLICY,
    append_economy_incident,
    economy_layer_report,
)
from services.server_mode.context import SmV2Context, current_ctx
from services.server_mode.routing import resolve_table
from services.trading.accounting.core import (
    _decimal_units,
    _quantity_step_units_from_precision,
    fee_points,
    notional_points,
    quantity_to_units,
    units_to_quantity,
)
from services.trading.accounting.funding_pool import (
    funding_pool_outstanding_principal,
    funding_pool_payload,
)
from services.trading.accounting.interest import (
    margin_interest_due_micropoints,
    margin_interest_due_points,
    margin_interest_points,
    margin_interest_total_hours,
)
from services.trading.accounting.trial_credit import (
    trial_allocate_sell_result,
    trial_credit_expires_at,
    trial_credit_payload,
    trial_credit_status_after_delta,
    trial_units_for_buy,
)
from services.trading.audit import emit_trading_audit_event
from services.trading.constants import (
    APR_DAYS_PER_YEAR,
    ASSET_SCALE,
    DEFAULT_PRICE_FUSION_MIN_PROVIDER_COUNT,
    DEFAULT_PRICE_FUSION_TRADE_MIN_PROVIDER_COUNT,
    DEFAULT_GRID_FEE_DISCOUNT_PERCENT,
    DEFAULT_SPOT_FEE_RATE_PERCENT,
    DEFAULT_TRADING_PRICE_SOURCE,
    DEPTH_CAPABLE_PROVIDERS,
    GRID_PREVIEW_YELLOW_NET_SPREAD_PERCENT,
    OPEN_ORDER_STATUSES,
    POINT_MICRO_SCALE,
    PRICE_PROVIDER_LABELS,
    REFERENCE_PRICE_CAPABLE_PROVIDERS,
    TICKER_CAPABLE_PROVIDERS,
    TRADING_BOT_AUDIT_INTERVAL_SECONDS,
    TRADING_BOT_AUDIT_LIMIT,
    TRADING_BOT_AUDIT_MIN_ENABLED_SECONDS,
    UNLIMITED_BOT_MAX_RUNS,
    WEIGHTED_PRICE_PROVIDERS,
    WORKFLOW_ACTION_TYPES,
    WORKFLOW_CONDITION_TYPES,
    WORKFLOW_NODE_TYPES,
    WORKFLOW_PORTS,
)
from services.trading.settings_schema import (
    TRADING_ROOT_BOOL_SETTING_KEYS,
    apr_pair_from_raw,
    bool_input_text,
    choice_input_value,
    daily_from_apr,
    float_input_text,
    int_input_text,
    interval_pair_from_raw,
    raw_bool_setting,
    raw_choice_setting,
    raw_float_setting,
    raw_int_setting,
    text_input_value,
    write_apr_from_daily,
)
from services.trading.notifications import (
    create_trading_user_notification,
    insufficient_balance_notification_payload,
    margin_liquidated_notification_payload,
    trade_fill_notification_payload,
)
from services.trading.admin import (
    allocate_reserve as allocate_reserve_helper,
    get_backtest_capacity_measurement as get_backtest_capacity_measurement_helper,
    get_backtest_capacity_time_budget_seconds as get_backtest_capacity_time_budget_seconds_helper,
    get_max_backtest_candles as get_max_backtest_candles_helper,
    get_root_settings as get_root_settings_helper,
    record_backtest_capacity_measurement as record_backtest_capacity_measurement_helper,
    settings_payload as settings_payload_helper,
    update_market as update_market_helper,
    update_root_settings as update_root_settings_helper,
)
from services.trading.background_engine import (
    enqueue_background_job_once as enqueue_background_job_once_helper,
    ensure_background_schema as ensure_background_schema_helper,
    get_background_status as get_background_status_helper,
    get_root_trading_snapshot as get_root_trading_snapshot_helper,
    refresh_root_trading_snapshots as refresh_root_trading_snapshots_helper,
    run_background_job_once as run_background_job_once_helper,
    run_due_background_jobs as run_due_background_jobs_helper,
    set_background_job_enabled as set_background_job_enabled_helper,
)
from services.trading.margin import (
    accrue_margin_interest as accrue_margin_interest_helper,
    add_margin_collateral as add_margin_collateral_helper,
    close_margin_position as close_margin_position_helper,
    margin_account_payload,
    margin_free_margin_points,
    margin_liquidation_order_key,
    margin_position_payload_with_risk,
    margin_risk_payload,
    margin_summary_payload,
    margin_summary_payload_legacy,
    notify_margin_risk_alerts,
    open_margin_position as open_margin_position_helper,
    scan_margin_risk_targets as scan_margin_risk_targets_helper,
    scan_margin_liquidations as scan_margin_liquidations_helper,
    withdraw_margin_collateral as withdraw_margin_collateral_helper,
)
from services.trading.orders import (
    cancel_order as cancel_order_helper,
    execute_order as execute_order_helper,
    match_open_limit_orders as match_open_limit_orders_helper,
    place_order as place_order_helper,
    scan_spot_risk_targets as scan_spot_risk_targets_helper,
)
from services.trading.price_fusion.context import (
    price_context_confidence,
    price_context_risk_grade_usable,
    price_source_label,
    price_usage_label,
)
from services.trading.price_fusion.orderbook import (
    depth_notional_score,
    depth_notional_snapshot,
    parse_orderbook_side,
    provider_depth_request_limit,
)
from services.trading.price_fusion.weights import (
    apply_price_fusion_weight_cap,
    build_price_fusion_weight_model,
    price_fusion_effective_score,
    price_fusion_reference_score,
)
from services.trading.payloads import (
    bot_audit_eligibility_reason_label,
    bot_audit_label,
    bot_payload,
    bot_run_payload,
    fill_payload,
    futures_position_payload,
    margin_position_payload,
    market_payload,
    order_payload,
    position_payload,
)
from services.trading.price_runtime import (
    append_price_fusion_warning as append_price_fusion_warning_helper,
    build_orderbook_snapshot as build_orderbook_snapshot_helper,
    build_price_context as build_price_context_helper,
    current_market_price_points as current_market_price_points_helper,
    ensure_market_price_snapshot_for_write as ensure_market_price_snapshot_for_write_helper,
    fetch_live_price_points as fetch_live_price_points_helper,
    fetch_weighted_fused_price_points as fetch_weighted_fused_price_points_helper,
    get_live_market_quote as get_live_market_quote_helper,
    price_fusion_warning as price_fusion_warning_helper,
    price_stream_provider_state as price_stream_provider_state_helper,
    primary_price_fusion_warning as primary_price_fusion_warning_helper,
    provider_orderbook_with_fallback as provider_orderbook_with_fallback_helper,
    provider_quantity_unit_info as provider_quantity_unit_info_helper,
    provider_ticker_with_fallback as provider_ticker_with_fallback_helper,
    provider_transport_meta as provider_transport_meta_helper,
    recent_price_window as recent_price_window_helper,
    resolve_stream_orderbook_snapshot as resolve_stream_orderbook_snapshot_helper,
    resolve_stream_ticker_snapshot as resolve_stream_ticker_snapshot_helper,
    root_price_fusion_status_on_conn as root_price_fusion_status_on_conn_helper,
    snapshot_market_price_points as snapshot_market_price_points_helper,
    stored_market_price_contexts as stored_market_price_contexts_helper,
    transport_state_from_provider_rows as transport_state_from_provider_rows_helper,
)
from services.trading.reporting import (
    funding_payload as funding_payload_runtime_helper,
    margin_trade_records as margin_trade_records_helper,
    position_payload_with_metrics as position_payload_with_metrics_helper,
    root_report as root_report_helper,
    user_asset_overview as user_asset_overview_helper,
    user_dashboard as user_dashboard_helper,
)
from services.trading.shadow import (
    ensure_shadow_wallet as ensure_shadow_wallet_helper,
    shadow_actor_user_id as shadow_actor_user_id_helper,
    shadow_existing_ledger_row as shadow_existing_ledger_row_helper,
    shadow_last_ledger_hash as shadow_last_ledger_hash_helper,
    shadow_record_transaction as shadow_record_transaction_helper,
    shadow_wallet_payload as shadow_wallet_payload_helper,
    wallet_payload as wallet_payload_helper,
    wallet_row as wallet_row_helper,
)
from services.trading.trial_credit import (
    cancel_trial_reclaim_sell_orders as cancel_trial_reclaim_sell_orders_helper,
    clear_trial_reclaim_blocked as clear_trial_reclaim_blocked_helper,
    ensure_trial_credit as ensure_trial_credit_helper,
    reclaim_trial_credit as reclaim_trial_credit_helper,
    release_trial_margin_collateral as release_trial_margin_collateral_helper,
    set_trial_reclaim_blocked as set_trial_reclaim_blocked_helper,
    trial_allocate_sell as trial_allocate_sell_helper,
    trial_credit_row as trial_credit_row_helper,
    trial_deploy as trial_deploy_helper,
    trial_delta as trial_delta_helper,
    trial_lock_for_buy as trial_lock_for_buy_helper,
    trial_mark_buy_executed as trial_mark_buy_executed_helper,
    trial_position as trial_position_helper,
    trial_spend as trial_spend_helper,
    trial_unlock as trial_unlock_helper,
)
from services.trading.grid import (
    create_grid_bot as create_grid_bot_helper,
    delete_grid_bot as delete_grid_bot_helper,
    grid_bot_payload,
    grid_fee_rate_percent,
    grid_levels,
    list_grid_bots as list_grid_bots_helper,
    grid_preview_fee_rates,
    grid_preview_risk,
    grid_preview_summary,
    grid_quantity_units,
    preview_grid_bot as preview_grid_bot_helper,
    scan_grid_bots as scan_grid_bots_helper,
    scan_one_grid_bot as scan_one_grid_bot_helper,
    set_grid_bot_share_parameters as set_grid_bot_share_parameters_helper,
    toggle_grid_bot as toggle_grid_bot_helper,
)
from services.trading.bots.indicators import (
    build_workflow_indicator_series,
    workflow_indicator_context,
)
from services.trading.bots.workflow import (
    condition_label,
    validate_workflow,
    validate_workflow_graph,
    workflow_condition_hit,
    workflow_decision,
    workflow_graph_decision,
)
from services.trading.bots.service import (
    bot_audit_candidates as bot_audit_candidates_helper,
    bot_audit_dashboard_on_conn as bot_audit_dashboard_on_conn_helper,
    bot_audit_enabled_at_on_row as bot_audit_enabled_at_helper,
    bot_audit_is_eligible_on_row as bot_audit_is_eligible_helper,
    bot_audit_latest_map_on_conn as bot_audit_latest_map_helper,
    bot_audit_run_findings as bot_audit_run_findings_helper,
    bot_condition_checks as bot_condition_checks_helper,
    bot_trigger_hit as bot_trigger_hit_helper,
    adjust_trading_bot_budget as adjust_trading_bot_budget_helper,
    delete_trading_bot as delete_trading_bot_helper,
    get_bot_audit_dashboard as get_bot_audit_dashboard_helper,
    increase_trading_bot_max_runs as increase_trading_bot_max_runs_helper,
    legacy_workflow as legacy_workflow_helper,
    list_trading_bots as list_trading_bots_helper,
    quantity_text_from_budget as quantity_text_from_budget_helper,
    record_bot_audit_run as record_bot_audit_run_helper,
    record_bot_run as record_bot_run_helper,
    run_due_bot_audits as run_due_bot_audits_helper,
    run_due_trading_bots as run_due_trading_bots_helper,
    run_trading_bot_once as run_trading_bot_once_helper,
    run_trading_bot_rows as run_trading_bot_rows_helper,
    run_trading_bots as run_trading_bots_helper,
    save_trading_bot as save_trading_bot_helper,
    set_trading_bot_share_parameters as set_trading_bot_share_parameters_helper,
    validate_bot_payload as validate_bot_payload_helper,
    _bot_payload_with_budget_meta as bot_payload_with_budget_meta_helper,
    workflow_live_context as workflow_live_context_helper,
    workflow_order_from_decision as workflow_order_from_decision_helper,
)
from services.trading.competition import (
    award_bot_competition_week as award_bot_competition_week_helper,
    get_bot_competition as get_bot_competition_helper,
)
from services.trading.backtest import (
    backtest_trading_bot as backtest_trading_bot_helper,
)
from services.trading.funding import (
    close_root_contract_position as close_root_contract_position_helper,
    funding_payload as funding_payload_helper,
    funding_snapshot_ctx as funding_snapshot_ctx_helper,
    get_funding_rate_snapshot as get_funding_rate_snapshot_helper,
    open_root_contract_position as open_root_contract_position_helper,
    publish_funding_rate_snapshot as publish_funding_rate_snapshot_helper,
    reset_root_simulated_balance as reset_root_simulated_balance_helper,
    root_sim_account as root_sim_account_helper,
    settle_funding_adjustment as settle_funding_adjustment_helper,
    sim_delta as sim_delta_helper,
)
from services.trading.markets import (
    create_market_provider_mapping as create_market_provider_mapping_helper,
    create_market_registry as create_market_registry_helper,
    disable_market_provider_mapping as disable_market_provider_mapping_helper,
    disable_market_registry as disable_market_registry_helper,
    fallback_market_display_symbol,
    get_market_provider_registry as get_market_provider_registry_helper,
    list_market_registry as list_market_registry_helper,
    market_display_symbol_from_registry_row,
    market_provider_mapping_payload as market_provider_mapping_payload_helper,
    market_provider_ids_from_mappings,
    market_registry_audit as market_registry_audit_helper,
    market_registry_payload as market_registry_payload_helper,
    market_seed_compare_value,
    market_supports_mapping_rows,
    normalize_market_symbol_from_rows,
    persist_market_registry_probe as persist_market_registry_probe_helper,
    probe_market_registry as probe_market_registry_helper,
    probe_market_registry_on_conn as probe_market_registry_on_conn_helper,
    provider_mapping_capabilities,
    registry_seed_status,
    update_market_provider_mapping as update_market_provider_mapping_helper,
    update_market_registry as update_market_registry_helper,
    validate_market_provider_mapping_payload as validate_market_provider_mapping_payload_helper,
    validate_market_registry_payload as validate_market_registry_payload_helper,
)
from services.trading.validators import (
    _apr_percent_from_daily,
    _billable_interest_hours_from_elapsed_seconds,
    _daily_percent_from_apr,
    _decimal_text,
    _normalize_borrow_interest_timing,
    _to_decimal,
    _to_float,
    _to_int,
    _to_price_float,
)
from services.trading.verification import (
    ledger_row as ledger_row_helper,
    replay_positions as replay_positions_helper,
    verify_fill_ledgers as verify_fill_ledgers_helper,
    verify_margin_position_locks as verify_margin_position_locks_helper,
    verify_open_order_locks as verify_open_order_locks_helper,
    verify_reserve_pool as verify_reserve_pool_helper,
    verify_sim_accounts as verify_sim_accounts_helper,
    verify_spot_realized_pnl as verify_spot_realized_pnl_helper,
    verify_state as verify_state_helper,
    verify_state_on_conn as verify_state_on_conn_helper,
)
from services.trading.mode_gate import (
    assert_same_world,
    assert_trading_allowed,
    funding_channel_key,
    liquidation_settle_table,
    liquidation_target_table,
    matching_orderbook_key,
)
from services.trading.catalog import (
    TRADING_MARKET_CATALOG_SEED_VERSION,
    list_market_definitions,
    list_live_price_markets,
    list_seed_markets,
    market_display_symbol,
    market_provider_id,
    market_sort_key,
    market_supports_btc_trade,
    market_supports_live_price,
    normalize_market_symbol,
)
from services.trading.streams import TradingPriceStreamHub, WS_CAPABLE_PRICE_PROVIDERS


USDT_TO_POINTS_RATE = 1
ROOT_SIMULATED_INITIAL_POINTS = 10_000
TRIAL_CREDIT_INITIAL_POINTS = 1_000
TRIAL_CREDIT_DAYS = 7
TRADING_FUNDING_POOL_LEGACY_INITIAL_POINTS = 10_000
TRADING_FUNDING_POOL_INITIAL_POINTS = int(DEFAULT_ECONOMY_POLICY["exchange_fund_initial"])
TRADING_FUNDING_POOL_PRESSURE_MULTIPLIER = 4.0
MARGIN_MAX_POOL_UTILIZATION_PERCENT = 80.0
MARGIN_LONG_FINANCING_RATE_PERCENT = 90.0
SHORT_COLLATERAL_RATE_PERCENT = 60.0
EXCHANGE_LIABILITY_LIMIT_POINTS = 0
EXCHANGE_LIABILITY_GRACE_MINUTES = 60
PROFIT_SETTLEMENT_INTERVAL_MINUTES = 0
SUPPORTED_EXECUTION_MODES = {"house_counterparty", "pvp_matching", "hybrid_liquidity"}
BACKTEST_SEGMENT_CANDLES = 10_000
# Default cap; root may override via trading_settings 'trading.backtest_max_candles'.
# Hard floor (1000) and ceiling (10_000_000) are enforced wherever the setting is consumed.
MAX_BACKTEST_CANDLES = 20_000
BACKTEST_MAX_CANDLES_FLOOR = 1_000
BACKTEST_MAX_CANDLES_CEILING = 10_000_000
# First-boot probe budget bounds (seconds). Default 60s gives a stable signal;
# floor 5s prevents nonsense projections from dust-sized probes.
BACKTEST_CAPACITY_TIME_BUDGET_DEFAULT_SECONDS = 60
BACKTEST_CAPACITY_TIME_BUDGET_MIN_SECONDS = 5
BACKTEST_CAPACITY_TIME_BUDGET_MAX_SECONDS = 600
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
OKX_TICKER_URL = "https://www.okx.com/api/v5/market/ticker"
COINBASE_TICKER_URL_TEMPLATE = "https://api.exchange.coinbase.com/products/{product_id}/ticker"
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
GEMINI_TICKER_URL_TEMPLATE = "https://api.gemini.com/v2/ticker/{symbol}"
BITSTAMP_TICKER_URL_TEMPLATE = "https://www.bitstamp.net/api/v2/ticker/{pair}/"
COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
BINANCE_DEPTH_URL = "https://api.binance.com/api/v3/depth"
OKX_BOOKS_URL = "https://www.okx.com/api/v5/market/books"
COINBASE_BOOK_URL_TEMPLATE = "https://api.exchange.coinbase.com/products/{product_id}/book"
KRAKEN_DEPTH_URL = "https://api.kraken.com/0/public/Depth"
GEMINI_BOOK_URL_TEMPLATE = "https://api.gemini.com/v1/book/{symbol}"
BITSTAMP_ORDER_BOOK_URL_TEMPLATE = "https://www.bitstamp.net/api/v2/order_book/{pair}/"
FUSED_PRICE_SOURCE = "fused_weighted"
PRICE_FUSION_MODES = {"auto_depth", "manual_weights"}
DEFAULT_PRICE_FUSION_MANUAL_WEIGHTS = {
    "binance_public_api": 40.0,
    "okx_public_api": 25.0,
    "coinbase_exchange": 15.0,
    "kraken_public_api": 10.0,
    "bitstamp_public_api": 8.0,
    "gemini_public_api": 2.0,
}
DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT = 1.0
DEFAULT_PRICE_FUSION_DEPTH_LEVELS = 100
MAX_PRICE_FUSION_DEPTH_LEVELS = 1000
DEFAULT_PRICE_FUSION_MIN_ORDERBOOK_COVERAGE_PERCENT = 0.5
DEFAULT_PRICE_FUSION_MAX_SINGLE_PROVIDER_WEIGHT_PERCENT = 40.0
DEFAULT_PRICE_FUSION_MAX_PROVIDER_AGE_SECONDS = 15
DEFAULT_PRICE_FUSION_MAX_PROVIDER_LATENCY_MS = 2500
DEFAULT_PRICE_FUSION_MAX_MIDPOINT_DEVIATION_PERCENT = 0.50
DEFAULT_PRICE_FUSION_MIN_SIDE_BALANCE_RATIO = 0.10
DEFAULT_PRICE_STREAM_WS_STALE_SECONDS = 10
DEFAULT_BORROW_APR_BTC_ETH_PERCENT = 8.0
DEFAULT_BORROW_APR_USDT_POINTS_PERCENT = 10.0
DEFAULT_BORROW_INTEREST_INTERVAL_HOURS = 1
DEFAULT_BORROW_INTEREST_MINIMUM_HOURS = 1
LIVE_PRICE_SOURCE_NAMES = {
    FUSED_PRICE_SOURCE,
    "binance_public_api",
    "okx_public_api",
    "coinbase_exchange",
    "kraken_public_api",
    "gemini_public_api",
    "bitstamp_public_api",
    "coingecko_simple_price",
    "test_live_price_provider",
}


def _warning_language(value):
    text = str(value or "").strip().lower()
    return "en" if text.startswith("en") else "zh-TW"


def _now():
    return datetime.now().isoformat()


def _json_dumps(value):
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value, default=None):
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except Exception:
        return default if default is not None else {}


def _client_idempotency_key(value, *, prefix):
    raw = str(value or "").strip()
    if not raw:
        return ""
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _normalize_price_fusion_manual_weights(raw):
    out = {}
    source = raw if isinstance(raw, dict) else {}
    for provider in WEIGHTED_PRICE_PROVIDERS:
        value = source.get(provider, DEFAULT_PRICE_FUSION_MANUAL_WEIGHTS.get(provider, 1.0))
        try:
            number = float(value)
        except Exception:
            number = DEFAULT_PRICE_FUSION_MANUAL_WEIGHTS.get(provider, 1.0)
        if not math.isfinite(number):
            number = DEFAULT_PRICE_FUSION_MANUAL_WEIGHTS.get(provider, 1.0)
        out[provider] = max(0.0, min(number, 1000.0))
    return out


def _median_float(values):
    numbers = sorted(float(value) for value in (values or []))
    if not numbers:
        return 0.0
    middle = len(numbers) // 2
    if len(numbers) % 2:
        return numbers[middle]
    return (numbers[middle - 1] + numbers[middle]) / 2.0


def _bot_max_runs_from_storage(value):
    number = int(value or 0)
    return -1 if number >= UNLIMITED_BOT_MAX_RUNS else number


def _bot_max_runs_to_storage(value, *, allow_unlimited=False, maximum=1000):
    raw = str(value).strip() if value is not None else ""
    if allow_unlimited and raw == "-1":
        return UNLIMITED_BOT_MAX_RUNS
    return _to_int(value, name="max_runs", minimum=1, maximum=maximum)


def _bot_max_runs_has_remaining(run_count, max_runs):
    max_runs = int(max_runs or 0)
    if max_runs >= UNLIMITED_BOT_MAX_RUNS:
        return True
    return int(run_count or 0) < max_runs


def _borrow_apr_group_for_asset(asset_symbol):
    asset = str(asset_symbol or "").strip().upper()
    if asset in {"BTC", "ETH"}:
        return "btc_eth"
    return "usdt_points"


def _condition_label(cond):
    return condition_label(cond)


def _registry_display_quote_currency(definition):
    return str(definition.get("display_quote_currency") or definition.get("quote_currency") or "").strip().upper()


def _registry_display_name(definition):
    base = str(definition.get("base_asset") or "").strip().upper()
    quote = _registry_display_quote_currency(definition)
    return f"{base}/{quote}" if base and quote else str(definition.get("symbol") or "").strip().upper()


def _registry_default_market_payload(definition):
    return {
        "symbol": str(definition.get("symbol") or "").strip().upper(),
        "base_asset": str(definition.get("base_asset") or "").strip().upper(),
        "quote_asset": str(definition.get("quote_currency") or "POINTS").strip().upper() or "POINTS",
        "display_name": _registry_display_name(definition),
        "display_quote_currency": _registry_display_quote_currency(definition),
        "market_type": "spot",
        "enabled": 1,
        "allow_spot": 1,
        "allow_margin": 1,
        "allow_bots": 1,
        "allow_risk_grade_usage": 1,
        "price_precision": 8,
        "quantity_precision": 8,
        "min_order_size": 0.00000001,
        "max_order_size": 1000000.0,
        "lot_size": 0.00000001,
        "tick_size": 0.00000001,
        "sort_order": int(definition.get("sort_order") or 9999),
        "default_manual_price_points": float(definition.get("default_manual_price_points") or 1.0),
        "live_price_enabled": 1 if definition.get("live_price_enabled") else 0,
        "reference_price_enabled": 1 if definition.get("reference_price_enabled") else 0,
        "btc_trade_enabled": 1 if definition.get("btc_trade_enabled") else 0,
        "registry_source": "catalog_seed",
        "seed_version": int(TRADING_MARKET_CATALOG_SEED_VERSION),
    }


def _provider_mapping_capabilities(provider):
    return provider_mapping_capabilities(
        provider,
        ticker_capable_providers=TICKER_CAPABLE_PROVIDERS,
        depth_capable_providers=DEPTH_CAPABLE_PROVIDERS,
        reference_price_capable_providers=REFERENCE_PRICE_CAPABLE_PROVIDERS,
    )


def _market_seed_compare_value(value):
    return market_seed_compare_value(value)


def _registry_seed_status(registry_row, mappings):
    return registry_seed_status(
        registry_row,
        mappings,
        registry_default_market_payload=_registry_default_market_payload,
        provider_mapping_capabilities_func=_provider_mapping_capabilities,
    )


def _seed_market_registry_from_catalog(conn):
    now = _now()
    for definition in list_market_definitions():
        payload = _registry_default_market_payload(definition)
        conn.execute(
            """
            INSERT OR IGNORE INTO trading_markets_registry (
                symbol, base_asset, quote_asset, display_name, display_quote_currency,
                market_type, enabled, allow_spot, allow_margin, allow_bots,
                allow_risk_grade_usage, price_precision, quantity_precision,
                min_order_size, max_order_size, lot_size, tick_size, sort_order,
                default_manual_price_points, live_price_enabled, reference_price_enabled,
                btc_trade_enabled, registry_source, seed_version, probe_status, probe_summary_json,
                created_at, updated_at, created_by, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'seeded', '{}', ?, ?, NULL, NULL)
            """,
            (
                payload["symbol"],
                payload["base_asset"],
                payload["quote_asset"],
                payload["display_name"],
                payload["display_quote_currency"],
                payload["market_type"],
                payload["enabled"],
                payload["allow_spot"],
                payload["allow_margin"],
                payload["allow_bots"],
                payload["allow_risk_grade_usage"],
                payload["price_precision"],
                payload["quantity_precision"],
                payload["min_order_size"],
                payload["max_order_size"],
                payload["lot_size"],
                payload["tick_size"],
                payload["sort_order"],
                payload["default_manual_price_points"],
                payload["live_price_enabled"],
                payload["reference_price_enabled"],
                payload["btc_trade_enabled"],
                payload["registry_source"],
                payload["seed_version"],
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE trading_markets_registry
            SET seed_version=?, updated_at=CASE WHEN registry_source='catalog_seed' AND updated_by IS NULL THEN ? ELSE updated_at END
            WHERE symbol=? AND registry_source='catalog_seed'
            """,
            (int(TRADING_MARKET_CATALOG_SEED_VERSION), now, payload["symbol"]),
        )
        registry = conn.execute(
            "SELECT id FROM trading_markets_registry WHERE symbol=?",
            (payload["symbol"],),
        ).fetchone()
        if not registry:
            continue
        provider_ids = dict(definition.get("provider_ids") or {})
        priority = 1
        for provider, provider_symbol in provider_ids.items():
            capabilities = _provider_mapping_capabilities(provider)
            conn.execute(
                """
                INSERT OR IGNORE INTO trading_market_provider_mappings (
                    market_id, provider, provider_symbol,
                    supports_ticker, supports_depth, supports_candles,
                    enabled, priority, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(registry["id"]),
                    provider,
                    str(provider_symbol or "").strip(),
                    capabilities["supports_ticker"],
                    capabilities["supports_depth"],
                    capabilities["supports_candles"],
                    1 if str(provider_symbol or "").strip() else 0,
                    priority,
                    now,
                    now,
                ),
            )
            priority += 1


def _sync_registry_markets_to_runtime(conn):
    runtime_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_markets)").fetchall()}
    runtime_has_provider_ids = "provider_ids_json" in runtime_cols
    rows = conn.execute(
        """
        SELECT *
        FROM trading_markets_registry
        ORDER BY sort_order ASC, symbol ASC
        """
    ).fetchall()
    now = _now()
    for registry in rows:
        mappings = conn.execute(
            """
            SELECT *
            FROM trading_market_provider_mappings
            WHERE market_id=?
            ORDER BY enabled DESC, priority ASC, id ASC
            """,
            (int(registry["id"]),),
        ).fetchall()
        provider_ids = {
            str(row["provider"] or "").strip(): str(row["provider_symbol"] or "").strip()
            for row in mappings
            if int(row["enabled"] or 0) and str(row["provider_symbol"] or "").strip()
        }
        live_supported = bool(registry["live_price_enabled"]) and any(
            int(row["enabled"] or 0) and int(row["supports_ticker"] or 0) and str(row["provider_symbol"] or "").strip()
            for row in mappings
        )
        reference_supported = bool(registry["reference_price_enabled"]) and any(
            int(row["enabled"] or 0) and int(row["supports_candles"] or 0) and str(row["provider_symbol"] or "").strip()
            for row in mappings
        )
        existing = conn.execute(
            "SELECT * FROM trading_markets WHERE symbol=?",
            (str(registry["symbol"] or "").strip().upper(),),
        ).fetchone()
        if existing:
            assignments = [
                "base_asset=?",
                "quote_currency=?",
                "enabled=?",
                "spot_enabled=?",
                "display_quote_currency=?",
                "display_name=?",
                "market_type=?",
                "sort_order=?",
                "allow_margin=?",
                "allow_bots=?",
                "allow_risk_grade_usage=?",
                "price_precision=?",
                "quantity_precision=?",
                "min_order_size=?",
                "max_order_size=?",
                "lot_size=?",
                "tick_size=?",
                "live_price_enabled=?",
                "reference_price_enabled=?",
                "btc_trade_enabled=?",
                "updated_at=?",
                "updated_by=NULL",
            ]
            values = [
                registry["base_asset"],
                registry["quote_asset"],
                int(registry["enabled"] or 0),
                int(registry["allow_spot"] or 0),
                registry["display_quote_currency"],
                registry["display_name"],
                registry["market_type"],
                int(registry["sort_order"] or 9999),
                int(registry["allow_margin"] or 0),
                int(registry["allow_bots"] or 0),
                int(registry["allow_risk_grade_usage"] or 0),
                int(registry["price_precision"] or 8),
                int(registry["quantity_precision"] or 8),
                float(registry["min_order_size"] or 0.00000001),
                float(registry["max_order_size"] or 1000000.0),
                float(registry["lot_size"] or 0.00000001),
                float(registry["tick_size"] or 0.00000001),
                1 if live_supported else 0,
                1 if reference_supported else 0,
                int(registry["btc_trade_enabled"] or 0),
                now,
            ]
            if runtime_has_provider_ids:
                assignments.insert(-2, "provider_ids_json=?")
                values.insert(-1, _json_dumps(provider_ids))
            conn.execute(
                f"UPDATE trading_markets SET {', '.join(assignments)} WHERE symbol=?",
                [*values, registry["symbol"]],
            )
        else:
            columns = [
                "symbol",
                "base_asset",
                "quote_currency",
                "enabled",
                "spot_enabled",
                "manual_price_points",
                "fee_rate_percent",
                "updated_at",
                "price_source",
                "display_quote_currency",
                "display_name",
                "market_type",
                "sort_order",
                "allow_margin",
                "allow_bots",
                "allow_risk_grade_usage",
                "price_precision",
                "quantity_precision",
                "min_order_size",
                "max_order_size",
                "lot_size",
                "tick_size",
                "live_price_enabled",
                "reference_price_enabled",
                "btc_trade_enabled",
            ]
            values = [
                registry["symbol"],
                registry["base_asset"],
                registry["quote_asset"],
                int(registry["enabled"] or 0),
                int(registry["allow_spot"] or 0),
                registry["default_manual_price_points"] or 1,
                DEFAULT_SPOT_FEE_RATE_PERCENT,
                now,
                DEFAULT_TRADING_PRICE_SOURCE,
                registry["display_quote_currency"],
                registry["display_name"],
                registry["market_type"],
                int(registry["sort_order"] or 9999),
                int(registry["allow_margin"] or 0),
                int(registry["allow_bots"] or 0),
                int(registry["allow_risk_grade_usage"] or 0),
                int(registry["price_precision"] or 8),
                int(registry["quantity_precision"] or 8),
                float(registry["min_order_size"] or 0.00000001),
                float(registry["max_order_size"] or 1000000.0),
                float(registry["lot_size"] or 0.00000001),
                float(registry["tick_size"] or 0.00000001),
                1 if live_supported else 0,
                1 if reference_supported else 0,
                int(registry["btc_trade_enabled"] or 0),
            ]
            if runtime_has_provider_ids:
                columns.append("provider_ids_json")
                values.append(_json_dumps(provider_ids))
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO trading_markets ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )


def _ensure_trading_hot_path_indexes(conn):
    """Install low-risk read indexes for the finance 5K hot-path baseline."""
    for ddl in (
        """
        CREATE INDEX IF NOT EXISTS idx_trading_orders_user_id_desc
        ON trading_orders(user_id, id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_trading_fills_user_id_desc
        ON trading_fills(user_id, id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_trading_margin_user_id_desc
        ON trading_margin_positions(user_id, id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_trading_bots_user_id_desc
        ON trading_bots(user_id, id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_trading_bot_runs_user_id_desc
        ON trading_bot_runs(user_id, id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_trading_grid_orders_user_order
        ON trading_grid_orders(user_id, trading_order_uuid)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_trading_market_price_snapshots_health
        ON trading_market_price_snapshots(price_health, confidence, updated_at)
        """,
    ):
        conn.execute(ddl)


def ensure_trading_schema(conn):
    # Slice 4b: pure CREATE TABLE DDL strings live in schema_ddl.py so
    # this function shrinks from 740 lines to ~280. Imperative migrations
    # (PRAGMA-guarded ALTER TABLE, legacy unit renames, default settings
    # INSERT OR IGNORE, registry catalog seed) stay below because they
    # need shared `now` + helpers.
    from services.trading.schema_ddl import ALL_TABLE_DDL

    for ddl in ALL_TABLE_DDL:
        conn.execute(ddl)
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO trading_reserve_pool (id, balance_points, updated_at) VALUES (1, 0, ?)",
        (now,),
    )
    initial_event = conn.execute(
        "SELECT 1 FROM trading_reserve_pool_events WHERE event_type='initial_funding' LIMIT 1"
    ).fetchone()
    if not initial_event:
        reserve = conn.execute("SELECT * FROM trading_reserve_pool WHERE id=1").fetchone()
        balance = int(reserve["balance_points"] or 0) if reserve else 0
        next_balance = balance + TRADING_FUNDING_POOL_INITIAL_POINTS
        conn.execute(
            "UPDATE trading_reserve_pool SET balance_points=?, updated_at=?, updated_by=NULL WHERE id=1",
            (next_balance, now),
        )
        conn.execute(
            """
            INSERT INTO trading_reserve_pool_events (
                event_uuid, delta_points, balance_after, event_type, reason,
                actor_user_id, source_user_id, order_id, fill_id, points_ledger_uuid, created_at
            ) VALUES (?, ?, ?, 'initial_funding', 'TRADING_FUNDING_POOL_INITIAL', NULL, NULL, NULL, NULL, NULL, ?)
            """,
            (str(uuid.uuid4()), TRADING_FUNDING_POOL_INITIAL_POINTS, next_balance, now),
        )
    else:
        alignment_event = conn.execute(
            "SELECT 1 FROM trading_reserve_pool_events WHERE event_type='walletized_exchange_fund_alignment' LIMIT 1"
        ).fetchone()
        initial_row = conn.execute(
            """
            SELECT delta_points FROM trading_reserve_pool_events
            WHERE event_type='initial_funding'
            ORDER BY id ASC LIMIT 1
            """
        ).fetchone()
        initial_points = int(initial_row["delta_points"] or 0) if initial_row else TRADING_FUNDING_POOL_LEGACY_INITIAL_POINTS
        if not alignment_event and initial_points < TRADING_FUNDING_POOL_INITIAL_POINTS:
            reserve = conn.execute("SELECT * FROM trading_reserve_pool WHERE id=1").fetchone()
            balance = int(reserve["balance_points"] or 0) if reserve else 0
            delta = TRADING_FUNDING_POOL_INITIAL_POINTS - initial_points
            next_balance = balance + delta
            conn.execute(
                "UPDATE trading_reserve_pool SET balance_points=?, updated_at=?, updated_by=NULL WHERE id=1",
                (next_balance, now),
            )
            conn.execute(
                """
                INSERT INTO trading_reserve_pool_events (
                    event_uuid, delta_points, balance_after, event_type, reason,
                    actor_user_id, source_user_id, order_id, fill_id, points_ledger_uuid, created_at
                ) VALUES (?, ?, ?, 'walletized_exchange_fund_alignment', 'POINTSCHAIN_EXCHANGE_FUND_ALIGNMENT', NULL, NULL, NULL, NULL, NULL, ?)
                """,
                (str(uuid.uuid4()), delta, next_balance, now),
            )
    conn.execute(
        "INSERT OR IGNORE INTO trading_state (id, safe_mode, reason, verification_json, updated_at) VALUES (1, 0, '', '{}', ?)",
        (now,),
    )
    legacy_unit = "b" + "ps"
    market_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_markets)").fetchall()}
    for column_name, ddl in (
        ("display_quote_currency", "ALTER TABLE trading_markets ADD COLUMN display_quote_currency TEXT NOT NULL DEFAULT 'USDT'"),
        ("display_name", "ALTER TABLE trading_markets ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"),
        ("market_type", "ALTER TABLE trading_markets ADD COLUMN market_type TEXT NOT NULL DEFAULT 'spot'"),
        ("sort_order", "ALTER TABLE trading_markets ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 9999"),
        ("allow_margin", "ALTER TABLE trading_markets ADD COLUMN allow_margin INTEGER NOT NULL DEFAULT 1"),
        ("allow_bots", "ALTER TABLE trading_markets ADD COLUMN allow_bots INTEGER NOT NULL DEFAULT 1"),
        ("allow_risk_grade_usage", "ALTER TABLE trading_markets ADD COLUMN allow_risk_grade_usage INTEGER NOT NULL DEFAULT 1"),
        ("price_precision", "ALTER TABLE trading_markets ADD COLUMN price_precision INTEGER NOT NULL DEFAULT 8"),
        ("quantity_precision", "ALTER TABLE trading_markets ADD COLUMN quantity_precision INTEGER NOT NULL DEFAULT 8"),
        ("min_order_size", "ALTER TABLE trading_markets ADD COLUMN min_order_size REAL NOT NULL DEFAULT 0.00000001"),
        ("max_order_size", "ALTER TABLE trading_markets ADD COLUMN max_order_size REAL NOT NULL DEFAULT 1000000"),
        ("lot_size", "ALTER TABLE trading_markets ADD COLUMN lot_size REAL NOT NULL DEFAULT 0.00000001"),
        ("tick_size", "ALTER TABLE trading_markets ADD COLUMN tick_size REAL NOT NULL DEFAULT 0.00000001"),
        ("live_price_enabled", "ALTER TABLE trading_markets ADD COLUMN live_price_enabled INTEGER NOT NULL DEFAULT 1"),
        ("reference_price_enabled", "ALTER TABLE trading_markets ADD COLUMN reference_price_enabled INTEGER NOT NULL DEFAULT 1"),
        ("btc_trade_enabled", "ALTER TABLE trading_markets ADD COLUMN btc_trade_enabled INTEGER NOT NULL DEFAULT 0"),
        ("provider_ids_json", "ALTER TABLE trading_markets ADD COLUMN provider_ids_json TEXT NOT NULL DEFAULT '{}'"),
        # Boot-ready gate (2026-05-06, warmup tightened 2026-05-07):
        # NULL until at least two consecutive live quotes have produced a
        # stable candidate for this market. Trading / liquidation / bot ops
        # refuse to act on markets where this is still NULL — protects
        # against both the seed default and the very first live quote after a
        # fresh boot or provider recovery.
        ("live_price_warmup_started_at", "ALTER TABLE trading_markets ADD COLUMN live_price_warmup_started_at TEXT"),
        ("live_price_confirmed_at", "ALTER TABLE trading_markets ADD COLUMN live_price_confirmed_at TEXT"),
    ):
        if column_name not in market_cols:
            conn.execute(ddl)
    market_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_markets)").fetchall()}
    legacy_fee_col = f"fee_{legacy_unit}"
    legacy_jump_col = f"max_price_jump_{legacy_unit}"
    if "fee_rate_percent" not in market_cols:
        conn.execute("ALTER TABLE trading_markets ADD COLUMN fee_rate_percent REAL NOT NULL DEFAULT 0.1")
        if legacy_fee_col in market_cols:
            conn.execute(f"UPDATE trading_markets SET fee_rate_percent=CAST({legacy_fee_col} AS REAL) / 100.0")
    conn.execute(
        """
        UPDATE trading_markets
        SET fee_rate_percent=?, updated_at=?
        WHERE ABS(COALESCE(fee_rate_percent, 0) - 0.3) < 0.0000001
          AND updated_by IS NULL
        """,
        (DEFAULT_SPOT_FEE_RATE_PERCENT, now),
    )
    if "max_price_jump_percent" not in market_cols:
        conn.execute("ALTER TABLE trading_markets ADD COLUMN max_price_jump_percent REAL NOT NULL DEFAULT 10")
        if legacy_jump_col in market_cols:
            conn.execute(f"UPDATE trading_markets SET max_price_jump_percent=CAST({legacy_jump_col} AS REAL) / 100.0")
    registry_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_markets_registry)").fetchall()}
    if "registry_source" not in registry_cols:
        conn.execute("ALTER TABLE trading_markets_registry ADD COLUMN registry_source TEXT NOT NULL DEFAULT 'catalog_seed'")
    if "seed_version" not in registry_cols:
        conn.execute("ALTER TABLE trading_markets_registry ADD COLUMN seed_version INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        UPDATE trading_markets_registry
        SET registry_source='catalog_seed'
        WHERE registry_source IS NULL OR TRIM(registry_source)=''
        """
    )
    margin_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_margin_positions)").fetchall()}
    legacy_interest_col = f"interest_{legacy_unit}_daily"
    if "interest_percent_daily" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN interest_percent_daily REAL NOT NULL DEFAULT 0")
        if legacy_interest_col in margin_cols:
            conn.execute(f"UPDATE trading_margin_positions SET interest_percent_daily=CAST({legacy_interest_col} AS REAL) / 100.0")
    defaults = [
        ("trading.enabled", "true"),
        ("trading.futures_enabled", "false"),
        ("trading.pvp_matching_enabled", "false"),
        ("trading.borrowing_enabled", "true"),
        ("trading.borrow_interest_percent_daily", str(_daily_percent_from_apr(DEFAULT_BORROW_APR_USDT_POINTS_PERCENT))),
        ("trading.borrow_apr_btc_eth_percent", str(DEFAULT_BORROW_APR_BTC_ETH_PERCENT)),
        ("trading.borrow_apr_usdt_points_percent", str(DEFAULT_BORROW_APR_USDT_POINTS_PERCENT)),
        ("trading.borrow_interest_pool_pressure_multiplier", str(TRADING_FUNDING_POOL_PRESSURE_MULTIPLIER)),
        ("trading.borrow_interest_interval_hours", str(DEFAULT_BORROW_INTEREST_INTERVAL_HOURS)),
        ("trading.borrow_interest_minimum_hours", str(DEFAULT_BORROW_INTEREST_MINIMUM_HOURS)),
        ("trading.margin_max_pool_utilization_percent", str(MARGIN_MAX_POOL_UTILIZATION_PERCENT)),
        ("trading.exchange_liability_limit_points", str(EXCHANGE_LIABILITY_LIMIT_POINTS)),
        ("trading.exchange_liability_grace_minutes", str(EXCHANGE_LIABILITY_GRACE_MINUTES)),
        ("trading.profit_settlement_interval_minutes", str(PROFIT_SETTLEMENT_INTERVAL_MINUTES)),
        ("trading.margin_long_financing_percent", str(MARGIN_LONG_FINANCING_RATE_PERCENT)),
        ("trading.short_collateral_percent", str(SHORT_COLLATERAL_RATE_PERCENT)),
        ("trading.margin_liquidation_enabled", "true"),
        ("trading.margin_maintenance_percent", "15"),
        ("trading.grid_fee_discount_percent", str(DEFAULT_GRID_FEE_DISCOUNT_PERCENT)),
        ("trading.max_price_staleness_seconds", "900"),
        ("trading.price_source", DEFAULT_TRADING_PRICE_SOURCE),
        ("trading.price_fusion_mode", "auto_depth"),
        ("trading.price_fusion_manual_weights_json", _json_dumps(DEFAULT_PRICE_FUSION_MANUAL_WEIGHTS)),
        ("trading.price_fusion_depth_band_percent", str(DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT)),
        ("trading.price_fusion_depth_levels", str(DEFAULT_PRICE_FUSION_DEPTH_LEVELS)),
        ("trading.price_fusion_min_orderbook_coverage_percent", str(DEFAULT_PRICE_FUSION_MIN_ORDERBOOK_COVERAGE_PERCENT)),
        ("trading.price_fusion_max_single_provider_weight_percent", str(DEFAULT_PRICE_FUSION_MAX_SINGLE_PROVIDER_WEIGHT_PERCENT)),
        ("trading.price_fusion_min_provider_count", str(DEFAULT_PRICE_FUSION_MIN_PROVIDER_COUNT)),
        ("trading.price_fusion_trade_min_provider_count", str(DEFAULT_PRICE_FUSION_TRADE_MIN_PROVIDER_COUNT)),
        ("trading.warning_language", "zh-TW"),
        ("trading.price_degrade_pause_market_orders", "false"),
        ("trading.price_degrade_pause_bots", "false"),
        ("trading.price_degrade_pause_borrowing", "false"),
        ("trading.allow_unready_markets", "true"),
        ("trading.disable_price_confidence_gates", "true"),
        ("trading.dev_allow_conservative_market_orders", "false"),
        ("trading.dev_allow_unready_markets", "false"),
        ("trading.dev_disable_price_confidence_gates", "false"),
        ("trading.simulated_slippage_enabled", "false"),
        ("trading.simulated_slippage_base_basis_points", "0"),
        ("trading.simulated_slippage_size_basis_points_per_10k_notional", "0"),
        ("trading.simulated_slippage_max_basis_points", "0"),
        ("trading.price_stream_ws_enabled", "true"),
        ("trading.price_stream_ws_stale_seconds", str(DEFAULT_PRICE_STREAM_WS_STALE_SECONDS)),
        ("trading.shadow_funding_publish_enabled", "false"),
        ("trading.btc_trade_enabled", "false"),
        ("trading.btc_trade_repo_url", "https://github.com/s9213712/BTC_trade.git"),
        ("trading.btc_trade_branch", "strategy/v15b-plus"),
        ("trading.bot_auto_scan_enabled", "true"),
        ("trading.bot_auto_scan_interval_seconds", "30"),
        ("trading.bot_auto_scan_limit", "50"),
        ("trading.bot_competition_enabled", "true"),
        ("trading.bot_competition_weekly_reward_points", "100"),
    ]
    for key, value in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO trading_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )
    conn.execute(
        """
        UPDATE trading_settings
        SET value=?, updated_at=?
        WHERE key='trading.price_source'
          AND value=?
          AND (updated_by IS NULL OR TRIM(CAST(updated_by AS TEXT))='')
        """,
        (DEFAULT_TRADING_PRICE_SOURCE, now, FUSED_PRICE_SOURCE),
    )
    conn.execute(
        """
        UPDATE trading_settings
        SET value=?, updated_at=?
        WHERE key='trading.price_fusion_min_provider_count'
          AND value='3'
          AND (updated_by IS NULL OR TRIM(CAST(updated_by AS TEXT))='')
        """,
        (str(DEFAULT_PRICE_FUSION_MIN_PROVIDER_COUNT), now),
    )
    conn.execute(
        """
        UPDATE trading_settings
        SET value=?, updated_at=?
        WHERE key='trading.price_fusion_trade_min_provider_count'
          AND value='2'
          AND (updated_by IS NULL OR TRIM(CAST(updated_by AS TEXT))='')
        """,
        (str(DEFAULT_PRICE_FUSION_TRADE_MIN_PROVIDER_COUNT), now),
    )
    _seed_market_registry_from_catalog(conn)
    _sync_registry_markets_to_runtime(conn)
    for table in ("trading_orders", "trading_fills"):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "funding_mode" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN funding_mode TEXT NOT NULL DEFAULT 'points_chain'")
    order_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_orders)").fetchall()}
    if "source_wallet_address" not in order_cols:
        conn.execute("ALTER TABLE trading_orders ADD COLUMN source_wallet_address TEXT NOT NULL DEFAULT ''")
    if "trial_frozen_points" not in order_cols:
        conn.execute("ALTER TABLE trading_orders ADD COLUMN trial_frozen_points INTEGER NOT NULL DEFAULT 0")
    if "chain_frozen_points" not in order_cols:
        conn.execute("ALTER TABLE trading_orders ADD COLUMN chain_frozen_points INTEGER NOT NULL DEFAULT 0")
    if "fee_micropoints" not in order_cols:
        conn.execute("ALTER TABLE trading_orders ADD COLUMN fee_micropoints INTEGER NOT NULL DEFAULT 0")
    if "stop_loss_percent" not in order_cols:
        conn.execute("ALTER TABLE trading_orders ADD COLUMN stop_loss_percent REAL")
    if "take_profit_percent" not in order_cols:
        conn.execute("ALTER TABLE trading_orders ADD COLUMN take_profit_percent REAL")
    position_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_spot_positions)").fetchall()}
    if "fee_carry_micropoints" not in position_cols:
        conn.execute("ALTER TABLE trading_spot_positions ADD COLUMN fee_carry_micropoints INTEGER NOT NULL DEFAULT 0")
    if "stop_loss_percent" not in position_cols:
        conn.execute("ALTER TABLE trading_spot_positions ADD COLUMN stop_loss_percent REAL")
    if "take_profit_percent" not in position_cols:
        conn.execute("ALTER TABLE trading_spot_positions ADD COLUMN take_profit_percent REAL")
    if "source_wallet_address" not in position_cols:
        conn.execute("ALTER TABLE trading_spot_positions ADD COLUMN source_wallet_address TEXT NOT NULL DEFAULT ''")
    if "funding_sources_json" not in position_cols:
        conn.execute("ALTER TABLE trading_spot_positions ADD COLUMN funding_sources_json TEXT NOT NULL DEFAULT '[]'")
    if "taint_status" not in position_cols:
        conn.execute("ALTER TABLE trading_spot_positions ADD COLUMN taint_status TEXT NOT NULL DEFAULT 'normal'")
    if "taint_source_tx_hash" not in position_cols:
        conn.execute("ALTER TABLE trading_spot_positions ADD COLUMN taint_source_tx_hash TEXT NOT NULL DEFAULT ''")
    fill_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_fills)").fetchall()}
    if "fee_micropoints" not in fill_cols:
        conn.execute("ALTER TABLE trading_fills ADD COLUMN fee_micropoints INTEGER NOT NULL DEFAULT 0")
    if "trial_repaid_points" not in fill_cols:
        conn.execute("ALTER TABLE trading_fills ADD COLUMN trial_repaid_points INTEGER NOT NULL DEFAULT 0")
    if "trial_profit_points" not in fill_cols:
        conn.execute("ALTER TABLE trading_fills ADD COLUMN trial_profit_points INTEGER NOT NULL DEFAULT 0")
    pending_profit_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_pending_profit)").fetchall()}
    for column_name, ddl in (
        ("position_uuid", "ALTER TABLE trading_pending_profit ADD COLUMN position_uuid TEXT NOT NULL DEFAULT ''"),
        ("governance_proposal_uuid", "ALTER TABLE trading_pending_profit ADD COLUMN governance_proposal_uuid TEXT NOT NULL DEFAULT ''"),
        ("liability_policy_json", "ALTER TABLE trading_pending_profit ADD COLUMN liability_policy_json TEXT NOT NULL DEFAULT '{}'"),
        ("settle_not_before_at", "ALTER TABLE trading_pending_profit ADD COLUMN settle_not_before_at TEXT"),
    ):
        if column_name not in pending_profit_cols:
            conn.execute(ddl)
    pnl_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_spot_realized_pnl)").fetchall()}
    if "buy_fee_micropoints" not in pnl_cols:
        conn.execute("ALTER TABLE trading_spot_realized_pnl ADD COLUMN buy_fee_micropoints INTEGER NOT NULL DEFAULT 0")
    if "sell_fee_micropoints" not in pnl_cols:
        conn.execute("ALTER TABLE trading_spot_realized_pnl ADD COLUMN sell_fee_micropoints INTEGER NOT NULL DEFAULT 0")
    if "settled_fee_micropoints" not in pnl_cols:
        conn.execute("ALTER TABLE trading_spot_realized_pnl ADD COLUMN settled_fee_micropoints INTEGER NOT NULL DEFAULT 0")
    trial_credit_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_trial_credits)").fetchall()}
    if "reclaim_blocked_reason" not in trial_credit_cols:
        conn.execute("ALTER TABLE trading_trial_credits ADD COLUMN reclaim_blocked_reason TEXT NOT NULL DEFAULT ''")
    if "reclaim_blocked_at" not in trial_credit_cols:
        conn.execute("ALTER TABLE trading_trial_credits ADD COLUMN reclaim_blocked_at TEXT")
    margin_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_margin_positions)").fetchall()}
    if "collateral_trial_points" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN collateral_trial_points INTEGER NOT NULL DEFAULT 0")
    if "collateral_chain_points" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN collateral_chain_points INTEGER NOT NULL DEFAULT 0")
    if "open_fee_trial_points" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN open_fee_trial_points INTEGER NOT NULL DEFAULT 0")
    if "open_fee_chain_points" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN open_fee_chain_points INTEGER NOT NULL DEFAULT 0")
    if "open_fee_micropoints" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN open_fee_micropoints INTEGER NOT NULL DEFAULT 0")
    if "close_fee_micropoints" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN close_fee_micropoints INTEGER NOT NULL DEFAULT 0")
    if "interest_paid_points" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN interest_paid_points INTEGER NOT NULL DEFAULT 0")
    if "interest_accrued_hours" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN interest_accrued_hours INTEGER NOT NULL DEFAULT 0")
    if "interest_carry_micropoints" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN interest_carry_micropoints INTEGER NOT NULL DEFAULT 0")
    if "interest_interval_hours" not in margin_cols:
        conn.execute(
            f"ALTER TABLE trading_margin_positions ADD COLUMN interest_interval_hours INTEGER NOT NULL DEFAULT {DEFAULT_BORROW_INTEREST_INTERVAL_HOURS}"
        )
    if "interest_minimum_hours" not in margin_cols:
        conn.execute(
            f"ALTER TABLE trading_margin_positions ADD COLUMN interest_minimum_hours INTEGER NOT NULL DEFAULT {DEFAULT_BORROW_INTEREST_MINIMUM_HOURS}"
        )
    if "borrowed_asset_symbol" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN borrowed_asset_symbol TEXT NOT NULL DEFAULT 'POINTS'")
    if "exit_price_points" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN exit_price_points INTEGER")
    if "realized_pnl_points" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN realized_pnl_points INTEGER NOT NULL DEFAULT 0")
    if "stop_loss_percent" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN stop_loss_percent REAL")
    if "take_profit_percent" not in margin_cols:
        conn.execute("ALTER TABLE trading_margin_positions ADD COLUMN take_profit_percent REAL")
    bot_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_bots)").fetchall()}
    if "bot_type" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN bot_type TEXT NOT NULL DEFAULT 'conditional'")
    if "interval_hours" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN interval_hours INTEGER NOT NULL DEFAULT 24")
    if "budget_points" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN budget_points INTEGER NOT NULL DEFAULT 0")
    if "price_upper_limit" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN price_upper_limit REAL")
    if "price_lower_limit" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN price_lower_limit REAL")
    # A zero daily cap deliberately means unlimited.  That preserves the
    # behaviour of pre-migration bots while new Workflow bots can opt into
    # the UI's explicit 1..100 UTC-day limit.
    if "max_daily_runs" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN max_daily_runs INTEGER NOT NULL DEFAULT 0")
    if "daily_run_date" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN daily_run_date TEXT")
    if "daily_run_count" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN daily_run_count INTEGER NOT NULL DEFAULT 0")
    if "workflow_json" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN workflow_json TEXT")
    if "execution_state_json" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN execution_state_json TEXT")
    if "enabled_at" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN enabled_at TEXT")
        conn.execute("UPDATE trading_bots SET enabled_at=COALESCE(created_at, updated_at) WHERE enabled=1 AND COALESCE(enabled_at, '')=''")
    if "last_scan_at" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN last_scan_at TEXT")
    if "stop_loss_percent" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN stop_loss_percent REAL")
    if "take_profit_percent" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN take_profit_percent REAL")
    if "share_parameters" not in bot_cols:
        conn.execute("ALTER TABLE trading_bots ADD COLUMN share_parameters INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        UPDATE trading_bots
        SET stop_loss_percent=NULL, take_profit_percent=NULL
        WHERE COALESCE(bot_type, 'conditional')!='dca'
          AND (stop_loss_percent IS NOT NULL OR take_profit_percent IS NOT NULL)
        """
    )
    grid_bot_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_grid_bots)").fetchall()}
    if "enabled_at" not in grid_bot_cols:
        conn.execute("ALTER TABLE trading_grid_bots ADD COLUMN enabled_at TEXT")
        conn.execute("UPDATE trading_grid_bots SET enabled_at=COALESCE(created_at, updated_at) WHERE enabled=1 AND COALESCE(enabled_at, '')=''")
    if "share_parameters" not in grid_bot_cols:
        conn.execute("ALTER TABLE trading_grid_bots ADD COLUMN share_parameters INTEGER NOT NULL DEFAULT 0")
    if "stop_loss_percent" not in grid_bot_cols:
        conn.execute("ALTER TABLE trading_grid_bots ADD COLUMN stop_loss_percent REAL")
    if "take_profit_percent" not in grid_bot_cols:
        conn.execute("ALTER TABLE trading_grid_bots ADD COLUMN take_profit_percent REAL")
    _ensure_trading_hot_path_indexes(conn)


def _connection_path(conn):
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row["file"] if hasattr(row, "keys") else row[2])
    except Exception:
        return ""


class TradingEngineService:
    _schema_lock = threading.Lock()
    _schema_ready_paths = set()

    MAX_BACKTEST_CANDLES = MAX_BACKTEST_CANDLES
    BACKTEST_MAX_CANDLES_FLOOR = BACKTEST_MAX_CANDLES_FLOOR
    BACKTEST_MAX_CANDLES_CEILING = BACKTEST_MAX_CANDLES_CEILING
    BACKTEST_CAPACITY_TIME_BUDGET_DEFAULT_SECONDS = BACKTEST_CAPACITY_TIME_BUDGET_DEFAULT_SECONDS
    BACKTEST_CAPACITY_TIME_BUDGET_MIN_SECONDS = BACKTEST_CAPACITY_TIME_BUDGET_MIN_SECONDS
    BACKTEST_CAPACITY_TIME_BUDGET_MAX_SECONDS = BACKTEST_CAPACITY_TIME_BUDGET_MAX_SECONDS
    DEFAULT_BORROW_APR_BTC_ETH_PERCENT = DEFAULT_BORROW_APR_BTC_ETH_PERCENT
    DEFAULT_BORROW_APR_USDT_POINTS_PERCENT = DEFAULT_BORROW_APR_USDT_POINTS_PERCENT
    DEFAULT_BORROW_INTEREST_INTERVAL_HOURS = DEFAULT_BORROW_INTEREST_INTERVAL_HOURS
    DEFAULT_BORROW_INTEREST_MINIMUM_HOURS = DEFAULT_BORROW_INTEREST_MINIMUM_HOURS
    TRADING_FUNDING_POOL_PRESSURE_MULTIPLIER = TRADING_FUNDING_POOL_PRESSURE_MULTIPLIER
    MARGIN_MAX_POOL_UTILIZATION_PERCENT = MARGIN_MAX_POOL_UTILIZATION_PERCENT
    MARGIN_LONG_FINANCING_RATE_PERCENT = MARGIN_LONG_FINANCING_RATE_PERCENT
    SHORT_COLLATERAL_RATE_PERCENT = SHORT_COLLATERAL_RATE_PERCENT
    EXCHANGE_LIABILITY_LIMIT_POINTS = EXCHANGE_LIABILITY_LIMIT_POINTS
    EXCHANGE_LIABILITY_GRACE_MINUTES = EXCHANGE_LIABILITY_GRACE_MINUTES
    PROFIT_SETTLEMENT_INTERVAL_MINUTES = PROFIT_SETTLEMENT_INTERVAL_MINUTES
    FUSED_PRICE_SOURCE = FUSED_PRICE_SOURCE
    PRICE_FUSION_MODES = PRICE_FUSION_MODES
    DEFAULT_PRICE_FUSION_MANUAL_WEIGHTS = DEFAULT_PRICE_FUSION_MANUAL_WEIGHTS
    DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT = DEFAULT_PRICE_FUSION_DEPTH_BAND_PERCENT
    DEFAULT_PRICE_FUSION_DEPTH_LEVELS = DEFAULT_PRICE_FUSION_DEPTH_LEVELS
    MAX_PRICE_FUSION_DEPTH_LEVELS = MAX_PRICE_FUSION_DEPTH_LEVELS
    DEFAULT_PRICE_FUSION_MIN_ORDERBOOK_COVERAGE_PERCENT = DEFAULT_PRICE_FUSION_MIN_ORDERBOOK_COVERAGE_PERCENT
    DEFAULT_PRICE_FUSION_MAX_SINGLE_PROVIDER_WEIGHT_PERCENT = DEFAULT_PRICE_FUSION_MAX_SINGLE_PROVIDER_WEIGHT_PERCENT
    DEFAULT_PRICE_FUSION_MAX_PROVIDER_AGE_SECONDS = DEFAULT_PRICE_FUSION_MAX_PROVIDER_AGE_SECONDS
    DEFAULT_PRICE_FUSION_MAX_PROVIDER_LATENCY_MS = DEFAULT_PRICE_FUSION_MAX_PROVIDER_LATENCY_MS
    DEFAULT_PRICE_FUSION_MAX_MIDPOINT_DEVIATION_PERCENT = DEFAULT_PRICE_FUSION_MAX_MIDPOINT_DEVIATION_PERCENT
    DEFAULT_PRICE_FUSION_MIN_SIDE_BALANCE_RATIO = DEFAULT_PRICE_FUSION_MIN_SIDE_BALANCE_RATIO
    DEFAULT_PRICE_STREAM_WS_STALE_SECONDS = DEFAULT_PRICE_STREAM_WS_STALE_SECONDS
    DEFAULT_TRADING_PRICE_SOURCE = DEFAULT_TRADING_PRICE_SOURCE
    DEFAULT_PRICE_FUSION_TRADE_MIN_PROVIDER_COUNT = DEFAULT_PRICE_FUSION_TRADE_MIN_PROVIDER_COUNT
    TRADING_BOT_AUDIT_INTERVAL_SECONDS = TRADING_BOT_AUDIT_INTERVAL_SECONDS
    TRADING_BOT_AUDIT_LIMIT = TRADING_BOT_AUDIT_LIMIT
    TRADING_BOT_AUDIT_MIN_ENABLED_SECONDS = TRADING_BOT_AUDIT_MIN_ENABLED_SECONDS
    ROOT_SIMULATED_INITIAL_POINTS = ROOT_SIMULATED_INITIAL_POINTS
    TRIAL_CREDIT_INITIAL_POINTS = TRIAL_CREDIT_INITIAL_POINTS
    TRIAL_CREDIT_DAYS = TRIAL_CREDIT_DAYS

    def __init__(self, *, get_db, points_service, audit=None, live_price_provider=None, historical_candles_provider=None, stream_hub=None):
        self.get_db = get_db
        self.points_service = points_service
        self.audit = audit or (lambda *args, **kwargs: None)
        self.live_price_provider = live_price_provider
        self.historical_candles_provider = historical_candles_provider
        self.stream_hub = stream_hub
        self._matching_orderbooks = {}
        self._funding_channels = {}
        self._live_quote_cache = {}
        self._live_quote_cache_lock = threading.RLock()

    def ensure_schema(self, conn):
        db_path = _connection_path(conn)
        if db_path and db_path in self._schema_ready_paths:
            return
        with self._schema_lock:
            if db_path and db_path in self._schema_ready_paths:
                return
            self.points_service.ensure_schema(conn)
            ensure_trading_schema(conn)
            self._align_reserve_pool_to_exchange_fund(conn)
            if db_path:
                self._schema_ready_paths.add(db_path)

    def _actor_id(self, actor):
        try:
            return int(actor.get("id") if hasattr(actor, "get") else actor["id"])
        except Exception:
            return None

    def _exchange_fund_chain_snapshot(self, conn):
        report_conn = conn
        close_report_conn = False
        points_get_db = getattr(self.points_service, "get_db", None)
        if points_get_db and points_get_db is not self.get_db:
            candidate = None
            try:
                candidate = points_get_db()
                if _connection_path(candidate) != _connection_path(conn):
                    self.points_service.ensure_schema(candidate)
                    report_conn = candidate
                    close_report_conn = True
                else:
                    candidate.close()
            except Exception:
                if candidate is not None:
                    try:
                        candidate.close()
                    except Exception:
                        pass
                report_conn = conn
                close_report_conn = False
        try:
            report = economy_layer_report(
                report_conn,
                chain_secret=self.points_service.chain_secret,
                actor={"role": "system", "id": None},
            )
            supply = report.get("supply") or {}
            fund = (report.get("funds") or {}).get("exchange_fund") or {}
            return {
                "balance_points": int(fund.get("balance") or 0),
                "address": fund.get("address") or "",
                "exchange_total_assets_points": int(supply.get("exchange_total_assets") or fund.get("balance") or 0),
                "exchange_receivable_principal_points": int(supply.get("exchange_receivable_principal") or 0),
                "replay_height": int((report.get("replay") or {}).get("height") or 0),
                "wallet_root_hash": (report.get("replay") or {}).get("wallet_root_hash") or "",
                "source_db_path": _connection_path(report_conn),
            }
        finally:
            if close_report_conn:
                report_conn.close()

    def _economy_fund_balance(self, conn, fund_key):
        report = economy_layer_report(
            conn,
            chain_secret=self.points_service.chain_secret,
            actor={"role": "system", "id": None},
        )
        fund = (report.get("funds") or {}).get(str(fund_key or "").strip().lower()) or {}
        return int(fund.get("balance") or 0)

    def _align_reserve_pool_to_exchange_fund(self, conn):
        if not table_columns(conn, "trading_reserve_pool") or not table_columns(conn, "trading_reserve_pool_events"):
            return None
        snapshot = self._exchange_fund_chain_snapshot(conn)
        exchange_balance = int(snapshot.get("balance_points") or 0)
        row = conn.execute("SELECT * FROM trading_reserve_pool WHERE id=1").fetchone()
        reserve_balance = int(row["balance_points"] or 0) if row else 0
        delta = exchange_balance - reserve_balance
        if delta == 0:
            return snapshot
        event_uuid = f"pointschain_exchange_fund_alignment:{snapshot.get('wallet_root_hash') or exchange_balance}"
        existing = conn.execute(
            "SELECT * FROM trading_reserve_pool_events WHERE event_uuid=?",
            (event_uuid,),
        ).fetchone()
        if existing:
            event_uuid = f"{event_uuid}:repair:{reserve_balance}:{exchange_balance}"
            if conn.execute(
                "SELECT * FROM trading_reserve_pool_events WHERE event_uuid=?",
                (event_uuid,),
            ).fetchone():
                return snapshot
        now = _now()
        if row:
            conn.execute(
                "UPDATE trading_reserve_pool SET balance_points=?, updated_at=?, updated_by=NULL WHERE id=1",
                (exchange_balance, now),
            )
        else:
            conn.execute(
                "INSERT INTO trading_reserve_pool (id, balance_points, updated_at, updated_by) VALUES (1, ?, ?, NULL)",
                (exchange_balance, now),
            )
        conn.execute(
            """
            INSERT INTO trading_reserve_pool_events (
                event_uuid, delta_points, balance_after, event_type, reason,
                actor_user_id, source_user_id, order_id, fill_id, points_ledger_uuid, created_at
            ) VALUES (?, ?, ?, 'pointschain_exchange_fund_alignment', 'POINTSCHAIN_EXCHANGE_FUND_ALIGNMENT', NULL, NULL, NULL, NULL, NULL, ?)
            """,
            (event_uuid, delta, exchange_balance, now),
        )
        return snapshot

    def _actor_username(self, actor):
        try:
            return str(actor.get("username") if hasattr(actor, "get") else actor["username"])
        except Exception:
            return ""

    def _actor_role(self, actor):
        try:
            return str(actor.get("role") if hasattr(actor, "get") else actor["role"]) or "user"
        except Exception:
            return "user"

    def _is_root_actor(self, actor):
        return self._actor_username(actor) == "root"

    def _audit_event(self, conn, event_type, message, *, actor=None, target_user_id=None, order_id=None, market_symbol=None, severity="info", metadata=None):
        emit_trading_audit_event(
            conn,
            event_type=event_type,
            message=message,
            actor_id=self._actor_id(actor),
            target_user_id=target_user_id,
            order_id=order_id,
            market_symbol=market_symbol,
            severity=severity,
            metadata=metadata,
            json_dumps=_json_dumps,
            now_text=_now,
            uuid_factory=uuid.uuid4,
        )

    def _state(self, conn):
        row = conn.execute("SELECT * FROM trading_state WHERE id=1").fetchone()
        if not row:
            ensure_trading_schema(conn)
            row = conn.execute("SELECT * FROM trading_state WHERE id=1").fetchone()
        return {
            "safe_mode": bool(row["safe_mode"]),
            "reason": row["reason"] or "",
            "verification": _json_loads(row["verification_json"], {}),
            "updated_at": row["updated_at"],
        }

    def _assert_writable(self, conn):
        state = self._state(conn)
        if state["safe_mode"]:
            raise ValueError(f"Trading safe mode active: {state['reason'] or 'verification failed'}")
        try:
            points_state = self.points_service._safe_mode_status(conn)
        except Exception:
            points_state = self.points_service.safe_mode_status()
        if points_state.get("safe_mode"):
            reason = points_state.get("reason") or "points chain verification failed"
            raise ValueError(f"PointsChain safe mode active: {reason}; trading is paused")
        enabled = conn.execute("SELECT value FROM trading_settings WHERE key='trading.enabled'").fetchone()
        if enabled and str(enabled["value"]).lower() not in {"true", "1", "yes"}:
            raise ValueError("trading is disabled")

    def _matching_orderbook_namespace(self, market_symbol, *, ctx=None):
        route_ctx = self._resolve_trading_ctx(ctx, action="matching_orderbook")
        market = str(market_symbol or "").strip().upper()
        key = matching_orderbook_key(market, route_ctx)
        book = self._matching_orderbooks.setdefault(
            key,
            {
                "market_symbol": market,
                "mode": route_ctx.mode,
                "tester_id": route_ctx.tester_id,
                "buy": {},
                "sell": {},
            },
        )
        return key, book, route_ctx

    def _matching_orderbook_keys_for_ctx(self, ctx):
        route_ctx = self._resolve_trading_ctx(ctx, action="matching_orderbook")
        return [
            key
            for key, book in self._matching_orderbooks.items()
            if str(book.get("mode") or "") == route_ctx.mode
            and int(book.get("tester_id") or 0) == int(route_ctx.tester_id or 0)
        ]

    def _matching_orderbook_apply_order(self, order, *, ctx=None):
        if not order:
            return None
        if str(order["order_type"] or "").strip().lower() != "limit":
            return None
        order_uuid = str(order["order_uuid"] or "").strip()
        if not order_uuid:
            return None
        side = str(order["side"] or "").strip().lower()
        key, book, route_ctx = self._matching_orderbook_namespace(order["market_symbol"], ctx=ctx)
        for side_name in ("buy", "sell"):
            if side_name != side:
                book[side_name].pop(order_uuid, None)
        if str(order["status"] or "") in OPEN_ORDER_STATUSES:
            book[side][order_uuid] = {
                "id": int(order["id"]),
                "order_uuid": order_uuid,
                "market_symbol": str(order["market_symbol"] or "").strip().upper(),
                "side": side,
                "status": str(order["status"] or ""),
                "limit_price_points": order["limit_price_points"],
                "updated_at": order["updated_at"],
            }
        else:
            book[side].pop(order_uuid, None)
        return key, route_ctx

    def _matching_orderbook_hydrate(self, conn, *, market_symbol=None, limit=200, ctx=None):
        orders_table, route_ctx = self._resolve_table("orders", ctx, action="matching_orderbook_hydrate")
        params = []
        where = "WHERE order_type='limit' AND status IN ('open', 'partially_filled')"
        if route_ctx.mode == "internal_test":
            if route_ctx.tester_id is None:
                raise ValueError("internal_test matching orderbook hydrate requires tester_id")
            where += " AND tester_user_id=?"
            params.append(int(route_ctx.tester_id))
        market = str(market_symbol or "").strip().upper()
        if market:
            where += " AND market_symbol=?"
            params.append(market)
        rows = conn.execute(
            f"SELECT * FROM {orders_table} {where} ORDER BY id ASC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        live_by_key = {}
        for row in rows:
            applied = self._matching_orderbook_apply_order(row, ctx=route_ctx)
            if not applied:
                continue
            key, _applied_ctx = applied
            live_by_key.setdefault(key, set()).add(str(row["order_uuid"] or ""))
        if market:
            keys = [matching_orderbook_key(market, route_ctx)]
        else:
            keys = self._matching_orderbook_keys_for_ctx(route_ctx)
        for key in keys:
            book = self._matching_orderbooks.get(key)
            if not book:
                continue
            live_uuids = live_by_key.get(key, set())
            for side_name in ("buy", "sell"):
                for order_uuid in list(book[side_name].keys()):
                    if order_uuid not in live_uuids:
                        book[side_name].pop(order_uuid, None)
        return route_ctx

    def _matching_orderbook_order_uuids(self, conn, *, market_symbol=None, limit=200, ctx=None):
        route_ctx = self._matching_orderbook_hydrate(conn, market_symbol=market_symbol, limit=limit, ctx=ctx)
        market = str(market_symbol or "").strip().upper()
        if market:
            keys = [matching_orderbook_key(market, route_ctx)]
        else:
            keys = self._matching_orderbook_keys_for_ctx(route_ctx)
        items = []
        for key in keys:
            book = self._matching_orderbooks.get(key) or {}
            for side_name in ("buy", "sell"):
                items.extend((book.get(side_name) or {}).values())
        items.sort(key=lambda item: int(item.get("id") or 0))
        return [str(item.get("order_uuid") or "") for item in items[: int(limit)] if str(item.get("order_uuid") or "")]

    def _funding_snapshot_ctx(self, snapshot):
        return funding_snapshot_ctx_helper(snapshot)

    def publish_funding_rate_snapshot(
        self,
        *,
        market_symbol,
        rate_percent,
        actor=None,
        ctx=None,
        provider_count=1,
        confidence="medium",
        stale=False,
        degraded=False,
        exclusion_reason="",
    ):
        return publish_funding_rate_snapshot_helper(
            self,
            market_symbol=market_symbol,
            rate_percent=rate_percent,
            actor=actor,
            ctx=ctx,
            provider_count=provider_count,
            confidence=confidence,
            stale=stale,
            degraded=degraded,
            exclusion_reason=exclusion_reason,
        )

    def get_funding_rate_snapshot(self, *, market_symbol, ctx=None):
        return get_funding_rate_snapshot_helper(self, market_symbol=market_symbol, ctx=ctx)

    def settle_funding_adjustment(
        self,
        *,
        actor,
        user_id,
        market_symbol,
        delta_points,
        published_snapshot=None,
        ctx=None,
        idempotency_key=None,
    ):
        return settle_funding_adjustment_helper(
            self,
            actor=actor,
            user_id=user_id,
            market_symbol=market_symbol,
            delta_points=delta_points,
            published_snapshot=published_snapshot,
            ctx=ctx,
            idempotency_key=idempotency_key,
        )

    def _legacy_production_ctx(self):
        return SmV2Context(
            mode="production",
            tester_id=None,
            actor_role="system",
            request_id="legacy-trading",
        )

    def _resolve_trading_ctx(self, ctx=None, *, action="trade"):
        if ctx is None:
            try:
                ctx = current_ctx()
            except Exception:
                ctx = self._legacy_production_ctx()
        return assert_trading_allowed(ctx, action=action)

    def _ambient_trading_ctx(self):
        try:
            return current_ctx()
        except Exception:
            return self._legacy_production_ctx()

    def _routing_ctx_for_read(self, ctx=None):
        route_ctx = ctx or self._ambient_trading_ctx()
        if getattr(route_ctx, "mode", "") not in {"production", "internal_test"}:
            return self._legacy_production_ctx()
        return route_ctx

    def _resolve_table(self, logical, ctx=None, *, for_write=False, action="trade"):
        route_ctx = self._resolve_trading_ctx(ctx, action=action) if for_write else self._routing_ctx_for_read(ctx)
        return resolve_table(logical, route_ctx), route_ctx

    def _sql_tables(self, ctx=None, *, for_write=False, action="trade"):
        _orders, route_ctx = self._resolve_table("orders", ctx, for_write=for_write, action=action)
        return ({
            "orders": _orders,
            "positions": resolve_table("positions", route_ctx),
            "points_ledger": resolve_table("points_ledger", route_ctx),
            "wallets": resolve_table("wallets", route_ctx),
        }, route_ctx)

    def _format_routed_sql(self, sql, ctx=None, *, for_write=False, action="trade"):
        tables, route_ctx = self._sql_tables(ctx, for_write=for_write, action=action)
        return sql.format(**tables), route_ctx

    def _execute_routed_sql(self, conn, sql, params=(), ctx=None, *, for_write=False, action="trade"):
        formatted, route_ctx = self._format_routed_sql(sql, ctx, for_write=for_write, action=action)
        return conn.execute(formatted, params), route_ctx

    def _shadow_actor_user_id(self, ctx, user_id):
        return shadow_actor_user_id_helper(ctx, user_id)

    def _ensure_shadow_wallet(self, conn, user_id, ctx):
        return ensure_shadow_wallet_helper(self, conn, user_id, ctx)

    def _shadow_wallet_payload(self, row):
        return shadow_wallet_payload_helper(self, row)

    def _wallet_row(self, conn, user_id, ctx=None):
        return wallet_row_helper(self, conn, user_id, ctx=ctx)

    def _wallet_payload(self, conn, user_id, ctx=None):
        return wallet_payload_helper(self, conn, user_id, ctx=ctx)

    def _wallet_payload_for_source(
        self,
        conn,
        user_id,
        source_wallet_address=None,
        ctx=None,
        require_spend=False,
        spend_context="trading spend",
    ):
        source_address = str(source_wallet_address or "").strip().lower()
        exchange_only = str(spend_context or "").strip().lower().startswith("exchange")
        def _is_exchange_hot_wallet(row):
            return bool(
                row
                and str(row["wallet_type"] if "wallet_type" in row.keys() else "").strip() == "official_hot"
                and str(row["custody_mode"] if "custody_mode" in row.keys() else "").strip() == "server_hot"
                and is_pc0_internal_address(row["address"] if "address" in row.keys() else "")
            )

        def _payload_for_wallet(row, *, source_label):
            selected_address = str(row["address"] if "address" in row.keys() else "").strip().lower()
            state = self.points_service._wallet_identity_balances_for_user(conn, int(user_id))
            balances = state.get("balances") or {}
            selected = balances.get(selected_address)
            if selected is None:
                raise ValueError("指定交易付款錢包沒有可用餘額資料")
            payload = self.points_service.wallet_payload_for_read(conn, int(user_id))
            balance = int(selected.get("balance") or 0)
            frozen = int(selected.get("frozen") or 0)
            payload.update(
                {
                    "points_balance": balance,
                    "points_frozen": frozen,
                    "soft_balance": balance,
                    "soft_frozen": frozen,
                    "hard_balance": 0,
                    "hard_frozen": 0,
                    "active_wallet_address": selected_address,
                    "selected_wallet_address": selected_address,
                    "selected_wallet_label": row["label"] if "label" in row.keys() else "",
                    "wallet_identity_source": source_label,
                }
            )
            return payload

        if not source_address:
            if exchange_only:
                hot = conn.execute(
                    """
                    SELECT *
                    FROM points_wallet_identities
                    WHERE user_id=? AND wallet_type='official_hot' AND custody_mode='server_hot'
                      AND status IN ('pending_backup', 'active')
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (int(user_id),),
                ).fetchone()
                if not hot:
                    repaired = create_official_hot_wallet(
                        conn,
                        user_id=int(user_id),
                        chain_secret=self.points_service.chain_secret,
                    )
                    try:
                        self.points_service.ensure_user_deposit_address(conn, int(user_id))
                    except Exception:
                        pass
                    hot = conn.execute(
                        """
                        SELECT *
                        FROM points_wallet_identities
                        WHERE address=?
                        LIMIT 1
                        """,
                        (repaired["address"],),
                    ).fetchone()
                if not hot:
                    raise ValueError("交易所僅支援 pc0 站內託管錢包；請先完成站內託管錢包建立")
                if not _is_exchange_hot_wallet(hot):
                    raise ValueError("交易所僅支援 pc0 站內託管錢包，不支援冷錢包直接下單")
                if require_spend:
                    self.points_service._assert_wallet_identity_can_spend(hot, context=spend_context)
                return _payload_for_wallet(hot, source_label="official_hot_exchange_wallet")
            payload = self._wallet_payload(conn, user_id, ctx=ctx)
            if require_spend:
                active_address = str(payload.get("active_wallet_address") or "").strip().lower()
                if active_address:
                    row = self.points_service._wallet_identity_row_for_user_address(
                        conn,
                        int(user_id),
                        active_address,
                        active_only=True,
                    )
                    if row:
                        self.points_service._assert_wallet_identity_can_spend(row, context=spend_context)
            return payload
        wallet_table, route_ctx = self._resolve_table("wallets", ctx, action="wallet-read")
        if wallet_table != "wallets":
            return self._wallet_payload(conn, user_id, ctx=route_ctx)
        row = self.points_service._wallet_identity_row_for_user_address(
            conn,
            int(user_id),
            source_address,
            active_only=True,
        )
        if not row:
            raise ValueError("指定交易付款錢包不存在或已停用")
        if exchange_only and not _is_exchange_hot_wallet(row):
            raise ValueError("交易所僅支援 pc0 站內託管錢包，不支援冷錢包直接下單")
        if require_spend:
            self.points_service._assert_wallet_identity_can_spend(row, context=spend_context)
        return _payload_for_wallet(row, source_label="selected_wallet")

    def _shadow_existing_ledger_row(self, conn, idempotency_key):
        return shadow_existing_ledger_row_helper(self, conn, idempotency_key)

    def _shadow_last_ledger_hash(self, conn):
        return shadow_last_ledger_hash_helper(self, conn)

    def _shadow_record_transaction(self, conn, *, ctx, user_id, currency_type, direction, amount, action_type, reference_type=None, reference_id=None, idempotency_key=None, reason="", public_metadata=None, private_metadata=None, sensitive_metadata_encrypted="", actor=None, risk_flag="none", risk_score=0):
        return shadow_record_transaction_helper(
            self,
            conn,
            ctx=ctx,
            user_id=user_id,
            currency_type=currency_type,
            direction=direction,
            amount=amount,
            action_type=action_type,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            reason=reason,
            public_metadata=public_metadata,
            private_metadata=private_metadata,
            sensitive_metadata_encrypted=sensitive_metadata_encrypted,
            actor=actor,
            risk_flag=risk_flag,
            risk_score=risk_score,
        )

    def _runtime_market_sort_key(self, item):
        symbol = str((item or {}).get("symbol") if isinstance(item, dict) else item or "").strip().upper()
        sort_order = (item or {}).get("sort_order") if isinstance(item, dict) else None
        try:
            return (int(sort_order or 9999), symbol)
        except Exception:
            return (9999, symbol)

    def _display_symbol_from_parts(self, *, base_asset="", quote_currency="", display_quote_currency=""):
        base = str(base_asset or "").strip().upper()
        quote = str(display_quote_currency or quote_currency or "").strip().upper()
        return f"{base}/{quote}" if base and quote else ""

    def _registry_market_row(self, conn, value, *, include_disabled=True):
        symbol = self._normalize_market_symbol_on_conn(conn, value, include_disabled=include_disabled)
        if not symbol:
            return None
        query = "SELECT * FROM trading_markets_registry WHERE symbol=?"
        params = [symbol]
        if not include_disabled:
            query += " AND enabled=1"
        return conn.execute(query, params).fetchone()

    def _normalize_market_symbol_on_conn(self, conn, value, *, include_disabled=True):
        rows = conn.execute(
            """
            SELECT symbol, base_asset, quote_asset, display_quote_currency, display_name, enabled
            FROM trading_markets_registry
            """
        ).fetchall()
        return normalize_market_symbol_from_rows(
            rows,
            value,
            include_disabled=include_disabled,
            display_symbol_from_parts=self._display_symbol_from_parts,
        )

    def normalize_market_symbol(self, value, *, include_disabled=True):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            return self._normalize_market_symbol_on_conn(conn, value, include_disabled=include_disabled)
        finally:
            conn.close()

    def _market_provider_mappings(self, conn, symbol, *, include_disabled=False):
        market = self._registry_market_row(conn, symbol, include_disabled=True)
        if not market:
            return []
        query = """
            SELECT *
            FROM trading_market_provider_mappings
            WHERE market_id=?
        """
        params = [int(market["id"])]
        if not include_disabled:
            query += " AND enabled=1"
        query += " ORDER BY priority ASC, id ASC"
        return conn.execute(query, params).fetchall()

    def _market_provider_ids_from_mappings(self, rows, *, support_field=None):
        return market_provider_ids_from_mappings(rows, support_field=support_field)

    def _market_provider_id_on_conn(self, conn, symbol, provider):
        provider_key = str(provider or "").strip()
        if not provider_key:
            return ""
        mapping = next(
            (
                row for row in self._market_provider_mappings(conn, symbol)
                if str(row["provider"] or "").strip() == provider_key
            ),
            None,
        )
        if mapping and int(mapping["enabled"] or 0):
            return str(mapping["provider_symbol"] or "").strip()
        return ""

    def market_provider_id(self, symbol, provider, *, conn=None):
        if conn is not None:
            return self._market_provider_id_on_conn(conn, symbol, provider)
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            return self._market_provider_id_on_conn(conn, symbol, provider)
        finally:
            conn.close()

    def _market_supports_live_price_on_conn(self, conn, symbol):
        market = self._registry_market_row(conn, symbol, include_disabled=True)
        if not market or not int(market["live_price_enabled"] or 0):
            return False
        return market_supports_mapping_rows(
            self._market_provider_mappings(conn, symbol),
            support_field="supports_ticker",
        )

    def _market_supports_reference_price_on_conn(self, conn, symbol):
        market = self._registry_market_row(conn, symbol, include_disabled=True)
        if not market or not int(market["reference_price_enabled"] or 0):
            return False
        return market_supports_mapping_rows(
            self._market_provider_mappings(conn, symbol),
            support_field="supports_candles",
        )

    def _market_supports_btc_trade_on_conn(self, conn, symbol):
        market = self._registry_market_row(conn, symbol, include_disabled=True)
        return bool(market and int(market["btc_trade_enabled"] or 0))

    def market_supports_reference_price(self, symbol):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            return self._market_supports_reference_price_on_conn(conn, symbol)
        finally:
            conn.close()

    def market_supports_btc_trade(self, symbol):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            return self._market_supports_btc_trade_on_conn(conn, symbol)
        finally:
            conn.close()

    def _list_live_price_market_symbols(self, conn):
        rows = conn.execute("SELECT symbol FROM trading_markets_registry WHERE enabled=1 ORDER BY sort_order ASC, symbol ASC").fetchall()
        return [str(row["symbol"] or "").strip().upper() for row in rows if self._market_supports_live_price_on_conn(conn, row["symbol"])]

    def _list_reference_price_market_symbols(self, conn):
        rows = conn.execute("SELECT symbol FROM trading_markets_registry WHERE enabled=1 ORDER BY sort_order ASC, symbol ASC").fetchall()
        return [str(row["symbol"] or "").strip().upper() for row in rows if self._market_supports_reference_price_on_conn(conn, row["symbol"])]

    def _market_display_symbol_on_conn(self, conn, symbol, quote_currency=None):
        market = self._registry_market_row(conn, symbol, include_disabled=True)
        if market:
            return market_display_symbol_from_registry_row(
                market,
                display_symbol_from_parts=self._display_symbol_from_parts,
            )
        return fallback_market_display_symbol(symbol, quote_currency=quote_currency)

    def market_display_symbol(self, symbol, quote_currency=None):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            return self._market_display_symbol_on_conn(conn, symbol, quote_currency=quote_currency)
        finally:
            conn.close()

    def _market(self, conn, symbol):
        row = conn.execute(
            "SELECT * FROM trading_markets WHERE symbol=?",
            (self._normalize_market_symbol_on_conn(conn, symbol),),
        ).fetchone()
        if not row:
            raise ValueError("market not found")
        if not int(row["enabled"] or 0) or not int(row["spot_enabled"] or 0):
            raise ValueError("spot trading is disabled for this market")
        if row["execution_mode"] != "house_counterparty":
            raise ValueError("only house_counterparty execution is enabled in v1")
        return dict(row)

    def _allow_unready_markets(self, conn=None):
        if str(os.environ.get("HACKME_DEV_TRADING_ALLOW_UNREADY_MARKETS") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
        if conn is None:
            return False
        row = conn.execute(
            "SELECT value FROM trading_settings WHERE key IN (?, ?) ORDER BY CASE key WHEN ? THEN 0 ELSE 1 END LIMIT 1",
            ("trading.allow_unready_markets", "trading.dev_allow_unready_markets", "trading.allow_unready_markets"),
        ).fetchone()
        return str(row["value"] if row else "").strip().lower() in {"1", "true", "yes", "on"}

    def _is_market_boot_ready(self, market, conn=None):
        """Boot-ready gate (2026-05-06).

        ``trading_markets.live_price_confirmed_at`` is NULL until the market
        has survived warmup with at least two stable live quotes. Root can turn
        this gate into a warning-only state because this project trades site
        points, not fiat-backed assets.
        """
        if self._allow_unready_markets(conn):
            return True
        if not isinstance(market, dict) and "live_price_confirmed_at" not in (getattr(market, "keys", lambda: ())()):
            return False
        try:
            confirmed_at = market["live_price_confirmed_at"]
        except (KeyError, IndexError):
            confirmed_at = None
        return bool(str(confirmed_at or "").strip())

    def _assert_market_boot_ready(self, market, *, usage="trading", conn=None):
        if not self._is_market_boot_ready(market, conn=conn):
            symbol = ""
            try:
                symbol = str(market["symbol"])
            except Exception:
                pass
            raise ValueError(
                f"market {symbol or '?'} 尚未收到任何即時價格更新，{usage} 暫停以避免使用啟動時的預設參考價"
            )

    def _validate_market_quantity_constraints(self, market, quantity_units):
        quantity_units = int(quantity_units or 0)
        if quantity_units <= 0:
            raise ValueError("quantity must be positive")
        quantity_decimal = Decimal(quantity_units) / Decimal(ASSET_SCALE)
        min_order_size = Decimal(str(market["min_order_size"] if "min_order_size" in market.keys() else "0.00000001"))
        max_order_size = Decimal(str(market["max_order_size"] if "max_order_size" in market.keys() else "1000000"))
        if quantity_decimal < min_order_size:
            raise ValueError(f"quantity below minimum {_decimal_text(min_order_size)}")
        if quantity_decimal > max_order_size:
            raise ValueError(f"quantity above maximum {_decimal_text(max_order_size)}")
        quantity_precision = int(market["quantity_precision"] if "quantity_precision" in market.keys() else 8)
        precision_step_units = _quantity_step_units_from_precision(quantity_precision)
        if precision_step_units > 1 and quantity_units % precision_step_units != 0:
            raise ValueError(f"quantity exceeds quantity precision {quantity_precision}")
        lot_size = Decimal(str(market["lot_size"] if "lot_size" in market.keys() else "0.00000001"))
        lot_units = max(1, _decimal_units(lot_size))
        if lot_units > 1 and quantity_units % lot_units != 0:
            raise ValueError(f"quantity must align with lot size {units_to_quantity(lot_units)}")

    def _validate_market_limit_price(self, market, raw_price):
        price_decimal = _to_decimal(raw_price, name="limit_price_points", minimum=0.00000001)
        price_precision = int(market["price_precision"] if "price_precision" in market.keys() else 8)
        quantum = Decimal(1).scaleb(-max(0, min(price_precision, 8)))
        if price_decimal != price_decimal.quantize(quantum, rounding=ROUND_HALF_UP):
            raise ValueError(f"limit price exceeds price precision {price_precision}")
        tick_size = Decimal(str(market["tick_size"] if "tick_size" in market.keys() else "0.00000001"))
        tick_units = max(1, _decimal_units(tick_size))
        price_units = max(1, _decimal_units(price_decimal))
        if tick_units > 1 and price_units % tick_units != 0:
            raise ValueError(f"limit price must align with tick size {_decimal_text(tick_size)}")
        return float(price_decimal.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))

    def _position(self, conn, user_id, symbol, *, ctx=None):
        now = _now()
        positions_table, route_ctx = self._resolve_table("positions", ctx, action="position-read")
        if positions_table == "test_shadow_positions":
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {positions_table}
                    (tester_user_id, user_id, market_symbol, quantity_units, locked_quantity_units, avg_cost_points, updated_at)
                VALUES (?, ?, ?, 0, 0, 0, ?)
                """,
                (self._shadow_actor_user_id(route_ctx, user_id), int(user_id), symbol, now),
            )
        else:
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {positions_table}
                    (user_id, market_symbol, quantity_units, locked_quantity_units, avg_cost_points, updated_at)
                VALUES (?, ?, 0, 0, 0, ?)
                """,
                (int(user_id), symbol, now),
            )
        return conn.execute(
            f"SELECT * FROM {positions_table} WHERE user_id=? AND market_symbol=?",
            (int(user_id), symbol),
        ).fetchone()

    def _reserve(self, conn):
        row = conn.execute("SELECT * FROM trading_reserve_pool WHERE id=1").fetchone()
        if not row:
            ensure_trading_schema(conn)
            row = conn.execute("SELECT * FROM trading_reserve_pool WHERE id=1").fetchone()
        return row

    def _funding_pool_outstanding_principal(self, conn):
        lent = int(conn.execute(
            """
            SELECT COALESCE(SUM(delta_points), 0)
            FROM trading_reserve_pool_events
            WHERE event_type='margin_principal_lent'
            """
        ).fetchone()[0] or 0)
        repaid = int(conn.execute(
            """
            SELECT COALESCE(SUM(delta_points), 0)
            FROM trading_reserve_pool_events
            WHERE event_type='margin_principal_repaid'
            """
        ).fetchone()[0] or 0)
        return funding_pool_outstanding_principal(lent=lent, repaid=repaid)

    def _borrow_apr_percent_for_asset(self, settings, *, asset_symbol):
        group = _borrow_apr_group_for_asset(asset_symbol)
        def resolve_setting(key, default):
            value = settings.get(key)
            if value is None or value == "":
                return float(default)
            return float(value)
        if group == "btc_eth":
            return resolve_setting("borrow_apr_btc_eth_percent", DEFAULT_BORROW_APR_BTC_ETH_PERCENT)
        return resolve_setting("borrow_apr_usdt_points_percent", DEFAULT_BORROW_APR_USDT_POINTS_PERCENT)

    def _margin_borrowed_asset_symbol(self, market, position_type):
        market_row = dict(market) if market is not None and not isinstance(market, dict) else (market or {})
        if str(position_type or "").strip().lower() == "short":
            return str(market_row.get("base_asset") or "").strip().upper() or "BTC"
        return str(market_row.get("quote_currency") or "POINTS").strip().upper() or "POINTS"

    def _grid_fee_rate_percent(self, base_fee_rate_percent, settings):
        return grid_fee_rate_percent(base_fee_rate_percent, settings)

    def _funding_pool_payload(self, conn, *, requested_principal=0, borrowed_asset=None):
        reserve = self._reserve(conn)
        settings = self._settings_payload(conn)
        balance = int(reserve["balance_points"] or 0)
        outstanding = self._funding_pool_outstanding_principal(conn)
        borrowed_asset = str(borrowed_asset or "POINTS").strip().upper() or "POINTS"
        base_apr = self._borrow_apr_percent_for_asset(settings, asset_symbol=borrowed_asset)
        raw_pressure = settings.get("borrow_interest_pool_pressure_multiplier")
        pressure = float(TRADING_FUNDING_POOL_PRESSURE_MULTIPLIER if raw_pressure is None else raw_pressure)
        max_utilization_percent = float(settings.get("margin_max_pool_utilization_percent") or 0)
        exchange_total_assets = max(0, balance + outstanding)
        max_outstanding = int(math.floor(exchange_total_assets * max_utilization_percent / 100.0))
        cfd_profit_reserve_required = max(0, exchange_total_assets - max_outstanding)
        remaining_capacity = max(0, max_outstanding - outstanding)
        liquid_available = min(max(0, balance), remaining_capacity)
        payload = funding_pool_payload(
            balance=liquid_available,
            outstanding=outstanding,
            requested_principal=requested_principal,
            borrowed_asset=borrowed_asset,
            base_apr=base_apr,
            pressure=pressure,
            initial_points=TRADING_FUNDING_POOL_INITIAL_POINTS,
            daily_from_apr=_daily_percent_from_apr,
            apr_from_daily=_apr_percent_from_daily,
            lendable_capacity=max_outstanding,
            liquid_available=liquid_available,
            exchange_fund_balance=balance,
            cfd_profit_reserve_required=cfd_profit_reserve_required,
        )
        projected_outstanding = int(payload["outstanding_principal_points"] or 0) + max(
            0, int(requested_principal or 0)
        )
        payload.update({
            "max_pool_utilization_percent": max_utilization_percent,
            "max_outstanding_principal_points": max_outstanding,
            "remaining_borrow_capacity_points": liquid_available,
            "projected_outstanding_principal_points": projected_outstanding,
            "projected_over_utilization_limit": projected_outstanding > max_outstanding,
            "exchange_fund_total_assets_points": exchange_total_assets,
            "cfd_profit_reserve_percent": round(max(0.0, 100.0 - max_utilization_percent), 6),
        })
        return payload

    def _reserve_delta(self, conn, *, delta, event_type, reason, actor=None, source_user_id=None, order_id=None, fill_id=None, points_ledger_uuid=None):
        reserve = self._reserve(conn)
        balance = int(reserve["balance_points"] or 0)
        next_balance = balance + int(delta)
        if next_balance < 0:
            raise ValueError("trading funding pool is insufficient")
        now = _now()
        event_uuid = str(uuid.uuid4())
        conn.execute(
            "UPDATE trading_reserve_pool SET balance_points=?, updated_at=?, updated_by=? WHERE id=1",
            (next_balance, now, self._actor_id(actor)),
        )
        conn.execute(
            """
            INSERT INTO trading_reserve_pool_events (
                event_uuid, delta_points, balance_after, event_type, reason, actor_user_id,
                source_user_id, order_id, fill_id, points_ledger_uuid, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_uuid,
                int(delta),
                next_balance,
                event_type,
                reason or "",
                self._actor_id(actor),
                int(source_user_id) if source_user_id else None,
                int(order_id) if order_id else None,
                int(fill_id) if fill_id else None,
                points_ledger_uuid,
                now,
            ),
        )
        self.points_service.append_trading_reserve_economy_event(
            conn,
            reserve_event_uuid=event_uuid,
            delta=int(delta),
            event_type=event_type,
            reason=reason,
            actor=actor,
            source_user_id=source_user_id,
            order_id=order_id,
            fill_id=fill_id,
            points_ledger_uuid=points_ledger_uuid,
        )
        return next_balance

    def _reserve_profit_settlement_capacity(
        self,
        conn,
        *,
        requested_points,
        **_metadata,
    ):
        requested = max(0, int(requested_points or 0))
        if requested <= 0:
            return {"payable_points": 0, "pending_points": 0}
        reserve = self._reserve(conn)
        balance = int(reserve["balance_points"] or 0)
        payable = min(requested, balance)
        return {
            "payable_points": payable,
            "pending_points": requested - payable,
        }

    def _trading_governance_actor(self, conn):
        try:
            row = conn.execute(
                """
                SELECT id, username, role
                FROM users
                WHERE username='root' OR role IN ('manager', 'super_admin')
                ORDER BY CASE WHEN username='root' THEN 0 WHEN role='super_admin' THEN 1 ELSE 2 END, id ASC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            row = None
        if not row:
            return None
        return {"id": row["id"], "username": row["username"], "role": row["role"]}

    def _pending_profit_policy_payload(self, conn, *, new_amount_points, market_symbol, position_uuid, user_id):
        settings = self._settings_payload(conn)
        existing_pending = int(
            conn.execute(
                "SELECT COALESCE(SUM(amount_points), 0) FROM trading_pending_profit WHERE status='pending'"
            ).fetchone()[0]
            or 0
        )
        liability_after = existing_pending + int(new_amount_points or 0)
        limit_points = int(settings.get("exchange_liability_limit_points") or 0)
        grace_minutes = int(settings.get("exchange_liability_grace_minutes") or 0)
        settlement_interval = int(settings.get("profit_settlement_interval_minutes") or 0)
        return {
            "proposal_type": "TRADING_EXCHANGE_SHORTFALL_RESOLUTION",
            "execution_class": "TRADING_EMERGENCY_GOVERNANCE",
            "description": "Resolve exchange fund profit-settlement shortfall without automatic treasury spending.",
            "market_symbol": str(market_symbol or ""),
            "position_uuid": str(position_uuid or ""),
            "affected_user_id": int(user_id),
            "new_pending_profit_points": int(new_amount_points or 0),
            "existing_pending_profit_points": existing_pending,
            "liability_after_points": liability_after,
            "current_policy": {
                "exchange_liability_limit_points": limit_points,
                "exchange_liability_grace_minutes": grace_minutes,
                "profit_settlement_interval_minutes": settlement_interval,
            },
            "options": [
                {
                    "key": "official_treasury_replenishment",
                    "label": "官方 Treasury 撥補交易所基金",
                    "requires_action_type": "EXCHANGE_FUND_REPLENISH",
                    "amount_points": int(new_amount_points or 0),
                },
                {
                    "key": "forced_borrower_repayment",
                    "label": "要求借貸方提前還款或降低未償本金",
                    "source_user_id": int(user_id),
                    "amount_points": int(new_amount_points or 0),
                },
                {
                    "key": "accept_temporary_liability",
                    "label": "接受限額內暫時負債並排程結算",
                    "suggested_liability_limit_points": max(limit_points, liability_after),
                    "suggested_grace_minutes": max(grace_minutes, 60),
                    "suggested_settlement_interval_minutes": max(settlement_interval, 60),
                },
            ],
        }

    def _create_exchange_shortfall_governance_proposal(
        self,
        conn,
        *,
        user_id,
        market_symbol,
        amount_points,
        position_uuid="",
    ):
        actor = self._trading_governance_actor(conn)
        if not actor:
            return ""
        payload = self._pending_profit_policy_payload(
            conn,
            new_amount_points=amount_points,
            market_symbol=market_symbol,
            position_uuid=position_uuid,
            user_id=user_id,
        )
        try:
            proposal = self.points_service._create_governance_proposal_locked(
                conn,
                actor=actor,
                proposal_type="protocol_parameter_change",
                governance_domain="PROTOCOL_PARAMETER",
                action_type="PARAMETER_CHANGE",
                title="交易所基金短缺緊急治理",
                description=payload["description"],
                reason="交易所基金不足以即時支付已實現盈利，需要治理決定處理方式",
                reference=f"trading-shortfall:{position_uuid or uuid.uuid4()}",
                requested_amount=int(amount_points or 0),
                requested_asset="points",
                payload=payload,
                impact_scope="exchange fund liabilities, borrower repayment policy, and profit settlement cadence",
                risk_summary="No automatic official-wallet spend is performed; voters choose replenishment, forced repayment, or bounded temporary liability.",
                proposal_severity="CRITICAL",
            )
            return str(proposal.get("proposal_uuid") or "")
        except Exception as exc:
            self._audit_event(
                conn,
                "TRADING_EXCHANGE_SHORTFALL_GOVERNANCE_CREATE_FAILED",
                "failed to create exchange shortfall governance proposal",
                actor=actor,
                target_user_id=user_id,
                market_symbol=str(market_symbol or ""),
                severity="critical",
                metadata={
                    "amount_points": int(amount_points or 0),
                    "position_uuid": str(position_uuid or ""),
                    "error": str(exc)[:240],
                },
            )
            return ""

    def _record_pending_profit(
        self,
        conn,
        *,
        user_id,
        market_symbol,
        amount_points,
        reason,
        actor=None,
        position_uuid="",
    ):
        amount = max(0, int(amount_points or 0))
        if amount <= 0:
            return None
        now = _now()
        policy_payload = self._pending_profit_policy_payload(
            conn,
            new_amount_points=amount,
            market_symbol=market_symbol,
            position_uuid=position_uuid,
            user_id=user_id,
        )
        proposal_uuid = self._create_exchange_shortfall_governance_proposal(
            conn,
            user_id=user_id,
            market_symbol=market_symbol,
            amount_points=amount,
            position_uuid=position_uuid,
        )
        cur = conn.execute(
            """
            INSERT INTO trading_pending_profit (
                user_id, market_symbol, amount_points, status, reason, position_uuid,
                governance_proposal_uuid, liability_policy_json, settle_not_before_at,
                created_at, released_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                int(user_id),
                str(market_symbol or ""),
                amount,
                str(reason or "")[:500],
                str(position_uuid or ""),
                proposal_uuid,
                _json_dumps(policy_payload),
                None,
                now,
            ),
        )
        try:
            append_economy_incident(
                conn,
                severity="critical",
                category="trading_exchange_fund_shortfall",
                trigger="margin_profit_pending_liability",
                automatic_actions=["pending_profit_recorded", "emergency_governance_started"],
                metadata={
                    "user_id": int(user_id),
                    "market_symbol": str(market_symbol or ""),
                    "amount_points": amount,
                    "position_uuid": str(position_uuid or ""),
                    "governance_proposal_uuid": proposal_uuid,
                    "liability_policy": policy_payload,
                    "reason": str(reason or ""),
                },
            )
        except Exception:
            pass
        self._audit_event(
            conn,
            "TRADING_PENDING_PROFIT_RECORDED",
            "margin profit shortfall recorded as pending exchange liability",
            actor=actor,
            target_user_id=user_id,
            market_symbol=str(market_symbol or ""),
            severity="critical",
            metadata={
                "amount_points": amount,
                "position_uuid": str(position_uuid or ""),
                "governance_proposal_uuid": proposal_uuid,
                "liability_policy": policy_payload,
                "reason": str(reason or ""),
            },
        )
        return conn.execute("SELECT * FROM trading_pending_profit WHERE id=?", (cur.lastrowid,)).fetchone()

    def _ledger(self, conn, *, ctx=None, **kwargs):
        ledger_table, route_ctx = self._resolve_table("points_ledger", ctx, action="ledger-write")
        if ledger_table == "test_shadow_ledger":
            return self._shadow_record_transaction(conn, ctx=route_ctx, **kwargs)
        return self.points_service.rc1_facade().append_product_ledger_locked(conn, **kwargs)[0]

    def _user_volume_stats(self, conn, user_id):
        user_id = int(user_id)
        now = _now()
        conn.execute(
            """
            INSERT OR IGNORE INTO trading_user_volume_stats (
                user_id, total_notional_points, spot_notional_points, margin_notional_points,
                total_fee_points, total_trade_count, last_trade_at, updated_at
            ) VALUES (?, 0, 0, 0, 0, 0, NULL, ?)
            """,
            (user_id, now),
        )
        return conn.execute("SELECT * FROM trading_user_volume_stats WHERE user_id=?", (user_id,)).fetchone()

    def _record_user_trade_volume(self, conn, *, user_id, trade_kind, notional_points, fee_points=0, occurred_at=None):
        user_id = int(user_id)
        notional_points = max(0, int(notional_points or 0))
        fee_points = max(0, int(fee_points or 0))
        now = str(occurred_at or _now())
        current = self._user_volume_stats(conn, user_id)
        total_notional = int(current["total_notional_points"] or 0) + notional_points
        spot_notional = int(current["spot_notional_points"] or 0) + (notional_points if trade_kind == "spot" else 0)
        margin_notional = int(current["margin_notional_points"] or 0) + (notional_points if trade_kind == "margin" else 0)
        total_fee = int(current["total_fee_points"] or 0) + fee_points
        total_trade_count = int(current["total_trade_count"] or 0) + 1
        conn.execute(
            """
            UPDATE trading_user_volume_stats
            SET total_notional_points=?, spot_notional_points=?, margin_notional_points=?,
                total_fee_points=?, total_trade_count=?, last_trade_at=?, updated_at=?
            WHERE user_id=?
            """,
            (
                total_notional,
                spot_notional,
                margin_notional,
                total_fee,
                total_trade_count,
                now,
                now,
                user_id,
            ),
        )
        return conn.execute("SELECT * FROM trading_user_volume_stats WHERE user_id=?", (user_id,)).fetchone()

    def _order_payload(self, row):
        return order_payload(row, units_to_quantity=units_to_quantity)

    def _bot_payload(self, row):
        return bot_payload(
            row,
            bot_max_runs_from_storage=_bot_max_runs_from_storage,
            bot_max_runs_has_remaining=_bot_max_runs_has_remaining,
            now_text=_now,
            market_display_symbol=self.market_display_symbol,
            json_loads=_json_loads,
        )

    def _bot_run_payload(self, row):
        return bot_run_payload(row)

    def _market_payload(self, row):
        return market_payload(
            row,
            json_loads=_json_loads,
            display_symbol_from_parts=self._display_symbol_from_parts,
        )

    def _settings_payload(self, conn):
        return settings_payload_helper(self, conn)
    def get_root_settings(self):
        return get_root_settings_helper(self)
    def get_max_backtest_candles(self, conn=None):
        return get_max_backtest_candles_helper(self, conn=conn)
    def get_backtest_capacity_time_budget_seconds(self, conn=None):
        return get_backtest_capacity_time_budget_seconds_helper(self, conn=conn)
    def get_backtest_capacity_measurement(self, conn=None):
        return get_backtest_capacity_measurement_helper(self, conn=conn)
    def record_backtest_capacity_measurement(
        self,
        *,
        measured_capacity_min,
        measured_capacity_max,
        measured_at,
        bottleneck_strategy="",
        fastest_strategy="",
        actor_id="system",
        seed_default_cap=True,
    ):
        return record_backtest_capacity_measurement_helper(
            self,
            measured_capacity_min=measured_capacity_min,
            measured_capacity_max=measured_capacity_max,
            measured_at=measured_at,
            bottleneck_strategy=bottleneck_strategy,
            fastest_strategy=fastest_strategy,
            actor_id=actor_id,
            seed_default_cap=seed_default_cap,
        )
    def update_root_settings(self, *, actor, settings=None, markets=None):
        return update_root_settings_helper(self, actor=actor, settings=settings, markets=markets)
    def _bot_trigger_hit(self, bot, observed_price, *, observed_low=None, observed_high=None):
        return bot_trigger_hit_helper(
            bot,
            observed_price,
            observed_low=observed_low,
            observed_high=observed_high,
        )

    def _quantity_text_from_budget(self, *, budget_points, price_points, fee_rate_percent=0.0):
        return quantity_text_from_budget_helper(
            budget_points=budget_points,
            price_points=price_points,
            fee_rate_percent=fee_rate_percent,
        )

    def _build_workflow_indicator_series(self, candles):
        return build_workflow_indicator_series(candles)

    def _workflow_indicator_context(self, candles, index):
        return workflow_indicator_context(candles, index)

    def _workflow_condition_hit(self, condition, context):
        return workflow_condition_hit(condition, context)

    def _bot_condition_checks(self, bot, current_price):
        return bot_condition_checks_helper(self, bot, current_price)

    def _workflow_graph_decision(self, workflow, *, context, run_count=0, last_run_at=None, execution_state=None):
        return workflow_graph_decision(
            workflow,
            context=context,
            run_count=run_count,
            last_run_at=last_run_at,
            execution_state=execution_state,
            workflow_condition_hit_func=self._workflow_condition_hit,
        )

    def _workflow_decision(self, workflow, *, context, run_count=0, last_run_at=None, execution_state=None):
        return workflow_decision(
            workflow,
            context=context,
            run_count=run_count,
            last_run_at=last_run_at,
            execution_state=execution_state,
            validate_workflow_func=self._validate_workflow,
            workflow_graph_decision_func=self._workflow_graph_decision,
            workflow_condition_hit_func=self._workflow_condition_hit,
        )

    def _workflow_order_from_decision(self, conn, *, user_id, actor, market, decision, price_points, budget_points=None, bot_id=None):
        return workflow_order_from_decision_helper(
            self,
            conn,
            user_id=user_id,
            actor=actor,
            market=market,
            decision=decision,
            price_points=price_points,
            budget_points=budget_points,
            bot_id=bot_id,
        )

    def _bot_payload_with_budget_meta(self, conn, row):
        return bot_payload_with_budget_meta_helper(self, conn, row)

    def run_trading_bots(self, *, actor, limit=50):
        return run_trading_bots_helper(self, actor=actor, limit=limit)

    def adjust_trading_bot_budget(self, *, actor, bot_uuid, budget_points=None, delta_points=None):
        return adjust_trading_bot_budget_helper(
            self,
            actor=actor,
            bot_uuid=bot_uuid,
            budget_points=budget_points,
            delta_points=delta_points,
        )

    def run_trading_bot_once(self, *, actor, bot_uuid):
        return run_trading_bot_once_helper(self, actor=actor, bot_uuid=bot_uuid)

    def run_due_trading_bots(self, *, actor=None, limit=50):
        return run_due_trading_bots_helper(self, actor=actor, limit=limit)

    def _run_trading_bot_rows(self, rows):
        return run_trading_bot_rows_helper(self, rows)

    def backtest_trading_bot(self, *, actor, payload):
        return backtest_trading_bot_helper(self, actor=actor, payload=payload)

    def _record_bot_run(self, bot, *, status, observed_price=None, order_uuid=None, error="", execution_state=None):
        return record_bot_run_helper(
            self,
            bot,
            status=status,
            observed_price=observed_price,
            order_uuid=order_uuid,
            error=error,
            execution_state=execution_state,
        )

    def place_order(self, *, actor, market_symbol, side, order_type, quantity, limit_price_points=None, stop_loss_percent=None, take_profit_percent=None, emergency_close=False, is_grid_order=False, use_locked_inventory=False, source_wallet_address=None, ctx=None):
        return place_order_helper(
            self,
            actor=actor,
            market_symbol=market_symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price_points=limit_price_points,
            stop_loss_percent=stop_loss_percent,
            take_profit_percent=take_profit_percent,
            emergency_close=emergency_close,
            is_grid_order=is_grid_order,
            use_locked_inventory=use_locked_inventory,
            source_wallet_address=source_wallet_address,
            ctx=ctx,
        )

    def match_open_limit_orders(self, *, actor=None, market_symbol=None, limit=200, ctx=None):
        return match_open_limit_orders_helper(
            self,
            actor=actor,
            market_symbol=market_symbol,
            limit=limit,
            ctx=ctx,
        )

    def _execute_order(self, conn, order, market, *, actor, ctx=None):
        return execute_order_helper(self, conn, order, market, actor=actor, ctx=ctx)

    def cancel_order(self, *, actor, order_uuid, ctx=None):
        return cancel_order_helper(self, actor=actor, order_uuid=order_uuid, ctx=ctx)

    def open_margin_position(self, *, actor, market_symbol, position_type, quantity, collateral_points, stop_loss_percent=None, take_profit_percent=None, idempotency_key=None, ctx=None):
        return open_margin_position_helper(
            self,
            actor=actor,
            market_symbol=market_symbol,
            position_type=position_type,
            quantity=quantity,
            collateral_points=collateral_points,
            stop_loss_percent=stop_loss_percent,
            take_profit_percent=take_profit_percent,
            idempotency_key=idempotency_key,
            ctx=ctx,
        )

    def add_margin_collateral(self, *, actor, position_uuid, amount_points, idempotency_key=None, ctx=None):
        return add_margin_collateral_helper(
            self,
            actor=actor,
            position_uuid=position_uuid,
            amount_points=amount_points,
            idempotency_key=idempotency_key,
            ctx=ctx,
        )

    def withdraw_margin_collateral(self, *, actor, position_uuid, amount_points, idempotency_key=None, ctx=None):
        return withdraw_margin_collateral_helper(
            self,
            actor=actor,
            position_uuid=position_uuid,
            amount_points=amount_points,
            idempotency_key=idempotency_key,
            ctx=ctx,
        )

    def close_margin_position(self, *, actor, position_uuid, force_liquidation=False, price_override_points=None, price_source_override=None, ctx=None):
        return close_margin_position_helper(
            self,
            actor=actor,
            position_uuid=position_uuid,
            force_liquidation=force_liquidation,
            price_override_points=price_override_points,
            price_source_override=price_source_override,
            ctx=ctx,
        )

    def scan_margin_liquidations(self, *, actor=None, limit=100, ctx=None):
        return scan_margin_liquidations_helper(
            self,
            actor=actor,
            limit=limit,
            ctx=ctx,
        )

    def scan_spot_risk_targets(self, *, actor=None, limit=200, ctx=None):
        return scan_spot_risk_targets_helper(self, actor=actor, limit=limit, ctx=ctx)

    def scan_margin_risk_targets(self, *, actor=None, limit=100, ctx=None):
        return scan_margin_risk_targets_helper(self, actor=actor, limit=limit, ctx=ctx)

    def update_market(self, *, actor, symbol, manual_price_points=None, max_price_jump_percent=None, fee_rate_percent=None, min_order_points=None, max_order_points=None, enabled=None, confirm_jump=False):
        # source-contract breadcrumb: TRADING_MARKET_UPDATED
        return update_market_helper(
            self,
            actor=actor,
            symbol=symbol,
            manual_price_points=manual_price_points,
            max_price_jump_percent=max_price_jump_percent,
            fee_rate_percent=fee_rate_percent,
            min_order_points=min_order_points,
            max_order_points=max_order_points,
            enabled=enabled,
            confirm_jump=confirm_jump,
        )
    def allocate_reserve(self, *, actor, source_user_id, amount_points, reason):
        return allocate_reserve_helper(
            self,
            actor=actor,
            source_user_id=source_user_id,
            amount_points=amount_points,
            reason=reason,
        )
    def open_root_contract_position(self, *, actor, market_symbol, side, quantity, leverage, margin_points):
        return open_root_contract_position_helper(
            self,
            actor=actor,
            market_symbol=market_symbol,
            side=side,
            quantity=quantity,
            leverage=leverage,
            margin_points=margin_points,
            root_simulated_initial_points=ROOT_SIMULATED_INITIAL_POINTS,
            trial_credit_days=TRIAL_CREDIT_DAYS,
        )

    def close_root_contract_position(self, *, actor, position_uuid):
        return close_root_contract_position_helper(
            self,
            actor=actor,
            position_uuid=position_uuid,
            root_simulated_initial_points=ROOT_SIMULATED_INITIAL_POINTS,
            trial_credit_days=TRIAL_CREDIT_DAYS,
        )

    def reset_root_simulated_balance(self, *, actor):
        return reset_root_simulated_balance_helper(
            self,
            actor=actor,
            root_simulated_initial_points=ROOT_SIMULATED_INITIAL_POINTS,
        )

    def _replay_positions(self, conn):
        return replay_positions_helper(self, conn)

    def _ledger_row(self, conn, ledger_uuid):
        return ledger_row_helper(self, conn, ledger_uuid)

    def _verify_fill_ledgers(self, conn, errors):
        # verification.py keeps the batch lookup strategy via `ledger_by_uuid`
        # and intentionally avoids per-ledger row lookups in this hot path.
        return verify_fill_ledgers_helper(self, conn, errors)

    def _verify_open_order_locks(self, conn, errors):
        return verify_open_order_locks_helper(self, conn, errors)

    def _verify_reserve_pool(self, conn, errors):
        return verify_reserve_pool_helper(self, conn, errors)

    def _verify_sim_accounts(self, conn, errors):
        # verification.py still checks root simulated collateral joins using:
        # FROM trading_margin_positions p
        # and the `u.username='root'` guard.
        return verify_sim_accounts_helper(self, conn, errors)

    def _verify_margin_position_locks(self, conn, errors):
        # verification.py preserves the root-simulated split:
        # is_root_simulated = user_id in root_user_ids
        # expected = 0 if is_root_simulated else (int(position["collateral_chain_points"] or 0) ...)
        return verify_margin_position_locks_helper(self, conn, errors)

    def _verify_spot_realized_pnl(self, conn, errors):
        return verify_spot_realized_pnl_helper(self, conn, errors)

    def _verify_state_on_conn(self, conn, *, enter_safe_mode=False):
        return verify_state_on_conn_helper(self, conn, enter_safe_mode=enter_safe_mode)

    def verify_state(self):
        return verify_state_helper(self)

    def _bot_audit_latest_map(self, conn):
        return bot_audit_latest_map_helper(conn)

    def _bot_audit_label(self, status):
        return bot_audit_label(status)

    def _bot_audit_eligibility_reason_label(self, reason):
        return bot_audit_eligibility_reason_label(reason)

    def _bot_audit_enabled_at(self, row):
        return bot_audit_enabled_at_helper(row)

    def _bot_audit_is_eligible(self, row, *, bot_kind, min_enabled_seconds):
        return bot_audit_is_eligible_helper(
            row,
            bot_kind=bot_kind,
            min_enabled_seconds=min_enabled_seconds,
        )

    def _bot_audit_run_findings(self, conn, row, *, bot_kind, min_enabled_seconds):
        return bot_audit_run_findings_helper(
            self,
            conn,
            row,
            bot_kind=bot_kind,
            min_enabled_seconds=min_enabled_seconds,
        )

    def _record_bot_audit_run(self, conn, row, *, bot_kind, audit_result):
        return record_bot_audit_run_helper(
            self,
            conn,
            row,
            bot_kind=bot_kind,
            audit_result=audit_result,
        )

    def _bot_audit_candidates(self, conn, *, limit):
        return bot_audit_candidates_helper(conn, limit=limit)

    def _bot_audit_dashboard_on_conn(self, conn, *, limit, settings=None):
        return bot_audit_dashboard_on_conn_helper(self, conn, limit=limit, settings=settings)

    def run_due_bot_audits(self, *, actor=None, limit=0, force=False):
        return run_due_bot_audits_helper(self, actor=actor, limit=limit, force=force)

    def get_bot_audit_dashboard(self, *, limit=100):
        return get_bot_audit_dashboard_helper(self, limit=limit)

    def ensure_background_schema(self, conn=None):
        return ensure_background_schema_helper(self, conn)

    def get_background_status(self, *, limit=20):
        return get_background_status_helper(self, limit=limit)

    def enqueue_background_job_once(self, *, job_key, requested_by=None, force=True):
        return enqueue_background_job_once_helper(
            self,
            job_key=job_key,
            requested_by=requested_by,
            force=force,
        )

    def get_root_trading_snapshot(self, *, snapshot_key):
        return get_root_trading_snapshot_helper(self, snapshot_key=snapshot_key)

    def refresh_root_trading_snapshots(self, *, source_job_key="manual", source_run_uuid=""):
        return refresh_root_trading_snapshots_helper(
            self,
            source_job_key=source_job_key,
            source_run_uuid=source_run_uuid,
        )

    def run_background_job_once(
        self,
        *,
        job_key,
        get_system_settings=None,
        get_runtime_server_mode=None,
        owner=None,
        force=False,
    ):
        return run_background_job_once_helper(
            self,
            job_key=job_key,
            get_system_settings=get_system_settings,
            get_runtime_server_mode=get_runtime_server_mode,
            owner=owner,
            force=force,
        )

    def run_due_background_jobs(
        self,
        *,
        get_system_settings=None,
        get_runtime_server_mode=None,
        owner=None,
        job_keys=None,
        queued_max_jobs=None,
    ):
        return run_due_background_jobs_helper(
            self,
            get_system_settings=get_system_settings,
            get_runtime_server_mode=get_runtime_server_mode,
            owner=owner,
            job_keys=job_keys,
            queued_max_jobs=queued_max_jobs,
        )

    def set_background_job_enabled(self, *, job_key, enabled, reason="", actor=None):
        return set_background_job_enabled_helper(
            self,
            job_key=job_key,
            enabled=enabled,
            reason=reason,
            actor=actor,
        )

    def root_report(self):
        return root_report_helper(self)


from services.trading import engine_market_methods as _engine_market_methods

for _name in ('_market_registry_audit', '_market_registry_payload', '_market_provider_mapping_payload', '_validate_market_registry_payload', '_validate_market_provider_mapping_payload', '_probe_market_registry_on_conn', '_persist_market_registry_probe', 'list_market_registry', 'get_market_provider_registry', 'create_market_registry', 'update_market_registry', 'disable_market_registry', 'create_market_provider_mapping', 'update_market_provider_mapping', 'disable_market_provider_mapping', 'probe_market_registry', '_live_price_symbol', '_fetch_json_url', '_price_points_from_float', '_call_with_optional_conn', '_provider_ticker_with_fallback', '_provider_orderbook_with_fallback', '_fetch_binance_price_points', '_fetch_okx_price_points', '_fetch_coinbase_price_points', '_fetch_kraken_price_points', '_fetch_gemini_price_points', '_fetch_bitstamp_price_points', '_fetch_coingecko_price_points', '_price_fusion_depth_levels', '_price_fusion_depth_band_percent', '_price_fusion_min_orderbook_coverage_percent', '_price_fusion_provider_weight_cap_percent', '_price_fusion_min_provider_count', '_price_stream_ws_enabled', '_price_stream_ws_stale_seconds', '_price_stream_provider_state', '_provider_transport_meta', '_resolve_stream_ticker_snapshot', '_resolve_stream_orderbook_snapshot', '_provider_quantity_unit_info', '_price_fusion_warning', '_append_price_fusion_warning', '_primary_price_fusion_warning', '_price_usage_label', '_price_source_label', '_price_context_confidence', '_price_context_risk_grade_usable', '_build_price_context', '_attach_market_price_contexts', '_stored_market_price_contexts', '_price_fusion_effective_score', '_price_fusion_reference_score', '_price_fusion_warning_is_degrading', '_price_fusion_exclusion_is_degrading', '_transport_state_from_provider_rows', '_assert_price_meta_allows_high_risk_use', '_provider_depth_request_limit', '_parse_orderbook_side', '_depth_notional_snapshot', '_depth_notional_score', '_build_orderbook_snapshot', '_normalize_orderbook_fetch_result', '_okx_http_book_getter', '_kraken_http_book_getter', '_fetch_binance_orderbook_snapshot', '_fetch_okx_orderbook_snapshot', '_fetch_coinbase_orderbook_snapshot', '_fetch_kraken_orderbook_snapshot', '_fetch_gemini_orderbook_snapshot', '_fetch_bitstamp_orderbook_snapshot', '_price_fusion_manual_weights', '_apply_price_fusion_weight_cap', '_build_price_fusion_weight_model', '_fetch_weighted_fused_price_points', '_default_price_fusion_market_symbol', '_root_price_fusion_status_on_conn', 'get_root_price_fusion_status', 'get_live_market_quote', '_fetch_live_price_points', '_fetch_indicator_candles', '_parse_candle_time_ms', '_recent_price_window', '_workflow_live_context', '_current_market_price_points', '_ensure_market_price_snapshot_for_write', '_snapshot_market_price_points', '_root_sim_account', '_sim_delta', '_is_root_user_id', '_system_actor', '_trial_credit_row', '_ensure_trial_credit', '_trial_position', '_trial_delta', '_trial_lock_for_buy', '_trial_spend', '_trial_deploy', '_trial_unlock', '_set_trial_reclaim_blocked', '_clear_trial_reclaim_blocked', '_trial_mark_buy_executed', '_trial_allocate_sell', '_cancel_trial_reclaim_sell_orders', '_release_trial_margin_collateral', '_reclaim_trial_credit', '_funding_payload', '_position_payload', '_position_payload_with_metrics', '_futures_position_payload', '_margin_position_payload', '_margin_trade_records', '_borrowing_settings', '_assert_borrowing_enabled', '_minimum_margin_collateral_points', '_margin_interest_total_hours', '_margin_interest_due_points', '_margin_interest_due_micropoints', '_margin_interest_points', '_accrue_margin_interest', '_margin_risk_payload', '_margin_position_payload_with_risk', '_margin_free_margin_points', '_margin_account_payload', '_margin_summary_payload', '_margin_liquidation_order_key', '_margin_summary_payload_legacy', '_fill_payload', '_spot_realized_map', '_spot_fee_map', '_spot_summary_payload', '_notify_trade_filled', '_is_insufficient_error', '_notify_insufficient_balance', '_notify_margin_liquidated', '_has_unread_margin_alert', '_notify_margin_risk_alerts', 'list_markets', 'user_dashboard', 'user_asset_overview', '_is_executable', '_legacy_workflow', '_validate_workflow', '_validate_workflow_graph', '_validate_bot_payload', '_bot_payload_with_budget_meta', 'list_trading_bots', 'save_trading_bot', 'set_trading_bot_share_parameters', 'delete_trading_bot', 'increase_trading_bot_max_runs', 'adjust_trading_bot_budget', '_grid_levels', '_grid_quantity_units', '_grid_preview_fee_rates', '_grid_preview_risk', '_grid_preview_summary', 'preview_grid_bot', '_grid_bot_payload', 'create_grid_bot', 'list_grid_bots', 'set_grid_bot_share_parameters', 'toggle_grid_bot', 'delete_grid_bot', 'scan_grid_bots', '_scan_one_grid_bot', 'get_bot_competition', 'award_bot_competition_week'):
    setattr(TradingEngineService, _name, getattr(_engine_market_methods, _name))

del _engine_market_methods
del _name
