"""Trading schema CREATE TABLE statements.

Slice 4b extraction: pure DDL strings used by
`services.trading.engine.ensure_trading_schema` to create the 28 trading
tables on a fresh DB. Lifting these into a constants module shrinks
`ensure_trading_schema` from 740 lines to ~280 lines and makes new
table definitions reviewable in isolation.

Behavior contract — slice 4b promise:
  - Each constant is the **byte-for-byte identical** SQL string that
    used to live inline. Whitespace inside the SQL is preserved so the
    schema snapshot test (`tests/test_trading_schema_snapshot.py`)
    passes unchanged.
  - The order of `ALL_TABLE_DDL` matches the historical execution order
    in ensure_trading_schema. Some tables reference others via FOREIGN
    KEY, so reordering would break creation on a fresh DB.
  - Every CREATE TABLE uses `IF NOT EXISTS` so re-running is idempotent.

Adding a new table:
  1. Add a `TRADING_<NAME>_DDL = <multiline SQL>` constant below.
  2. Append it to `ALL_TABLE_DDL` at the correct position (respecting
     FOREIGN KEY dependencies).
  3. Update `tests/test_trading_schema_snapshot.py` (must_have set
     and EXPECTED_SETTINGS_KEYS / column expectations as needed).
  4. Same commit as the schema change.

Imperative migrations (PRAGMA-guarded ALTER TABLE, legacy unit renames,
default trading_settings INSERT OR IGNORE, registry catalog seed) stay
in `engine.py` because they need shared state (`now`, helpers like
`_seed_market_registry_from_catalog`). Splitting those is slice 4c.
"""


TRADING_SETTINGS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by INTEGER
        )
        """


TRADING_MARKETS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_markets (
            symbol TEXT PRIMARY KEY,
            base_asset TEXT NOT NULL,
            quote_currency TEXT NOT NULL DEFAULT 'POINTS',
            enabled INTEGER NOT NULL DEFAULT 1,
            spot_enabled INTEGER NOT NULL DEFAULT 1,
            futures_enabled INTEGER NOT NULL DEFAULT 0,
            pvp_matching_enabled INTEGER NOT NULL DEFAULT 0,
            execution_mode TEXT NOT NULL DEFAULT 'house_counterparty',
            manual_price_points INTEGER NOT NULL CHECK (manual_price_points >= 0),
            max_price_jump_percent REAL NOT NULL DEFAULT 10,
            min_order_points INTEGER NOT NULL DEFAULT 1,
            max_order_points INTEGER NOT NULL DEFAULT 100000,
            fee_rate_percent REAL NOT NULL DEFAULT 0.1,
            updated_at TEXT NOT NULL,
            updated_by INTEGER,
            price_source TEXT NOT NULL DEFAULT 'binance_public_api',
            live_price_warmup_started_at TEXT,
            live_price_confirmed_at TEXT,
            CHECK (execution_mode IN ('house_counterparty', 'pvp_matching', 'hybrid_liquidity'))
        )
        """


TRADING_MARKETS_REGISTRY_DDL = """
        CREATE TABLE IF NOT EXISTS trading_markets_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL UNIQUE,
            base_asset TEXT NOT NULL,
            quote_asset TEXT NOT NULL DEFAULT 'POINTS',
            display_name TEXT NOT NULL,
            display_quote_currency TEXT NOT NULL DEFAULT 'USDT',
            market_type TEXT NOT NULL DEFAULT 'spot',
            enabled INTEGER NOT NULL DEFAULT 1,
            allow_spot INTEGER NOT NULL DEFAULT 1,
            allow_margin INTEGER NOT NULL DEFAULT 1,
            allow_bots INTEGER NOT NULL DEFAULT 1,
            allow_risk_grade_usage INTEGER NOT NULL DEFAULT 1,
            price_precision INTEGER NOT NULL DEFAULT 8,
            quantity_precision INTEGER NOT NULL DEFAULT 8,
            min_order_size REAL NOT NULL DEFAULT 0.00000001,
            max_order_size REAL NOT NULL DEFAULT 1000000,
            lot_size REAL NOT NULL DEFAULT 0.00000001,
            tick_size REAL NOT NULL DEFAULT 0.00000001,
            sort_order INTEGER NOT NULL DEFAULT 9999,
            default_manual_price_points REAL NOT NULL DEFAULT 1,
            live_price_enabled INTEGER NOT NULL DEFAULT 1,
            reference_price_enabled INTEGER NOT NULL DEFAULT 1,
            btc_trade_enabled INTEGER NOT NULL DEFAULT 0,
            registry_source TEXT NOT NULL DEFAULT 'catalog_seed',
            seed_version INTEGER NOT NULL DEFAULT 1,
            probe_status TEXT NOT NULL DEFAULT 'pending',
            probe_summary_json TEXT NOT NULL DEFAULT '{}',
            probe_checked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_by INTEGER,
            updated_by INTEGER,
            CHECK (market_type IN ('spot', 'synthetic', 'reference_only')),
            CHECK (enabled IN (0, 1)),
            CHECK (allow_spot IN (0, 1)),
            CHECK (allow_margin IN (0, 1)),
            CHECK (allow_bots IN (0, 1)),
            CHECK (allow_risk_grade_usage IN (0, 1)),
            CHECK (live_price_enabled IN (0, 1)),
            CHECK (reference_price_enabled IN (0, 1)),
            CHECK (btc_trade_enabled IN (0, 1)),
            CHECK (registry_source IN ('catalog_seed', 'custom'))
        )
        """


