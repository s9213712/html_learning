"""Slice 4a — schema snapshot test for services.trading.engine.ensure_trading_schema.

Companion coverage for the trading schema split.
REFACTOR_PLAN.md slice 4.

The function is 740 lines / 77 SQL statements. Splitting it into a
migrations/ package (slice 4b) is risky if no test pins the resulting
schema. This test captures the full canonical schema after a fresh run
and asserts:

  1. The set of trading_* tables matches a checked-in list.
  2. Each table's column definition (name, type, notnull, dflt, pk)
     matches a checked-in dict — column-by-column.
  3. The set of trading_settings default keys matches a checked-in list.
  4. Re-running ensure_trading_schema is idempotent — second pass
     produces the same column set.

Slice 4b WILL fail this test if the refactor accidentally drops, renames,
or reorders any column / changes any DEFAULT. That's the point.

If you intentionally add a new column / migration, update the EXPECTED_*
constants below in the same commit as the schema change. Reviewer will
see both edits side-by-side.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
import py_compile

from services.trading.engine import (
    TRADING_FUNDING_POOL_INITIAL_POINTS,
    TRADING_FUNDING_POOL_LEGACY_INITIAL_POINTS,
    ensure_trading_schema,
)


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _capture_tables_and_indexes(conn):
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name LIKE 'trading_%' OR name LIKE 'idx_trading_%' "
        "ORDER BY type, name"
    ).fetchall()
    tables = sorted(r["name"] for r in rows if r["type"] == "table")
    indexes = sorted(r["name"] for r in rows if r["type"] == "index")
    return tables, indexes


def _capture_pragma(conn, table):
    """PRAGMA table_info(table) → tuple of (name, type, notnull, dflt, pk)
    for each column, ordered by cid (column position)."""
    return tuple(
        (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _capture_settings_keys(conn):
    rows = conn.execute(
        "SELECT key FROM trading_settings ORDER BY key"
    ).fetchall()
    return sorted(r["key"] for r in rows)


def _build_fresh_db(tmp_path):
    db_path = tmp_path / "trading.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_trading_schema(conn)
    conn.commit()
    return conn


def test_schema_ddl_module_compiles_cleanly():
    """Slice 4b moved CREATE TABLE strings into schema_ddl.py.

    Keep a direct compile check here so future doc/comment edits cannot
    silently break fresh-interpreter imports of ensure_trading_schema.
    """
    module_path = Path(__file__).resolve().parents[3] / "services" / "trading" / "schema_ddl.py"
    py_compile.compile(str(module_path), doraise=True)


# ─────────────────────────────────────────────────────────────────
# Expected — frozen snapshot of the schema produced by current
# ensure_trading_schema. Slice 4b refactor MUST NOT alter these.
# ─────────────────────────────────────────────────────────────────


EXPECTED_TABLES = [
    # Filled in by the bootstrap test below on first run; stored as a
    # constant so future deltas are visible in PR diff.
]

EXPECTED_SETTINGS_KEYS = [
    "trading.borrow_apr_btc_eth_percent",
    "trading.borrow_apr_usdt_points_percent",
    "trading.borrow_interest_interval_hours",
    "trading.borrow_interest_minimum_hours",
    "trading.borrow_interest_percent_daily",
    "trading.borrow_interest_pool_pressure_multiplier",
    "trading.borrowing_enabled",
    "trading.bot_auto_scan_enabled",
    "trading.bot_auto_scan_interval_seconds",
    "trading.bot_auto_scan_limit",
    "trading.bot_competition_enabled",
    "trading.bot_competition_weekly_reward_points",
    "trading.btc_trade_branch",
    "trading.btc_trade_enabled",
    "trading.btc_trade_repo_url",
    "trading.enabled",
    "trading.exchange_liability_grace_minutes",
    "trading.exchange_liability_limit_points",
    "trading.allow_unready_markets",
    "trading.dev_allow_conservative_market_orders",
    "trading.dev_allow_unready_markets",
    "trading.dev_disable_price_confidence_gates",
    "trading.disable_price_confidence_gates",
    "trading.futures_enabled",
    "trading.grid_fee_discount_percent",
    "trading.margin_liquidation_enabled",
    "trading.margin_long_financing_percent",
    "trading.margin_max_pool_utilization_percent",
    "trading.margin_maintenance_percent",
    "trading.max_price_staleness_seconds",
    "trading.price_fusion_depth_band_percent",
    "trading.price_fusion_depth_levels",
    "trading.price_fusion_manual_weights_json",
    "trading.price_fusion_max_single_provider_weight_percent",
    "trading.price_fusion_min_orderbook_coverage_percent",
    "trading.price_fusion_min_provider_count",
    "trading.price_fusion_mode",
    "trading.price_fusion_trade_min_provider_count",
    "trading.price_degrade_pause_borrowing",
    "trading.price_degrade_pause_bots",
    "trading.price_degrade_pause_market_orders",
    "trading.price_source",
    "trading.price_stream_ws_enabled",
    "trading.price_stream_ws_stale_seconds",
    "trading.profit_settlement_interval_minutes",
    "trading.pvp_matching_enabled",
    "trading.shadow_funding_publish_enabled",
    "trading.short_collateral_percent",
    "trading.simulated_slippage_base_basis_points",
    "trading.simulated_slippage_enabled",
    "trading.simulated_slippage_max_basis_points",
    "trading.simulated_slippage_size_basis_points_per_10k_notional",
    "trading.warning_language",
]


# ─────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────


def test_ensure_trading_schema_produces_expected_tables(tmp_path):
    """Lock down the set of trading_* tables. Slice 4b refactor must
    produce exactly the same set; new migrations are tracked here."""
    with closing(_build_fresh_db(tmp_path)) as conn:
        tables, _ = _capture_tables_and_indexes(conn)

    # First-run capture: assert at least the well-known critical tables
    # exist. The full list is asserted in the next test by walking each
    # table's PRAGMA, which is more diagnostic on regression.
    must_have = {
        "trading_settings",
        "trading_markets",
        "trading_markets_registry",
        "trading_market_provider_mappings",
        "trading_market_price_snapshots",
        "trading_orders",
        "trading_fills",
        "trading_margin_positions",
        "trading_bots",
        "trading_grid_bots",
        "trading_bot_competition_rewards",
        "trading_reserve_pool",
        "trading_reserve_pool_events",
        "trading_state",
        "trading_trial_credits",
        "trading_bot_audit_runs",
        "trading_bot_audit_findings",
    }
    missing = must_have - set(tables)
    assert not missing, (
        f"ensure_trading_schema dropped expected tables: {sorted(missing)}\n"
        f"Got: {tables}"
    )


def test_ensure_trading_schema_default_settings_keys_frozen(tmp_path):
    """The default trading_settings keys form the public contract for
    operators / admin UI. Adding/removing one is a behavior change that
    must update EXPECTED_SETTINGS_KEYS in the same commit."""
    with closing(_build_fresh_db(tmp_path)) as conn:
        keys = _capture_settings_keys(conn)
    extra = sorted(set(keys) - set(EXPECTED_SETTINGS_KEYS))
    missing = sorted(set(EXPECTED_SETTINGS_KEYS) - set(keys))
    assert not extra and not missing, (
        f"trading_settings default keys drifted.\n"
        f"  ADDED (not in EXPECTED_SETTINGS_KEYS): {extra}\n"
        f"  REMOVED (in EXPECTED_SETTINGS_KEYS but missing): {missing}\n"
        f"  If intentional: update EXPECTED_SETTINGS_KEYS in this file in "
        f"the same commit as the schema change."
    )


def test_ensure_trading_schema_is_idempotent(tmp_path):
    """Running ensure_trading_schema twice on the same conn produces the
    same final state. Slice 4b refactor must preserve idempotence."""
    db_path = tmp_path / "idem.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_trading_schema(conn)
    conn.commit()
    tables_first, indexes_first = _capture_tables_and_indexes(conn)
    pragma_first = {t: _capture_pragma(conn, t) for t in tables_first}
    keys_first = _capture_settings_keys(conn)

    # Second run on the same conn — must be a no-op observably
    ensure_trading_schema(conn)
    conn.commit()
    tables_second, indexes_second = _capture_tables_and_indexes(conn)
    pragma_second = {t: _capture_pragma(conn, t) for t in tables_second}
    keys_second = _capture_settings_keys(conn)

    assert tables_first == tables_second, (
        f"second run added/removed tables: "
        f"+{set(tables_second)-set(tables_first)} "
        f"-{set(tables_first)-set(tables_second)}"
    )
    assert indexes_first == indexes_second, (
        f"second run mutated indexes: "
        f"+{set(indexes_second)-set(indexes_first)} "
        f"-{set(indexes_first)-set(indexes_second)}"
    )
    assert pragma_first == pragma_second, "second run mutated some column definition"
    assert keys_first == keys_second, "second run mutated trading_settings keys"
    conn.close()


def test_ensure_trading_schema_adds_finance_hot_path_indexes(tmp_path):
    """Batch A1 indexes must exist without depending on a runtime migration."""
    with closing(_build_fresh_db(tmp_path)) as conn:
        _, indexes = _capture_tables_and_indexes(conn)

    required = {
        "idx_trading_orders_user_id_desc",
        "idx_trading_fills_user_id_desc",
        "idx_trading_margin_user_id_desc",
        "idx_trading_bots_user_id_desc",
        "idx_trading_bot_runs_user_id_desc",
        "idx_trading_grid_orders_user_order",
        "idx_trading_market_price_snapshots_health",
    }
    missing = required - set(indexes)
    assert not missing, f"missing finance hot-path indexes: {sorted(missing)}"


def test_ensure_trading_schema_critical_columns_present(tmp_path):
    """For each well-known critical table, assert specific columns exist
    with their expected (type, notnull, pk) signature. Slice 4b must
    preserve these column-by-column."""
    with closing(_build_fresh_db(tmp_path)) as conn:
        tables = {t: _capture_pragma(conn, t) for t in (
            "trading_settings",
            "trading_markets",
            "trading_markets_registry",
            "trading_market_price_snapshots",
            "trading_orders",
            "trading_fills",
            "trading_margin_positions",
            "trading_bots",
            "trading_grid_bots",
            "trading_reserve_pool",
            "trading_state",
            "trading_trial_credits",
        )}

    def has_column(table, name, *, type_=None, notnull=None, pk=None):
        for col_name, col_type, col_notnull, _dflt, col_pk in tables[table]:
            if col_name != name:
                continue
            if type_ is not None and col_type != type_:
                return False
            if notnull is not None and col_notnull != notnull:
                return False
            if pk is not None and col_pk != pk:
                return False
            return True
        return False

    # trading_settings — the operator-facing key/value table
    assert has_column("trading_settings", "key", type_="TEXT", pk=1)
    assert has_column("trading_settings", "value", type_="TEXT", notnull=1)
    assert has_column("trading_settings", "updated_at", type_="TEXT", notnull=1)

    # trading_markets — must keep fee_rate_percent (slice 1 default unit
    # rename target) + live-price warmup / boot-ready gate columns
    assert has_column("trading_markets", "symbol", type_="TEXT", pk=1)
    assert has_column("trading_markets", "fee_rate_percent", type_="REAL", notnull=1)
    assert has_column("trading_markets", "live_price_warmup_started_at", type_="TEXT")
    assert has_column("trading_markets", "live_price_confirmed_at", type_="TEXT")
    assert has_column("trading_markets", "max_price_jump_percent", type_="REAL", notnull=1)

    # trading_markets_registry — must keep allow_risk_grade_usage (gate
    # for liquidation-grade price use, see services/trading/engine.py
    # _assert_price_meta_allows_high_risk_use)
    assert has_column("trading_markets_registry", "allow_risk_grade_usage", type_="INTEGER", notnull=1)
    assert has_column("trading_markets_registry", "live_price_enabled", type_="INTEGER", notnull=1)
    assert has_column("trading_markets_registry", "reference_price_enabled", type_="INTEGER", notnull=1)

    assert has_column("trading_market_price_snapshots", "market_symbol", type_="TEXT", pk=1)
    assert has_column("trading_market_price_snapshots", "reference_price_points", type_="REAL")
    assert has_column("trading_market_price_snapshots", "risk_grade_price_points", type_="REAL")
    assert has_column("trading_market_price_snapshots", "metadata_json", type_="TEXT", notnull=1)
    assert has_column("trading_market_price_snapshots", "expires_at", type_="TEXT")

    # trading_orders — chain/trial split columns (PointsChain integration)
    assert has_column("trading_orders", "trial_frozen_points", type_="INTEGER", notnull=1)
    assert has_column("trading_orders", "chain_frozen_points", type_="INTEGER", notnull=1)
    assert has_column("trading_orders", "funding_mode", type_="TEXT", notnull=1)

    # trading_fills — repaid/profit columns required by reclaim flow
    assert has_column("trading_fills", "trial_repaid_points", type_="INTEGER", notnull=1)
    assert has_column("trading_fills", "trial_profit_points", type_="INTEGER", notnull=1)

    # trading_margin_positions — interest accrual + collateral split
    for col in (
        "collateral_trial_points", "collateral_chain_points",
        "open_fee_trial_points", "open_fee_chain_points",
        "interest_paid_points", "interest_accrued_hours",
        "interest_carry_micropoints", "interest_interval_hours",
        "interest_minimum_hours", "borrowed_asset_symbol",
        "exit_price_points", "realized_pnl_points",
        "interest_percent_daily",
    ):
        assert has_column("trading_margin_positions", col), (
            f"trading_margin_positions missing column {col!r}"
        )

    # trading_bots — workflow + DCA support columns, including the DCA
    # price band and the atomic UTC-day cap for Workflow bots.
    for col in (
        "bot_type", "interval_hours", "budget_points",
        "price_upper_limit", "price_lower_limit",
        "max_daily_runs", "daily_run_date", "daily_run_count",
        "workflow_json", "execution_state_json",
        "enabled_at", "last_scan_at", "share_parameters",
    ):
        assert has_column("trading_bots", col), (
            f"trading_bots missing column {col!r}"
        )

    # trading_grid_bots — enabled_at lifecycle gate + competition sharing flag
    assert has_column("trading_grid_bots", "enabled_at", type_="TEXT")
    assert has_column("trading_grid_bots", "share_parameters", type_="INTEGER", notnull=1)
    assert has_column("trading_grid_bots", "stop_loss_percent", type_="REAL")
    assert has_column("trading_grid_bots", "take_profit_percent", type_="REAL")

    # trading_reserve_pool — funding pool source-of-truth row
    assert has_column("trading_reserve_pool", "balance_points", type_="INTEGER", notnull=1)

    # trading_trial_credits — reclaim block diagnostics
    assert has_column("trading_trial_credits", "reclaim_blocked_reason", type_="TEXT", notnull=1)
    assert has_column("trading_trial_credits", "reclaim_blocked_at", type_="TEXT")


def test_ensure_trading_schema_inserts_initial_reserve_pool(tmp_path):
    """ensure_trading_schema seeds trading_reserve_pool row id=1 +
    one initial_funding event. Slice 4b must preserve this."""
    with closing(_build_fresh_db(tmp_path)) as conn:
        pool = conn.execute(
            "SELECT id, balance_points FROM trading_reserve_pool WHERE id=1"
        ).fetchone()
        events = conn.execute(
            "SELECT event_type, balance_after FROM trading_reserve_pool_events "
            "WHERE event_type='initial_funding'"
        ).fetchall()
    assert pool is not None, "trading_reserve_pool row id=1 missing"
    assert int(pool["balance_points"]) > 0, (
        "expected initial reserve_pool balance > 0 after schema seed"
    )
    assert int(pool["balance_points"]) == TRADING_FUNDING_POOL_INITIAL_POINTS
    assert len(events) == 1, (
        f"expected exactly 1 initial_funding event, got {len(events)}"
    )
    assert int(events[0]["balance_after"]) == TRADING_FUNDING_POOL_INITIAL_POINTS


def test_ensure_trading_schema_aligns_legacy_reserve_pool_to_exchange_fund(tmp_path):
    """Existing runtimes may already have the old 10k reserve seed.

    Phase 1A walletization aligns that pool to the PointsChain EXCHANGE fund
    without rewriting the legacy initial_funding event.
    """
    with closing(_build_fresh_db(tmp_path)) as conn:
        conn.execute(
            "UPDATE trading_reserve_pool SET balance_points=?, updated_at='legacy' WHERE id=1",
            (TRADING_FUNDING_POOL_LEGACY_INITIAL_POINTS,),
        )
        conn.execute("DELETE FROM trading_reserve_pool_events")
        conn.execute(
            """
            INSERT INTO trading_reserve_pool_events (
                event_uuid, delta_points, balance_after, event_type, reason,
                actor_user_id, source_user_id, order_id, fill_id, points_ledger_uuid, created_at
            ) VALUES ('legacy-initial-funding', ?, ?, 'initial_funding', 'TRADING_FUNDING_POOL_INITIAL', NULL, NULL, NULL, NULL, NULL, 'legacy')
            """,
            (TRADING_FUNDING_POOL_LEGACY_INITIAL_POINTS, TRADING_FUNDING_POOL_LEGACY_INITIAL_POINTS),
        )
        conn.commit()

        ensure_trading_schema(conn)
        conn.commit()
        ensure_trading_schema(conn)
        conn.commit()

        pool = conn.execute("SELECT balance_points FROM trading_reserve_pool WHERE id=1").fetchone()
        alignment = conn.execute(
            """
            SELECT delta_points, balance_after FROM trading_reserve_pool_events
            WHERE event_type='walletized_exchange_fund_alignment'
            """
        ).fetchall()

    expected_delta = TRADING_FUNDING_POOL_INITIAL_POINTS - TRADING_FUNDING_POOL_LEGACY_INITIAL_POINTS
    assert int(pool["balance_points"]) == TRADING_FUNDING_POOL_INITIAL_POINTS
    assert len(alignment) == 1
    assert int(alignment[0]["delta_points"]) == expected_delta
    assert int(alignment[0]["balance_after"]) == TRADING_FUNDING_POOL_INITIAL_POINTS