TRADING_MARKET_PROVIDER_MAPPINGS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_market_provider_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id INTEGER NOT NULL REFERENCES trading_markets_registry(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_symbol TEXT NOT NULL DEFAULT '',
            supports_ticker INTEGER NOT NULL DEFAULT 0,
            supports_depth INTEGER NOT NULL DEFAULT 0,
            supports_candles INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 100,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (market_id, provider)
        )
        """


TRADING_MARKET_PRICE_SNAPSHOTS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_market_price_snapshots (
            market_symbol TEXT PRIMARY KEY,
            reference_price_points REAL,
            risk_grade_price_points REAL,
            resolved_source TEXT NOT NULL DEFAULT '',
            price_health TEXT NOT NULL DEFAULT 'unknown',
            confidence TEXT NOT NULL DEFAULT 'unknown',
            reference_provider_count INTEGER NOT NULL DEFAULT 0,
            risk_grade_provider_count INTEGER NOT NULL DEFAULT 0,
            high_risk_blocked INTEGER NOT NULL DEFAULT 1 CHECK (high_risk_blocked IN (0,1)),
            high_risk_block_reason TEXT NOT NULL DEFAULT '',
            degraded INTEGER NOT NULL DEFAULT 0 CHECK (degraded IN (0,1)),
            stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0,1)),
            fallback INTEGER NOT NULL DEFAULT 0 CHECK (fallback IN (0,1)),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            fetched_at TEXT,
            expires_at TEXT,
            stale_until TEXT,
            updated_at TEXT NOT NULL
        )
        """


TRADING_MARKET_REGISTRY_AUDIT_DDL = """
        CREATE TABLE IF NOT EXISTS trading_market_registry_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER,
            action TEXT NOT NULL,
            market_symbol TEXT NOT NULL,
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """


TRADING_ORDERS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL REFERENCES trading_markets(symbol),
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            funding_mode TEXT NOT NULL DEFAULT 'points_chain',
            source_wallet_address TEXT NOT NULL DEFAULT '',
            execution_mode TEXT NOT NULL DEFAULT 'house_counterparty',
            quantity_units INTEGER NOT NULL CHECK (quantity_units > 0),
            limit_price_points INTEGER,
            execution_price_points INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            frozen_points INTEGER NOT NULL DEFAULT 0,
            trial_frozen_points INTEGER NOT NULL DEFAULT 0 CHECK (trial_frozen_points >= 0),
            chain_frozen_points INTEGER NOT NULL DEFAULT 0 CHECK (chain_frozen_points >= 0),
            fee_points INTEGER NOT NULL DEFAULT 0,
            fee_micropoints INTEGER NOT NULL DEFAULT 0 CHECK (fee_micropoints >= 0),
            filled_quantity_units INTEGER NOT NULL DEFAULT 0,
            stop_loss_percent REAL,
            take_profit_percent REAL,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (side IN ('buy', 'sell')),
            CHECK (order_type IN ('market', 'limit')),
            CHECK (status IN ('open', 'partially_filled', 'filled', 'cancelled', 'rejected')),
            CHECK (execution_mode IN ('house_counterparty', 'pvp_matching', 'hybrid_liquidity'))
        )
        """


TRADING_FILLS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fill_uuid TEXT NOT NULL UNIQUE,
            order_id INTEGER NOT NULL REFERENCES trading_orders(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            funding_mode TEXT NOT NULL DEFAULT 'points_chain',
            quantity_units INTEGER NOT NULL CHECK (quantity_units > 0),
            price_points INTEGER NOT NULL CHECK (price_points > 0),
            notional_points INTEGER NOT NULL CHECK (notional_points >= 0),
            fee_points INTEGER NOT NULL DEFAULT 0,
            fee_micropoints INTEGER NOT NULL DEFAULT 0 CHECK (fee_micropoints >= 0),
            reserve_delta_points INTEGER NOT NULL DEFAULT 0,
            trial_repaid_points INTEGER NOT NULL DEFAULT 0 CHECK (trial_repaid_points >= 0),
            trial_profit_points INTEGER NOT NULL DEFAULT 0 CHECK (trial_profit_points >= 0),
            points_ledger_uuids_json TEXT,
            created_at TEXT NOT NULL,
            CHECK (side IN ('buy', 'sell'))
        )
        """


TRADING_SPOT_REALIZED_PNL_DDL = """
        CREATE TABLE IF NOT EXISTS trading_spot_realized_pnl (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pnl_uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL,
            order_id INTEGER NOT NULL REFERENCES trading_orders(id) ON DELETE CASCADE,
            fill_id INTEGER NOT NULL UNIQUE REFERENCES trading_fills(id) ON DELETE CASCADE,
            funding_mode TEXT NOT NULL DEFAULT 'points_chain',
            quantity_units INTEGER NOT NULL CHECK (quantity_units > 0),
            avg_cost_points INTEGER NOT NULL DEFAULT 0,
            sell_price_points INTEGER NOT NULL CHECK (sell_price_points > 0),
            gross_cost_points INTEGER NOT NULL DEFAULT 0,
            gross_proceeds_points INTEGER NOT NULL DEFAULT 0,
            buy_fee_estimate_points INTEGER NOT NULL DEFAULT 0,
            sell_fee_points INTEGER NOT NULL DEFAULT 0,
            buy_fee_micropoints INTEGER NOT NULL DEFAULT 0 CHECK (buy_fee_micropoints >= 0),
            sell_fee_micropoints INTEGER NOT NULL DEFAULT 0 CHECK (sell_fee_micropoints >= 0),
            settled_fee_micropoints INTEGER NOT NULL DEFAULT 0 CHECK (settled_fee_micropoints >= 0),
            net_pnl_points INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """


TRADING_SIM_ACCOUNTS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_sim_accounts (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            balance_points INTEGER NOT NULL DEFAULT 0 CHECK (balance_points >= 0),
            locked_points INTEGER NOT NULL DEFAULT 0 CHECK (locked_points >= 0),
            initial_balance_points INTEGER NOT NULL DEFAULT 10000,
            updated_at TEXT NOT NULL,
            reset_at TEXT,
            reset_by INTEGER
        )
        """


TRADING_TRIAL_CREDITS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_trial_credits (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            initial_points INTEGER NOT NULL DEFAULT 1000 CHECK (initial_points >= 0),
            available_points INTEGER NOT NULL DEFAULT 0 CHECK (available_points >= 0),
            locked_points INTEGER NOT NULL DEFAULT 0 CHECK (locked_points >= 0),
            deployed_points INTEGER NOT NULL DEFAULT 0 CHECK (deployed_points >= 0),
            status TEXT NOT NULL DEFAULT 'active',
            activated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            reclaimed_at TEXT,
            reclaim_blocked_reason TEXT NOT NULL DEFAULT '',
            reclaim_blocked_at TEXT,
            updated_at TEXT NOT NULL,
            CHECK (status IN ('active', 'expired', 'depleted', 'reclaimed'))
        )
        """


TRADING_TRIAL_POSITION_COSTS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_trial_position_costs (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL REFERENCES trading_markets(symbol),
            quantity_units INTEGER NOT NULL DEFAULT 0 CHECK (quantity_units >= 0),
            trial_cost_points INTEGER NOT NULL DEFAULT 0 CHECK (trial_cost_points >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, market_symbol)
        )
        """


TRADING_OPERATION_IDEMPOTENCY_DDL = """
        CREATE TABLE IF NOT EXISTS trading_operation_idempotency (
            idempotency_key TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            reference_uuid TEXT,
            response_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """


TRADING_SPOT_POSITIONS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_spot_positions (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL REFERENCES trading_markets(symbol),
            quantity_units INTEGER NOT NULL DEFAULT 0 CHECK (quantity_units >= 0),
            locked_quantity_units INTEGER NOT NULL DEFAULT 0 CHECK (locked_quantity_units >= 0),
            avg_cost_points INTEGER NOT NULL DEFAULT 0,
            fee_carry_micropoints INTEGER NOT NULL DEFAULT 0 CHECK (fee_carry_micropoints >= 0),
            source_wallet_address TEXT NOT NULL DEFAULT '',
            funding_sources_json TEXT NOT NULL DEFAULT '[]',
            taint_status TEXT NOT NULL DEFAULT 'normal',
            taint_source_tx_hash TEXT NOT NULL DEFAULT '',
            stop_loss_percent REAL,
            take_profit_percent REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, market_symbol)
        )
        """


TRADING_FUTURES_POSITIONS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_futures_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity_units INTEGER NOT NULL,
            entry_price_points INTEGER NOT NULL,
            leverage INTEGER NOT NULL DEFAULT 1,
            margin_points INTEGER NOT NULL,
            liquidation_price_points INTEGER,
            status TEXT NOT NULL DEFAULT 'disabled',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (status IN ('disabled', 'open', 'closed', 'liquidated'))
        )
        """


TRADING_MARGIN_POSITIONS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_margin_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL,
            position_type TEXT NOT NULL,
            quantity_units INTEGER NOT NULL CHECK (quantity_units > 0),
            entry_price_points INTEGER NOT NULL CHECK (entry_price_points > 0),
            principal_points INTEGER NOT NULL DEFAULT 0 CHECK (principal_points >= 0),
            collateral_points INTEGER NOT NULL CHECK (collateral_points > 0),
            open_fee_points INTEGER NOT NULL DEFAULT 0,
            close_fee_points INTEGER NOT NULL DEFAULT 0,
            open_fee_micropoints INTEGER NOT NULL DEFAULT 0 CHECK (open_fee_micropoints >= 0),
            close_fee_micropoints INTEGER NOT NULL DEFAULT 0 CHECK (close_fee_micropoints >= 0),
            stop_loss_percent REAL,
            take_profit_percent REAL,
            exit_price_points INTEGER,
            realized_pnl_points INTEGER NOT NULL DEFAULT 0,
            interest_percent_daily REAL NOT NULL DEFAULT 0,
            interest_points INTEGER NOT NULL DEFAULT 0,
            interest_paid_points INTEGER NOT NULL DEFAULT 0,
            interest_accrued_hours INTEGER NOT NULL DEFAULT 0,
            interest_carry_micropoints INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            updated_at TEXT NOT NULL,
            collateral_trial_points INTEGER NOT NULL DEFAULT 0 CHECK (collateral_trial_points >= 0),
            collateral_chain_points INTEGER NOT NULL DEFAULT 0 CHECK (collateral_chain_points >= 0),
            open_fee_trial_points INTEGER NOT NULL DEFAULT 0 CHECK (open_fee_trial_points >= 0),
            open_fee_chain_points INTEGER NOT NULL DEFAULT 0 CHECK (open_fee_chain_points >= 0),
            CHECK (position_type IN ('margin_long', 'short')),
            CHECK (status IN ('open', 'closed', 'liquidated'))
        )
        """


TRADING_PENDING_PROFIT_DDL = """
        CREATE TABLE IF NOT EXISTS trading_pending_profit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL,
            amount_points INTEGER NOT NULL CHECK (amount_points > 0),
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            position_uuid TEXT NOT NULL DEFAULT '',
            governance_proposal_uuid TEXT NOT NULL DEFAULT '',
            liability_policy_json TEXT NOT NULL DEFAULT '{}',
            settle_not_before_at TEXT,
            created_at TEXT NOT NULL,
            released_at TEXT,
            CHECK (status IN ('pending', 'released', 'rejected'))
        )
        """


TRADING_RESERVE_POOL_DDL = """
        CREATE TABLE IF NOT EXISTS trading_reserve_pool (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            balance_points INTEGER NOT NULL DEFAULT 0 CHECK (balance_points >= 0),
            updated_at TEXT NOT NULL,
            updated_by INTEGER
        )
        """


TRADING_RESERVE_POOL_EVENTS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_reserve_pool_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE,
            delta_points INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            reason TEXT,
            actor_user_id INTEGER,
            source_user_id INTEGER,
            order_id INTEGER,
            fill_id INTEGER,
            points_ledger_uuid TEXT,
            created_at TEXT NOT NULL
        )
        """


TRADING_USER_VOLUME_STATS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_user_volume_stats (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            total_notional_points INTEGER NOT NULL DEFAULT 0 CHECK (total_notional_points >= 0),
            spot_notional_points INTEGER NOT NULL DEFAULT 0 CHECK (spot_notional_points >= 0),
            margin_notional_points INTEGER NOT NULL DEFAULT 0 CHECK (margin_notional_points >= 0),
            total_fee_points INTEGER NOT NULL DEFAULT 0 CHECK (total_fee_points >= 0),
            total_trade_count INTEGER NOT NULL DEFAULT 0 CHECK (total_trade_count >= 0),
            last_trade_at TEXT,
            updated_at TEXT NOT NULL
        )
        """


TRADING_AUDIT_EVENTS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            actor_user_id INTEGER,
            target_user_id INTEGER,
            order_id INTEGER,
            market_symbol TEXT,
            message TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL
        )
        """


TRADING_STATE_DDL = """
        CREATE TABLE IF NOT EXISTS trading_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            safe_mode INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            verification_json TEXT,
            updated_at TEXT NOT NULL,
            updated_by INTEGER
        )
        """


TRADING_BOTS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bot_type TEXT NOT NULL DEFAULT 'conditional',
            name TEXT NOT NULL,
            market_symbol TEXT NOT NULL REFERENCES trading_markets(symbol),
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            quantity_text TEXT NOT NULL,
            limit_price_points INTEGER,
            trigger_type TEXT NOT NULL DEFAULT 'price_below',
            trigger_price_points INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_runs INTEGER NOT NULL DEFAULT 1,
            run_count INTEGER NOT NULL DEFAULT 0,
            cooldown_seconds INTEGER NOT NULL DEFAULT 300,
            interval_hours INTEGER NOT NULL DEFAULT 24,
            budget_points INTEGER NOT NULL DEFAULT 0,
            price_upper_limit REAL,
            price_lower_limit REAL,
            max_daily_runs INTEGER NOT NULL DEFAULT 0,
            daily_run_date TEXT,
            daily_run_count INTEGER NOT NULL DEFAULT 0,
            stop_loss_percent REAL,
            take_profit_percent REAL,
            share_parameters INTEGER NOT NULL DEFAULT 0,
            workflow_json TEXT,
            execution_state_json TEXT,
            last_run_at TEXT,
            last_error TEXT,
            enabled_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (bot_type IN ('conditional', 'dca')),
            CHECK (side IN ('buy', 'sell')),
            CHECK (order_type IN ('market', 'limit')),
            CHECK (trigger_type IN ('always', 'price_above', 'price_below')),
            CHECK (max_runs >= 1),
            CHECK (run_count >= 0),
            CHECK (cooldown_seconds >= 0),
            CHECK (interval_hours >= 1),
            CHECK (budget_points >= 0),
            CHECK (max_daily_runs >= 0 AND max_daily_runs <= 100),
            CHECK (daily_run_count >= 0),
            CHECK (price_lower_limit IS NULL OR price_upper_limit IS NULL OR price_lower_limit <= price_upper_limit)
        )
        """


TRADING_BOT_RUNS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_bot_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_uuid TEXT NOT NULL UNIQUE,
            bot_id INTEGER NOT NULL REFERENCES trading_bots(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            trigger_price_points INTEGER,
            observed_price_points INTEGER,
            status TEXT NOT NULL,
            order_uuid TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            CHECK (status IN ('triggered', 'skipped', 'failed'))
        )
        """


TRADING_GRID_BOTS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_grid_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            market_symbol TEXT NOT NULL REFERENCES trading_markets(symbol),
            upper_price_points INTEGER NOT NULL CHECK (upper_price_points > 0),
            lower_price_points INTEGER NOT NULL CHECK (lower_price_points > 0),
            grid_count INTEGER NOT NULL CHECK (grid_count >= 2 AND grid_count <= 200),
            order_amount_points INTEGER NOT NULL CHECK (order_amount_points > 0),
            enabled INTEGER NOT NULL DEFAULT 1,
            total_profit_points INTEGER NOT NULL DEFAULT 0,
            total_trades INTEGER NOT NULL DEFAULT 0,
            initial_price_points INTEGER NOT NULL DEFAULT 0,
            grid_levels_json TEXT,
            stop_loss_percent REAL,
            take_profit_percent REAL,
            share_parameters INTEGER NOT NULL DEFAULT 0,
            last_scan_at TEXT,
            last_error TEXT,
            enabled_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (upper_price_points > lower_price_points)
        )
        """


TRADING_GRID_ORDERS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_grid_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_uuid TEXT NOT NULL UNIQUE,
            grid_bot_id INTEGER NOT NULL REFERENCES trading_grid_bots(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            level_index INTEGER NOT NULL,
            price_points INTEGER NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
            trading_order_uuid TEXT,
            filled_quantity_units INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'filled', 'cancelled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """


TRADING_BOT_COMPETITION_REWARDS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_bot_competition_rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_key TEXT NOT NULL,
            category TEXT NOT NULL,
            bot_kind TEXT NOT NULL,
            bot_uuid TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            performance_percent REAL NOT NULL DEFAULT 0,
            pnl_points INTEGER NOT NULL DEFAULT 0,
            principal_points INTEGER NOT NULL DEFAULT 0,
            reward_points INTEGER NOT NULL DEFAULT 0,
            ledger_uuid TEXT,
            awarded_by INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE (week_key, category),
            CHECK (category IN ('dca', 'workflow', 'grid')),
            CHECK (bot_kind IN ('trading_bot', 'grid_bot')),
            CHECK (rank >= 1),
            CHECK (reward_points >= 0)
        )
        """


TRADING_BOT_AUDIT_RUNS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_bot_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_uuid TEXT NOT NULL UNIQUE,
            bot_kind TEXT NOT NULL CHECK (bot_kind IN ('trading_bot', 'grid_bot')),
            bot_uuid TEXT NOT NULL,
            bot_id INTEGER,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_symbol TEXT NOT NULL,
            audit_status TEXT NOT NULL CHECK (audit_status IN ('green', 'yellow', 'red')),
            eligible_reason TEXT NOT NULL,
            findings_json TEXT,
            finding_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            blocker_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """


TRADING_BOT_AUDIT_FINDINGS_DDL = """
        CREATE TABLE IF NOT EXISTS trading_bot_audit_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES trading_bot_audit_runs(id) ON DELETE CASCADE,
            severity TEXT NOT NULL CHECK (severity IN ('warning', 'blocker')),
            code TEXT NOT NULL,
            message TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL
        )
        """


# Order matters: tables with FOREIGN KEY references must come after the
# referenced table.  This sequence mirrors the historical order in
# `services.trading.engine.ensure_trading_schema` exactly so the schema
# snapshot test (tests/test_trading_schema_snapshot.py) sees no drift.
ALL_TABLE_DDL = (
    TRADING_SETTINGS_DDL,
    TRADING_MARKETS_DDL,
    TRADING_MARKETS_REGISTRY_DDL,
    TRADING_MARKET_PROVIDER_MAPPINGS_DDL,
    TRADING_MARKET_PRICE_SNAPSHOTS_DDL,
    TRADING_MARKET_REGISTRY_AUDIT_DDL,
    TRADING_ORDERS_DDL,
    TRADING_FILLS_DDL,
    TRADING_SPOT_REALIZED_PNL_DDL,
    TRADING_SIM_ACCOUNTS_DDL,
    TRADING_TRIAL_CREDITS_DDL,
    TRADING_TRIAL_POSITION_COSTS_DDL,
    TRADING_OPERATION_IDEMPOTENCY_DDL,
    TRADING_SPOT_POSITIONS_DDL,
    TRADING_FUTURES_POSITIONS_DDL,
    TRADING_MARGIN_POSITIONS_DDL,
    TRADING_PENDING_PROFIT_DDL,
    TRADING_RESERVE_POOL_DDL,
    TRADING_RESERVE_POOL_EVENTS_DDL,
    TRADING_USER_VOLUME_STATS_DDL,
    TRADING_AUDIT_EVENTS_DDL,
    TRADING_STATE_DDL,
    TRADING_BOTS_DDL,
    TRADING_BOT_RUNS_DDL,
    TRADING_GRID_BOTS_DDL,
    TRADING_GRID_ORDERS_DDL,
    TRADING_BOT_COMPETITION_REWARDS_DDL,
    TRADING_BOT_AUDIT_RUNS_DDL,
    TRADING_BOT_AUDIT_FINDINGS_DDL,
)
