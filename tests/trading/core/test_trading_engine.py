import sqlite3
import json
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pytest

from services.trading import margin as trading_margin_module
import services.trading.trading_engine as trading_engine_module
from services.points_chain import (
    PointsLedgerService,
    create_multisig_wallet,
    create_official_hot_wallet,
    ensure_points_economy_schema,
)
from services.points_chain.economy_layer import append_economy_event, economy_layer_report
from services.points_chain.schema import canonical_json, sha256_text, utc_now
from services.points_chain.wallet_identity import address_from_hash
from services.snapshots import ensure_snapshot_schema
from services.server_mode.context import SmV2Context
from services.trading.trading_engine import TradingEngineService, ensure_trading_schema, fee_points, notional_points
from services.trading.mode_gate import CrossWorldContamination, TradingDisabledInMode


ROOT = Path(__file__).resolve().parents[3]


def _db(tmp_path):
    path = tmp_path / "trading.db"

    def get_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
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
    return get_db


def _stamp_all_markets_boot_ready(trading):
    """Boot-ready gate (2026-05-06): production refuses to trade / liquidate /
    run bots until at least one live price has been confirmed for the market.
    Tests inject deterministic prices and don't go through the live worker, so
    we stamp all known markets up-front to keep existing fixtures honest.
    """
    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        conn.execute(
            "UPDATE trading_markets "
            "SET live_price_warmup_started_at=NULL, "
            "live_price_confirmed_at=COALESCE(live_price_confirmed_at, ?)",
            ("2024-01-01T00:00:00",),
        )
        conn.commit()
    finally:
        conn.close()


def _services(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    prices = {"BTC/POINTS": 77059, "ETH/POINTS": 5000}
    trading = TradingEngineService(get_db=get_db, points_service=points, live_price_provider=lambda symbol: prices[symbol])
    # Most trading-engine tests use injected prices, not live fused-network data.
    # Force the runtime price source onto the synthetic provider path so these
    # tests stay deterministic even when the host machine has real network access.
    _set_trading_setting(trading, "trading.price_source", "binance_public_api")
    trading.test_prices = prices
    _stamp_all_markets_boot_ready(trading)
    return points, trading


def test_exchange_fund_snapshot_uses_points_service_db_when_databases_are_split(tmp_path):
    def db_factory(path):
        def get_db():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
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
        conn.commit()
        conn.close()
        return get_db

    points_get_db = db_factory(tmp_path / "points.db")
    trading_get_db = db_factory(tmp_path / "trading.db")
    points = PointsLedgerService(get_db=points_get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    points_conn = points.get_db()
    try:
        points.ensure_schema(points_conn)
        economy_layer_report(points_conn, chain_secret=points.chain_secret, actor={"id": 3, "role": "root"})
        append_economy_event(
            points_conn,
            chain_secret=points.chain_secret,
            event_type="trading_reserve_pool_flow",
            transaction_type="margin_principal_lent",
            source_fund_key="exchange_fund",
            destination_fund_key=None,
            destination_address="pc1borrower",
            amount=4_900_001,
            idempotency_key="split-db-exchange-drain",
            actor={"id": 3, "role": "root"},
        )
        points_conn.commit()
    finally:
        points_conn.close()

    trading = TradingEngineService(
        get_db=trading_get_db,
        points_service=points,
        live_price_provider=lambda symbol: {"BTC/POINTS": 77059, "ETH/POINTS": 5000}[symbol],
    )
    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        snapshot = trading._exchange_fund_chain_snapshot(conn)
        reserve = trading._reserve(conn)
        conn.commit()
    finally:
        conn.close()

    assert snapshot["balance_points"] == 99_999
    assert snapshot["source_db_path"].endswith("points.db")
    assert int(reserve["balance_points"]) == 99_999


def _services_with_history(tmp_path, *, prices=None, candles=None):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    live_prices = {"BTC/POINTS": 77059, "ETH/POINTS": 5000}
    if prices:
        live_prices.update(prices)

    def history_provider(symbol, interval, limit):
        data = list(candles or [])
        return data[-int(limit or len(data)) :]

    trading = TradingEngineService(
        get_db=get_db,
        points_service=points,
        live_price_provider=lambda symbol: live_prices[symbol],
        historical_candles_provider=history_provider,
    )
    _set_trading_setting(trading, "trading.price_source", "binance_public_api")
    trading.test_prices = live_prices
    _stamp_all_markets_boot_ready(trading)
    return points, trading


def _set_live_price(trading, *, symbol, price_points, max_price_jump_percent=None):
    if hasattr(trading, "test_prices"):
        trading.test_prices[symbol] = price_points
    kwargs = {"manual_price_points": price_points, "confirm_jump": True}
    if max_price_jump_percent is not None:
        kwargs["max_price_jump_percent"] = max_price_jump_percent
    trading.update_market(actor=_actor(3, "root", "super_admin"), symbol=symbol, **kwargs)


def _actor(user_id=1, username="alice", role="user"):
    return {"id": user_id, "username": username, "role": role}


def _official_hot_wallet(points, user_id):
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        wallet = create_official_hot_wallet(conn, user_id=user_id, chain_secret=points.chain_secret)
        conn.commit()
        return wallet
    finally:
        conn.close()


def _set_governance_timelock_ready(points, proposal_uuid):
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (proposal_uuid,),
        ).fetchone()
        payload = json.loads(row["payload_json"]) if row and row["payload_json"] else {}
        if isinstance(payload.get("execution_guard"), dict):
            payload["execution_guard"]["timelock_until"] = "2026-01-01T00:00:00Z"
            payload["execution_guard"]["timelock_ends_at"] = "2026-01-01T00:00:00Z"
        execution_hash = points._governance_execution_payload_hash(
            action_type=row["action_type"],
            governance_domain=row["governance_domain"],
            target_wallet_address=row["target_wallet_address"],
            target_address=row["target_address"],
            target_branch=row["target_branch"],
            requested_amount=row["requested_amount"],
            requested_asset=row["requested_asset"],
            payload=payload,
        )
        conn.execute(
            """
            UPDATE points_chain_governance_proposals
            SET timelock_until='2026-01-01T00:00:00Z',
                timelock_ends_at='2026-01-01T00:00:00Z',
                payload_json=?,
                execution_payload_hash=?
            WHERE proposal_uuid=?
            """,
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), execution_hash, proposal_uuid),
        )
        conn.commit()
    finally:
        conn.close()


def _official_treasury_grant_via_governance(points, *, destination_wallet_address, amount, request_uuid, action_type="TREASURY_TRANSFER"):
    root = _actor(3, "root", "super_admin")
    manager = _actor(2, "bob", "manager")
    root_wallet = _official_hot_wallet(points, 3)
    manager_wallet = _official_hot_wallet(points, 2)
    created = points.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination_wallet_address,
        amount=amount,
        reason="governance official transfer",
        reference=request_uuid,
        action_type=action_type,
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    points.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    points.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    _set_governance_timelock_ready(points, proposal_uuid)
    points.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    signed = points.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    assert signed["multisig"]["ready"] is True
    executed = points.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    return executed["result"]["transfer"], proposal_uuid


def test_trading_dashboard_uses_selected_payment_wallet_balance(tmp_path):
    points, trading = _services(tmp_path)
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        primary = create_official_hot_wallet(conn, user_id=1, chain_secret=points.chain_secret)
        now = utc_now()
        secondary_address = address_from_hash("test-trading-secondary-wallet")
        conn.execute(
            """
            INSERT INTO points_wallet_identities (
                user_id, address, wallet_type, custody_mode, key_algorithm,
                public_key_jwk_json, public_key_hash, server_private_key_stored,
                is_primary, status, label, backup_confirmed_at, metadata_json,
                created_at, updated_at
            ) VALUES (1, ?, 'multisig', 'multisig', 'MULTISIG_POLICY_V1', '{}', ?, 0, 0, 'active', 'Trading wallet B', ?, ?, ?, ?)
            """,
            (
                secondary_address,
                sha256_text(secondary_address),
                now,
                canonical_json({"test_wallet": True}),
                now,
                now,
            ),
        )
        secondary = conn.execute("SELECT * FROM points_wallet_identities WHERE address=?", (secondary_address,)).fetchone()
        conn.commit()
    finally:
        conn.close()

    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=100,
        action_type="test_seed_primary_wallet",
        idempotency_key="test-trading-primary-wallet",
        public_metadata={"destination_wallet_address": primary["address"]},
        actor=_actor(3, "root", "super_admin"),
    )
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=900,
        action_type="test_seed_secondary_wallet",
        idempotency_key="test-trading-secondary-wallet",
        public_metadata={"destination_wallet_address": secondary["address"]},
        actor=_actor(3, "root", "super_admin"),
    )

    default_dashboard = trading.user_dashboard(user_id=1)
    selected_dashboard = trading.user_dashboard(user_id=1, source_wallet_address=secondary["address"])

    assert default_dashboard["funding"]["wallet_available_points"] == 100
    assert default_dashboard["funding"]["selected_wallet_address"] == primary["address"]
    assert selected_dashboard["funding"]["wallet_available_points"] == 900
    assert selected_dashboard["funding"]["selected_wallet_address"] == secondary["address"]


def test_trading_dashboard_rejects_inactive_selected_payment_wallet(tmp_path):
    _points, trading = _services(tmp_path)

    dashboard = trading.user_dashboard(user_id=1, source_wallet_address="pc1deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")

    assert dashboard["funding"]["wallet_selection_warning"] == "指定交易付款錢包不存在或已停用"
    assert dashboard["funding"]["selected_wallet_address"] == dashboard["funding"]["active_wallet_address"]


def test_trading_asset_overview_uses_lightweight_read_model(tmp_path):
    points, trading = _services(tmp_path)
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        primary = create_official_hot_wallet(conn, user_id=1, chain_secret=points.chain_secret)
        conn.execute("UPDATE trading_markets SET manual_price_points=77059 WHERE symbol='BTC/POINTS'")
        conn.execute(
            """
            INSERT OR REPLACE INTO trading_spot_positions (
                user_id, market_symbol, quantity_units, locked_quantity_units,
                avg_cost_points, fee_carry_micropoints, updated_at
            ) VALUES (1, 'BTC/POINTS', ?, 0, 70000, 0, ?)
            """,
            (trading_engine_module.ASSET_SCALE, utc_now()),
        )
        conn.execute(
            """
            INSERT INTO trading_margin_positions (
                position_uuid, user_id, market_symbol, position_type, quantity_units,
                entry_price_points, principal_points, collateral_points, status,
                opened_at, updated_at
            ) VALUES ('asset-overview-margin', 1, 'BTC/POINTS', 'margin_long', ?, 70000, 1000, 5000, 'open', ?, ?)
            """,
            (trading_engine_module.ASSET_SCALE, utc_now(), utc_now()),
        )
        conn.commit()
    finally:
        conn.close()

    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=1000,
        action_type="test_asset_overview_wallet",
        idempotency_key="test-asset-overview-wallet",
        public_metadata={"destination_wallet_address": primary["address"]},
        actor=_actor(3, "root", "super_admin"),
    )
    trading.user_dashboard = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("asset overview must not load full dashboard"))

    overview = trading.user_asset_overview(user_id=1)

    assert overview["available_points"] == 1000
    assert overview["spot_market_value_points"] == 77059
    assert overview["margin_position_equity_points"] == 12059
    assert overview["open_spot_positions"] == 1
    assert overview["open_margin_positions"] == 1
    assert overview["read_model_source"] == "trading_asset_overview_v1"


def test_trading_asset_overview_uses_current_legacy_wallet_columns_without_identity_balance(tmp_path):
    _points, trading = _services(tmp_path)
    conn = trading.get_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO points_wallets (
                user_id, soft_balance, hard_balance, soft_frozen, hard_frozen, created_at, updated_at
            ) VALUES (2, 0, 0, 0, 0, ?, ?)
            """,
            (utc_now(), utc_now()),
        )
        conn.execute(
            "UPDATE points_wallets SET soft_balance=321, hard_balance=9, soft_frozen=4, hard_frozen=2 WHERE user_id=2"
        )
        conn.commit()
    finally:
        conn.close()

    overview = trading.user_asset_overview(user_id=2)

    assert overview["available_points"] == 330
    assert overview["locked_points"] == 6
    assert overview["read_model_source"] == "trading_asset_overview_v1"


def test_trading_asset_overview_route_does_not_call_full_dashboard():
    trading_routes = (ROOT / "routes" / "trading.py").read_text(encoding="utf-8")
    route_body = trading_routes.split("def trading_asset_overview", 1)[1].split('@app.route("/api/admin/trading/asset-overview"', 1)[0]

    assert "user_asset_overview" in route_body
    assert "user_dashboard" not in route_body


def test_exchange_order_rejects_user_multisig_receive_only_wallet_source(tmp_path):
    points, trading = _services(tmp_path)
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        primary = create_official_hot_wallet(conn, user_id=1, chain_secret=points.chain_secret)
        multisig = create_multisig_wallet(
            conn,
            user_id=1,
            threshold=2,
            signer_addresses=[primary["address"], "pc1" + "a" * 48, "pc1" + "b" * 48],
        )
        conn.commit()
    finally:
        conn.close()

    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=10_000,
        action_type="test_seed_multisig_wallet",
        idempotency_key="test-trading-multisig-receive-only-wallet",
        public_metadata={"destination_wallet_address": multisig["address"]},
        actor=_actor(3, "root", "super_admin"),
    )
    _deplete_trial_credit(trading, user_id=1)

    dashboard = trading.user_dashboard(user_id=1, source_wallet_address=multisig["address"])
    assert dashboard["funding"]["selected_wallet_address"] == multisig["address"]
    assert dashboard["funding"]["wallet_available_points"] == 10_000

    with pytest.raises(ValueError, match="交易所僅支援 pc0 站內託管錢包"):
        trading.place_order(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            side="buy",
            order_type="market",
            quantity="1",
            source_wallet_address=multisig["address"],
        )


def test_exchange_order_rejects_self_custody_cold_wallet_source(tmp_path):
    points, trading = _services(tmp_path)
    cold_address = address_from_hash("test-trading-cold-wallet-direct-order")
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        now = utc_now()
        conn.execute(
            """
            INSERT INTO points_wallet_identities (
                user_id, address, wallet_type, custody_mode, key_algorithm,
                public_key_jwk_json, public_key_hash, server_private_key_stored,
                is_primary, status, label, backup_confirmed_at, metadata_json,
                created_at, updated_at
            ) VALUES (1, ?, 'self_custody_cold', 'self_custody', 'ECDSA_P256_SHA256', '{}', ?, 0, 0, 'active', 'Cold wallet', ?, '{}', ?, ?)
            """,
            (
                cold_address,
                sha256_text(cold_address),
                now,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="交易所僅支援 pc0 站內託管錢包"):
        trading.place_order(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            side="buy",
            order_type="market",
            quantity="0.1",
            source_wallet_address=cold_address,
        )


def _sm_ctx(mode="production", *, tester_id=None):
    return SmV2Context(mode=mode, tester_id=tester_id, actor_role="user", request_id=f"test-{mode}-{tester_id or 'prod'}")


def _set_trading_setting(trading, key, value):
    conn = trading.get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (str(key), str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def _depth_snapshot(trading, source, quantity_per_level, *, price=100.0):
    bids = [[price, quantity_per_level] for _ in range(10)] + [[price * 0.98, 9999]]
    asks = [[price * 1.001, quantity_per_level] for _ in range(10)] + [[price * 1.02, 9999]]
    return trading._build_orderbook_snapshot(
        source=source,
        bids=bids,
        asks=asks,
        fetch_meta={"fetched_at": trading_engine_module._now(), "latency_ms": 120.0},
        max_levels=100,
    )


def _notifications(trading, user_id):
    conn = trading.get_db()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT type, title, body, link FROM notifications WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def _deplete_trial_credit(trading, user_id=1):
    dashboard = trading.user_dashboard(user_id=user_id)
    assert dashboard["funding"]["trial_credit"]
    conn = trading.get_db()
    try:
        conn.execute(
            """
            UPDATE trading_trial_credits
            SET available_points=0, locked_points=0, deployed_points=0,
                status='depleted', updated_at=datetime('now')
            WHERE user_id=?
            """,
            (int(user_id),),
        )
        conn.commit()
    finally:
        conn.close()


class _FakePriceResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        import json

        return json.dumps(self.payload).encode("utf-8")


def test_depth_notional_score_accepts_orderbook_rows_with_extra_columns(tmp_path):
    _points, trading = _services(tmp_path)

    midpoint, depth_score = trading._depth_notional_score(
        [["100.0", "2.5", "0", "1"], ["99.5", "1.0", "0", "1"]],
        [["100.5", "3.0", "0", "1"], ["101.0", "1.0", "0", "1"]],
    )

    assert midpoint == pytest.approx(100.25)
    assert depth_score > 0


def test_place_order_rejects_non_trading_mode_before_sql(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=10_000, action_type="seed")
    with pytest.raises(TradingDisabledInMode):
        trading.place_order(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            side="buy",
            order_type="market",
            quantity="0.1",
            ctx=SmV2Context(mode="maintenance", tester_id=None, actor_role="user", request_id="g1-order"),
        )


def test_margin_open_rejects_non_trading_mode_before_sql(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=20_000, action_type="seed")
    with pytest.raises(TradingDisabledInMode):
        trading.open_margin_position(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            position_type="margin_long",
            quantity="1.0",
            collateral_points=1_000,
            ctx=SmV2Context(mode="maintenance", tester_id=None, actor_role="user", request_id="g1-margin"),
        )


def test_funding_publish_channels_are_isolated_by_mode_and_tester(tmp_path):
    _points, trading = _services(tmp_path)
    _set_trading_setting(trading, "trading.shadow_funding_publish_enabled", "true")

    prod = trading.publish_funding_rate_snapshot(
        market_symbol="BTC/POINTS",
        rate_percent=0.01,
        actor=_actor(),
        ctx=_sm_ctx("production"),
        provider_count=4,
        confidence="high",
    )["snapshot"]
    shadow = trading.publish_funding_rate_snapshot(
        market_symbol="BTC/POINTS",
        rate_percent=0.77,
        actor=_actor(),
        ctx=_sm_ctx("internal_test", tester_id=7),
        provider_count=1,
        confidence="low",
        degraded=True,
    )["snapshot"]

    prod_latest = trading.get_funding_rate_snapshot(market_symbol="BTC/POINTS", ctx=_sm_ctx("production"))["snapshot"]
    shadow_latest = trading.get_funding_rate_snapshot(market_symbol="BTC/POINTS", ctx=_sm_ctx("internal_test", tester_id=7))["snapshot"]

    assert prod["channel_key"] != shadow["channel_key"]
    assert prod_latest["rate_percent"] == pytest.approx(0.01)
    assert shadow_latest["rate_percent"] == pytest.approx(0.77)
    assert prod_latest["mode"] == "production"
    assert shadow_latest["mode"] == "internal_test"
    assert shadow_latest["tester_id"] == 7


def test_internal_test_funding_settlement_writes_only_shadow_wallet_and_ledger(tmp_path):
    points, trading = _services(tmp_path)
    _set_trading_setting(trading, "trading.shadow_funding_publish_enabled", "true")
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=500, action_type="seed")

    snapshot = trading.publish_funding_rate_snapshot(
        market_symbol="BTC/POINTS",
        rate_percent=0.25,
        actor=_actor(),
        ctx=_sm_ctx("internal_test", tester_id=1),
        provider_count=1,
        confidence="low",
        degraded=True,
    )["snapshot"]

    conn = trading.get_db()
    try:
        ensure_snapshot_schema(conn)
        prod_wallet_before = int(conn.execute("SELECT COALESCE(soft_balance, 0) + COALESCE(hard_balance, 0) FROM points_wallets WHERE user_id=1").fetchone()[0] or 0)
        prod_ledger_before = int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0)
        prod_chain_before = int(conn.execute("SELECT COUNT(*) FROM points_chain_blocks").fetchone()[0] or 0)
    finally:
        conn.close()

    result = trading.settle_funding_adjustment(
        actor=_actor(),
        user_id=1,
        market_symbol="BTC/POINTS",
        delta_points=125,
        published_snapshot=snapshot,
        ctx=_sm_ctx("internal_test", tester_id=1),
    )

    assert result["wallet"]["points_balance"] == 125
    conn = trading.get_db()
    try:
        shadow_wallet = conn.execute("SELECT * FROM test_shadow_wallets WHERE user_id=1").fetchone()
        shadow_ledger = conn.execute("SELECT * FROM test_shadow_ledger WHERE ledger_uuid=?", (result["ledger_uuid"],)).fetchone()
        assert shadow_wallet is not None
        assert int(shadow_wallet["balance_points"] or 0) == 125
        assert shadow_ledger is not None
        assert shadow_ledger["action_type"] == "trading_funding_settlement"
        assert int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0) == prod_ledger_before
        assert int(conn.execute("SELECT COUNT(*) FROM points_chain_blocks").fetchone()[0] or 0) == prod_chain_before
        assert int(conn.execute("SELECT COALESCE(soft_balance, 0) + COALESCE(hard_balance, 0) FROM points_wallets WHERE user_id=1").fetchone()[0] or 0) == prod_wallet_before
    finally:
        conn.close()


def test_shadow_funding_flag_misopen_still_cannot_pollute_production(tmp_path):
    points, trading = _services(tmp_path)
    _set_trading_setting(trading, "trading.shadow_funding_publish_enabled", "true")
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=500, action_type="seed")

    prod_snapshot = trading.publish_funding_rate_snapshot(
        market_symbol="BTC/POINTS",
        rate_percent=0.03,
        actor=_actor(),
        ctx=_sm_ctx("production"),
    )["snapshot"]
    trading.publish_funding_rate_snapshot(
        market_symbol="BTC/POINTS",
        rate_percent=0.99,
        actor=_actor(),
        ctx=_sm_ctx("internal_test", tester_id=9),
    )
    prod_wallet_before = points.get_wallet(1)["points_balance"]
    result = trading.settle_funding_adjustment(
        actor=_actor(),
        user_id=1,
        market_symbol="BTC/POINTS",
        delta_points=50,
        published_snapshot=prod_snapshot,
        ctx=_sm_ctx("production"),
    )

    assert result["wallet"]["points_balance"] == prod_wallet_before + 50
    shadow_latest = trading.get_funding_rate_snapshot(market_symbol="BTC/POINTS", ctx=_sm_ctx("internal_test", tester_id=9))["snapshot"]
    prod_latest = trading.get_funding_rate_snapshot(market_symbol="BTC/POINTS", ctx=_sm_ctx("production"))["snapshot"]
    assert shadow_latest["rate_percent"] == pytest.approx(0.99)
    assert prod_latest["rate_percent"] == pytest.approx(0.03)


def test_funding_settlement_rejects_cross_world_snapshot(tmp_path):
    _points, trading = _services(tmp_path)
    _set_trading_setting(trading, "trading.shadow_funding_publish_enabled", "true")
    prod_snapshot = trading.publish_funding_rate_snapshot(
        market_symbol="BTC/POINTS",
        rate_percent=0.05,
        actor=_actor(),
        ctx=_sm_ctx("production"),
    )["snapshot"]

    with pytest.raises(CrossWorldContamination):
        trading.settle_funding_adjustment(
            actor=_actor(),
            user_id=1,
            market_symbol="BTC/POINTS",
            delta_points=25,
            published_snapshot=prod_snapshot,
            ctx=_sm_ctx("internal_test", tester_id=1),
        )


@pytest.mark.parametrize(
    ("method_name", "payload", "expected_source"),
    [
        (
            "_fetch_okx_orderbook_snapshot",
            {
                "data": [
                    {
                        "bids": [["100", "2", "0", "1"], ["99", "1", "0", "1"]],
                        "asks": [["101", "3", "0", "1"], ["102", "1", "0", "1"]],
                    }
                ]
            },
            "okx_public_api",
        ),
        (
            "_fetch_coinbase_orderbook_snapshot",
            {
                "bids": [["100", "2", "extra"], ["99", "1", "extra"]],
                "asks": [["101", "3", "extra"], ["102", "1", "extra"]],
            },
            "coinbase_exchange",
        ),
        (
            "_fetch_kraken_orderbook_snapshot",
            {
                "error": [],
                "result": {
                    "XXBTZUSD": {
                        "bids": [["100", "2", "1715000000"], ["99", "1", "1715000001"]],
                        "asks": [["101", "3", "1715000002"], ["102", "1", "1715000003"]],
                    }
                },
            },
            "kraken_public_api",
        ),
    ],
)
def test_exchange_orderbook_snapshots_accept_provider_rows_with_extra_columns(tmp_path, monkeypatch, method_name, payload, expected_source):
    _points, trading = _services(tmp_path)
    monkeypatch.setattr(trading, "_fetch_json_url", lambda *_args, **_kwargs: payload)

    snapshot = getattr(trading, method_name)("BTC/POINTS")

    assert snapshot["source"] == expected_source
    assert snapshot["price_points"] > 0
    assert snapshot["depth_score"] > 0


def test_legacy_rate_unit_label_removed_from_repository_text():
    needle = "b" + "ps"
    ignored_dirs = {
        ".git", ".venv", "venv", "env", ".tox", ".eggs",
        "__pycache__", ".pytest_cache", "node_modules",
        "runtime", "storage", "reports", "secure_backups", "build", "dist", "artifacts",
    }
    ignored_prefixes = {
        Path("public/js/vendor"),
        Path("public/js/hls.light.min.js"),
    }
    ignored_suffixes = {".pyc", ".db", ".sqlite", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".gz"}
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ignored_dirs & set(path.parts) or path.suffix.lower() in ignored_suffixes:
            continue
        relative = path.relative_to(ROOT)
        if any(relative == prefix or prefix in relative.parents for prefix in ignored_prefixes):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if needle in text.lower():
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_matching_orderbook_registry_namespaces_orders_by_mode_and_tester(tmp_path):
    _points, trading = _services(tmp_path)
    now = trading_engine_module._now()
    prod_ctx = _sm_ctx("production")
    tester1_ctx = _sm_ctx("internal_test", tester_id=1)
    tester2_ctx = _sm_ctx("internal_test", tester_id=2)

    def _limit_row(order_id, order_uuid, *, status="open"):
        return {
            "id": order_id,
            "order_uuid": order_uuid,
            "market_symbol": "BTC/POINTS",
            "side": "buy",
            "order_type": "limit",
            "status": status,
            "limit_price_points": 77000,
            "updated_at": now,
        }

    trading._matching_orderbook_apply_order(_limit_row(1, "prod-order"), ctx=prod_ctx)
    trading._matching_orderbook_apply_order(_limit_row(2, "tester-1-order"), ctx=tester1_ctx)
    trading._matching_orderbook_apply_order(_limit_row(3, "tester-2-order"), ctx=tester2_ctx)

    prod_key = trading._matching_orderbook_namespace("BTC/POINTS", ctx=prod_ctx)[0]
    tester1_key = trading._matching_orderbook_namespace("BTC/POINTS", ctx=tester1_ctx)[0]
    tester2_key = trading._matching_orderbook_namespace("BTC/POINTS", ctx=tester2_ctx)[0]

    assert set(trading._matching_orderbooks) >= {prod_key, tester1_key, tester2_key}
    assert set(trading._matching_orderbooks[prod_key]["buy"]) == {"prod-order"}
    assert set(trading._matching_orderbooks[tester1_key]["buy"]) == {"tester-1-order"}
    assert set(trading._matching_orderbooks[tester2_key]["buy"]) == {"tester-2-order"}

    trading._matching_orderbook_apply_order(_limit_row(2, "tester-1-order", status="cancelled"), ctx=tester1_ctx)

    assert "tester-1-order" not in trading._matching_orderbooks[tester1_key]["buy"]
    assert "prod-order" in trading._matching_orderbooks[prod_key]["buy"]
    assert "tester-2-order" in trading._matching_orderbooks[tester2_key]["buy"]


def test_matching_orderbook_hydrate_reads_only_routed_world(tmp_path):
    _points, trading = _services(tmp_path)
    conn = trading.get_db()
    try:
        ensure_snapshot_schema(conn)
        now = trading_engine_module._now()
        conn.execute(
            """
            INSERT INTO trading_orders (
                order_uuid, user_id, market_symbol, side, order_type, funding_mode, execution_mode,
                quantity_units, limit_price_points, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("prod-limit-1", 1, "BTC/POINTS", "buy", "limit", "points_chain", "house_counterparty", 100, 77000, "open", now, now),
        )
        conn.execute(
            """
            INSERT INTO test_shadow_orders (
                order_uuid, tester_user_id, user_id, market_symbol, side, order_type, funding_mode, execution_mode,
                quantity_units, limit_price_points, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("shadow-limit-7", 7, 7, "BTC/POINTS", "buy", "limit", "points_chain", "house_counterparty", 100, 77100, "open", now, now),
        )
        conn.execute(
            """
            INSERT INTO test_shadow_orders (
                order_uuid, tester_user_id, user_id, market_symbol, side, order_type, funding_mode, execution_mode,
                quantity_units, limit_price_points, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("shadow-limit-8", 8, 8, "BTC/POINTS", "buy", "limit", "points_chain", "house_counterparty", 100, 77200, "open", now, now),
        )
        conn.commit()

        prod_orders = trading._matching_orderbook_order_uuids(conn, market_symbol="BTC/POINTS", ctx=_sm_ctx("production"))
        tester7_orders = trading._matching_orderbook_order_uuids(
            conn, market_symbol="BTC/POINTS", ctx=_sm_ctx("internal_test", tester_id=7)
        )
        tester8_orders = trading._matching_orderbook_order_uuids(
            conn, market_symbol="BTC/POINTS", ctx=_sm_ctx("internal_test", tester_id=8)
        )

        assert prod_orders == ["prod-limit-1"]
        assert tester7_orders == ["shadow-limit-7"]
        assert tester8_orders == ["shadow-limit-8"]
    finally:
        conn.close()


def test_internal_test_open_margin_position_routes_row_to_shadow_table(tmp_path):
    _points, trading = _services(tmp_path)
    conn = trading.get_db()
    try:
        ensure_snapshot_schema(conn)
        conn.commit()
    finally:
        conn.close()

    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.2",
        collateral_points=250,
        ctx=_sm_ctx("internal_test", tester_id=7),
    )

    conn = trading.get_db()
    try:
        prod_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM trading_margin_positions WHERE position_uuid=?",
                (opened["position"]["position_uuid"],),
            ).fetchone()[0]
            or 0
        )
        shadow_row = conn.execute(
            """
            SELECT tester_user_id, user_id, status, collateral_trial_points, collateral_chain_points
            FROM test_shadow_margin_positions
            WHERE position_uuid=?
            """,
            (opened["position"]["position_uuid"],),
        ).fetchone()
    finally:
        conn.close()

    assert prod_count == 0
    assert shadow_row is not None
    assert int(shadow_row["tester_user_id"] or 0) == 7
    assert int(shadow_row["user_id"] or 0) == 1
    assert shadow_row["status"] == "open"
    assert int(shadow_row["collateral_trial_points"] or 0) == 250
    assert int(shadow_row["collateral_chain_points"] or 0) == 0


def test_internal_test_open_margin_position_writes_chain_collateral_to_shadow_ledger(tmp_path):
    _points, trading = _services(tmp_path)
    ctx = _sm_ctx("internal_test", tester_id=9)
    conn = trading.get_db()
    try:
        ensure_snapshot_schema(conn)
        trading._ensure_shadow_wallet(conn, 1, ctx)
        now = trading_engine_module._now()
        conn.execute(
            """
            UPDATE test_shadow_wallets
            SET balance_points=?, frozen_points=0, updated_at=?
            WHERE user_id=?
            """,
            (2_000, now, 1),
        )
        trading._ensure_trial_credit(conn, 1)
        conn.execute(
            """
            UPDATE trading_trial_credits
            SET available_points=0, locked_points=0, deployed_points=0, status='depleted', updated_at=?
            WHERE user_id=?
            """,
            (now, 1),
        )
        prod_ledger_before = int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0)
        conn.commit()
    finally:
        conn.close()

    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.2",
        collateral_points=300,
        ctx=ctx,
    )

    conn = trading.get_db()
    try:
        prod_ledger_after = int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0)
        shadow_ledger_rows = conn.execute(
            """
            SELECT tester_user_id, action_type, direction, amount
            FROM test_shadow_ledger
            WHERE reference_id=?
            ORDER BY id ASC
            """,
            (opened["position"]["position_uuid"],),
        ).fetchall()
        shadow_wallet = conn.execute(
            "SELECT balance_points, frozen_points FROM test_shadow_wallets WHERE user_id=?",
            (1,),
        ).fetchone()
    finally:
        conn.close()

    assert prod_ledger_after == prod_ledger_before
    assert [row["action_type"] for row in shadow_ledger_rows] == [
        "trading_margin_collateral_freeze",
    ]
    assert all(int(row["tester_user_id"] or 0) == 9 for row in shadow_ledger_rows)
    assert shadow_wallet is not None
    assert int(shadow_wallet["balance_points"] or 0) == 1_700
    assert int(shadow_wallet["frozen_points"] or 0) == 300


def test_internal_test_root_limit_order_routes_row_to_shadow_orders(tmp_path):
    _points, trading = _services(tmp_path)
    ctx = _sm_ctx("internal_test", tester_id=3)
    conn = trading.get_db()
    try:
        ensure_snapshot_schema(conn)
        conn.commit()
    finally:
        conn.close()

    result = trading.place_order(
        actor=_actor(3, "root", "super_admin"),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="limit",
        quantity="1",
        limit_price_points=1,
        ctx=ctx,
    )

    assert result["order"]["status"] == "open"
    conn = trading.get_db()
    try:
        shadow = conn.execute(
            """
            SELECT tester_user_id, user_id, market_symbol, order_type, status
            FROM test_shadow_orders
            WHERE order_uuid=?
            """,
            (result["order"]["order_uuid"],),
        ).fetchone()
    finally:
        conn.close()

    assert shadow is not None
    assert int(shadow["tester_user_id"] or 0) == 3
    assert int(shadow["user_id"] or 0) == 3
    assert shadow["market_symbol"] == "ETH/POINTS"
    assert shadow["order_type"] == "limit"
    assert shadow["status"] == "open"


def test_spot_buy_uses_trial_credit_before_points_chain_and_updates_position(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=2000,
        action_type="admin_adjust_credit",
    )

    result = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.1",
    )

    assert result["order"]["status"] == "filled"
    wallet = points.get_wallet(1)
    assert wallet["points_balance"] == 2000
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["funding"]["trial_credit"]["available_points"] == 500
    assert dashboard["funding"]["trial_credit"]["deployed_points"] == 500
    assert dashboard["positions"][0]["market_symbol"] == "ETH/POINTS"
    assert dashboard["positions"][0]["quantity"] == "0.1"
    assert dashboard["futures_positions"] == []
    with points.get_db() as conn:
        position = conn.execute(
            "SELECT source_wallet_address, funding_sources_json, taint_status FROM trading_spot_positions WHERE user_id=1 AND market_symbol='ETH/POINTS'"
        ).fetchone()
    assert position["source_wallet_address"] == ""
    assert json.loads(position["funding_sources_json"]) == []
    assert position["taint_status"] == "normal"
    ledger_actions = [row["action_type"] for row in points.list_ledger(user_id=1, include_user_id=True)]
    assert "trading_spot_buy" not in ledger_actions
    report = trading.root_report()
    assert report["reserve_pool"]["balance_points"] == trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS
    assert report["funding_pool"]["exchange_fund_balance_points"] == trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS
    assert report["funding_pool"]["available_points"] == int(
        trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS
        * trading_engine_module.MARGIN_MAX_POOL_UTILIZATION_PERCENT
        / 100
    )
    assert report["funding_pool"]["cfd_profit_reserve_required_points"] == (
        trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS - report["funding_pool"]["available_points"]
    )
    notes = _notifications(trading, 1)
    assert notes[-1]["type"] == "trading_order_filled"
    assert notes[-1]["title"] == "交易已成交"
    assert "ETH/POINTS 買入 0.1 已成交" in notes[-1]["body"]


def test_mixed_trial_and_real_points_buy_only_records_real_points_on_chain(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=2000,
        action_type="admin_adjust_credit",
    )

    result = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.25",
    )

    assert result["order"]["status"] == "filled"
    dashboard = trading.user_dashboard(user_id=1)
    fill = dashboard["fills"][0]
    assert fill["funding_mode"] == "trial_mixed"
    assert fill["notional_points"] == 1250
    assert fill["fee_points"] == 0
    assert fill["fee_micropoints"] == 1_250_000
    assert dashboard["funding"]["trial_credit"]["available_points"] == 0
    assert dashboard["funding"]["trial_credit"]["deployed_points"] == 1000

    ledger_rows = points.list_ledger(user_id=1, include_user_id=True)
    trading_rows = [row for row in ledger_rows if row["reference_id"] == result["order"]["order_uuid"]]
    amounts_by_action = {row["action_type"]: row["amount"] for row in trading_rows}
    assert amounts_by_action == {
        "trading_freeze": 250,
        "trading_unfreeze": 250,
        "trading_spot_buy": 250,
    }
    with points.get_db() as conn:
        position = conn.execute(
            "SELECT source_wallet_address, funding_sources_json, taint_status FROM trading_spot_positions WHERE user_id=1 AND market_symbol='ETH/POINTS'"
        ).fetchone()
    if position["source_wallet_address"]:
        assert json.loads(position["funding_sources_json"])[0]["chain_spend_points"] == 250
        assert position["taint_status"] == "source_traced"
    else:
        assert json.loads(position["funding_sources_json"]) == []
        assert position["taint_status"] == "normal"
    assert points.get_wallet(1)["points_balance"] == 1750


def test_spot_cfd_principal_payout_and_fee_flow_through_exchange_fund(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=7000,
        action_type="admin_adjust_credit",
    )
    _deplete_trial_credit(trading, user_id=1)
    before_exchange = points.economy_stats()["economy_layer"]["funds"]["exchange_fund"]["balance"]

    buy = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="1",
    )
    after_buy = points.economy_stats()["economy_layer"]
    assert buy["order"]["status"] == "filled"
    assert after_buy["funds"]["exchange_fund"]["balance"] > before_exchange
    buy_principal = after_buy["funds"]["exchange_fund"]["balance"] - before_exchange

    result = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="sell",
        order_type="market",
        quantity="1",
    )

    fill = trading.user_dashboard(user_id=1)["fills"][0]
    fee = int(fill["fee_points"] or 0)
    assert result["order"]["status"] == "filled"
    assert fee > 0
    after = points.economy_stats()["economy_layer"]
    assert after["funds"]["exchange_fund"]["balance"] == before_exchange + fee

    conn = trading.get_db()
    try:
        trading_events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT transaction_type, amount, metadata_json
                FROM points_economy_events
                WHERE event_type='trading_reserve_pool_flow'
                ORDER BY id ASC
                """
            ).fetchall()
        ]
        reserve_events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT event_type, delta_points
                FROM trading_reserve_pool_events
                WHERE event_type IN ('spot_cfd_principal_collected', 'spot_cfd_gross_payout', 'fee_retained')
                ORDER BY id ASC
                """
            ).fetchall()
        ]
        walletized_principal = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM points_economy_events
            WHERE idempotency_key LIKE 'walletized_ledger:%'
              AND metadata_json LIKE '%trading_spot_buy%'
            """
        ).fetchone()
    finally:
        conn.close()

    assert trading_events == []
    assert [event["event_type"] for event in reserve_events] == [
        "spot_cfd_principal_collected",
        "spot_cfd_gross_payout",
        "fee_retained",
    ]
    assert int(reserve_events[0]["delta_points"]) == buy_principal
    assert int(reserve_events[1]["delta_points"]) == -buy_principal
    assert int(reserve_events[2]["delta_points"]) == fee
    assert int(walletized_principal["count"] or 0) == 1


def test_fee_points_ceil_positive_fractional_fee_for_integer_point_ledger():
    assert trading_engine_module.fee_points(0, 0.3) == 0
    assert trading_engine_module.fee_points(100, 0) == 0
    assert trading_engine_module.fee_points(100, 0.3) == 1
    assert trading_engine_module.fee_points(167, 0.3) == 1
    assert trading_engine_module.fee_points(334, 0.3) == 2
    assert trading_engine_module.fee_points(500, 0.3) == 2


def test_small_spot_buy_accumulates_fractional_fee_until_sell_settlement(tmp_path):
    _, trading = _services(tmp_path)

    bought = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.02",
    )

    assert bought["order"]["status"] == "filled"
    assert bought["order"]["fee_points"] == 0
    assert bought["order"]["fee_micropoints"] == 100_000

    dashboard = trading.user_dashboard(user_id=1)
    fill = dashboard["fills"][0]
    assert fill["notional_points"] == 100
    assert fill["fee_points"] == 0
    assert fill["fee_micropoints"] == 100_000
    assert dashboard["positions"][0]["fee_carry_micropoints"] == 100_000
    assert dashboard["funding"]["trial_credit"]["available_points"] == 900

    sold = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="sell",
        order_type="market",
        quantity="0.02",
    )
    assert sold["order"]["fee_points"] == 1
    assert sold["order"]["fee_micropoints"] == 200_000

    dashboard = trading.user_dashboard(user_id=1)
    sell_fill = dashboard["fills"][0]
    assert sell_fill["fee_points"] == 1
    assert sell_fill["fee_micropoints"] == 200_000
    assert dashboard["positions"][0]["fee_carry_micropoints"] == 0


@pytest.mark.parametrize(
    ("market_symbol", "entry_price", "exit_price", "quantity"),
    [
        ("XRP/POINTS", Decimal("3"), Decimal("3.3"), "100"),
        ("BNB/POINTS", Decimal("700"), Decimal("770"), "0.5"),
        ("PAXG/POINTS", Decimal("3300"), Decimal("3600"), "0.1"),
    ],
)
def test_new_points_markets_spot_orders_keep_cost_fee_and_realized_pnl_sane(tmp_path, market_symbol, entry_price, exit_price, quantity):
    _, trading = _services_with_history(
        tmp_path,
        prices={
            "XRP/POINTS": 3.0,
            "BNB/POINTS": 700.0,
            "PAXG/POINTS": 3300.0,
        },
    )

    trading.test_prices[market_symbol] = float(entry_price)
    buy = trading.place_order(
        actor=_actor(),
        market_symbol=market_symbol,
        side="buy",
        order_type="market",
        quantity=quantity,
    )

    dashboard_after_buy = trading.user_dashboard(user_id=1)
    position = next(row for row in dashboard_after_buy["positions"] if row["market_symbol"] == market_symbol)
    expected_notional = notional_points(position["quantity_units"], float(entry_price))

    assert buy["order"]["status"] == "filled"
    assert position["quantity"] == quantity
    assert position["avg_cost_points"] >= float(entry_price)
    assert position["gross_cost_points"] == expected_notional
    assert position["cost_basis_points"] == expected_notional + position["settlement_fee_points"]

    trading.test_prices[market_symbol] = float(exit_price)
    sold = trading.place_order(
        actor=_actor(),
        market_symbol=market_symbol,
        side="sell",
        order_type="market",
        quantity=quantity,
    )
    dashboard_after_sell = trading.user_dashboard(user_id=1)
    closed_position = next(row for row in dashboard_after_sell["positions"] if row["market_symbol"] == market_symbol)
    sell_fill = dashboard_after_sell["fills"][0]

    assert sold["order"]["status"] == "filled"
    assert closed_position["quantity_units"] == 0
    assert closed_position["quantity"] == "0"
    assert closed_position["realized_pnl_points"] > 0
    assert sell_fill["market_symbol"] == market_symbol
    assert sell_fill["price_points"] == float(exit_price)
    assert sell_fill["fee_points"] == sold["order"]["fee_points"]
    assert sell_fill["realized_pnl_points"] > 0


@pytest.mark.parametrize(
    ("market_symbol", "candles", "order_points"),
    [
        (
            "XRP/POINTS",
            [
                {"time": 1, "close_points": 3.0},
                {"time": 2, "close_points": 3.0},
                {"time": 3, "close_points": 4.0},
            ],
            300,
        ),
        (
            "BNB/POINTS",
            [
                {"time": 1, "close_points": 700.0},
                {"time": 2, "close_points": 700.0},
                {"time": 3, "close_points": 770.0},
            ],
            700,
        ),
        (
            "PAXG/POINTS",
            [
                {"time": 1, "close_points": 3300.0},
                {"time": 2, "close_points": 3300.0},
                {"time": 3, "close_points": 3600.0},
            ],
            330,
        ),
    ],
)
def test_new_points_markets_dca_backtest_keeps_fee_and_final_value_consistent(tmp_path, market_symbol, candles, order_points):
    _, trading = _services_with_history(
        tmp_path,
        prices={
            "XRP/POINTS": 3.0,
            "BNB/POINTS": 700.0,
            "PAXG/POINTS": 3300.0,
        },
        candles=candles,
    )

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": market_symbol,
            "strategy": "dca",
            "bot_config": {"order_points": order_points, "interval_candles": 99},
            "initial_cash_points": order_points,
            "candles": candles,
        },
    )

    spend = order_points
    fee = fee_points(spend, 0.1)
    buy_price = Decimal(str(candles[0]["close_points"]))
    last_price = Decimal(str(candles[-1]["close_points"]))
    units = int((Decimal(str(spend - fee)) * Decimal(trading_engine_module.ASSET_SCALE) / buy_price).quantize(Decimal("1"), rounding=ROUND_DOWN))
    expected_value = notional_points(units, float(last_price))

    assert result["ok"] is True
    assert result["market_symbol"] == market_symbol
    assert result["strategy"] == "dca"
    assert result["trade_count"] == 1
    assert result["trades"][0]["fee_points"] == fee
    assert result["trades"][0]["price_points"] == float(buy_price)
    assert result["position_value_points"] == expected_value
    assert result["final_value_points"] == expected_value
    assert result["pnl_points"] == expected_value - spend


@pytest.mark.parametrize(
    ("market_symbol", "price_points", "quantity", "collateral_points"),
    [
        ("XRP/POINTS", 3.0, "100", 500),
        ("BNB/POINTS", 700.0, "0.1", 200),
        ("PAXG/POINTS", 3300.0, "0.02", 200),
    ],
)
def test_new_points_markets_short_borrow_uses_non_btc_eth_apr_group_and_closes(tmp_path, market_symbol, price_points, quantity, collateral_points):
    points, trading = _services_with_history(
        tmp_path,
        prices={
            "XRP/POINTS": 3.0,
            "BNB/POINTS": 700.0,
            "PAXG/POINTS": 3300.0,
        },
    )
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_apr_btc_eth_percent": 8,
            "borrow_apr_usdt_points_percent": 10,
            "borrow_interest_pool_pressure_multiplier": 0,
            "borrow_interest_interval_hours": 1,
            "borrow_interest_minimum_hours": 1,
        },
        markets=[{"symbol": market_symbol, "manual_price_points": price_points}],
    )

    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol=market_symbol,
        position_type="short",
        quantity=quantity,
        collateral_points=collateral_points,
    )
    position = trading.user_dashboard(user_id=1)["margin_positions"][0]

    assert opened["position"]["status"] == "open"
    assert opened["position"]["borrowed_asset_symbol"] == market_symbol.split("/", 1)[0]
    assert opened["position"]["interest_percent_daily"] == pytest.approx(10.0 / 365.0)
    assert position["interest_apr_percent"] == pytest.approx(10.0)
    assert position["interest_interval_hours"] == 1
    assert position["interest_minimum_hours"] == 1
    assert position["breakeven_price_points"] > 0
    assert position["liquidation_price_points"] > opened["position"]["entry_price_points"]

    closed = trading.close_margin_position(actor=_actor(), position_uuid=opened["position"]["position_uuid"])
    assert closed["position"]["status"] == "closed"
    assert trading.verify_state()["ok"] is True


def test_backtest_accepts_full_year_hourly_window_under_new_limit(tmp_path):
    _, trading = _services(tmp_path)
    candles = [
        {
            "time": i,
            "time_iso": f"2024-01-01T{(i % 24):02d}:00:00+00:00",
            "open_points": 100 + (i % 5),
            "high_points": 101 + (i % 5),
            "low_points": 99 + (i % 5),
            "close_points": 100 + (i % 5),
            "price_points": 100 + (i % 5),
        }
        for i in range(8784)
    ]

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "BTC/POINTS",
            "strategy": "conditional",
            "trigger_type": "price_below",
            "trigger_price_points": 0,
            "candles": candles,
        },
    )

    assert result["ok"] is True
    assert result["candle_count"] == 8784
    assert result["max_backtest_candles"] == trading_engine_module.MAX_BACKTEST_CANDLES


def test_dca_backtest_preserves_interval_across_segment_boundaries(tmp_path):
    _, trading = _services(tmp_path)
    candle_count = trading_engine_module.BACKTEST_SEGMENT_CANDLES + 5
    candles = [
        {
            "time": i,
            "time_iso": f"2024-01-01T{(i % 24):02d}:00:00+00:00",
            "open_points": 100,
            "high_points": 100,
            "low_points": 100,
            "close_points": 100,
            "price_points": 100,
        }
        for i in range(candle_count)
    ]

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "BTC/POINTS",
            "strategy": "dca",
            "interval_candles": 3,
            "order_points": 2,
            "initial_cash_points": 10000,
            "candles": candles,
        },
    )

    expected_trades = ((candle_count - 1) // 3) + 1
    assert result["ok"] is True
    assert result["segmented_backtest"] is True
    assert result["segmented_backtest_batches"] == 2
    assert result["candle_count"] == candle_count
    assert result["trade_count"] == expected_trades


def test_workflow_backtest_preserves_position_across_segment_boundaries(tmp_path):
    _, trading = _services(tmp_path)
    candles = [
        {
            "time": i,
            "time_iso": f"2024-01-01T{(i % 24):02d}:00:00+00:00",
            "open_points": 200,
            "high_points": 200,
            "low_points": 200,
            "close_points": 200,
            "price_points": 200,
        }
        for i in range(trading_engine_module.BACKTEST_SEGMENT_CANDLES - 1)
    ]
    candles.extend([
        {
            "time": trading_engine_module.BACKTEST_SEGMENT_CANDLES - 1,
            "time_iso": "2024-02-20T00:00:00+00:00",
            "open_points": 90,
            "high_points": 90,
            "low_points": 90,
            "close_points": 90,
            "price_points": 90,
        },
        {
            "time": trading_engine_module.BACKTEST_SEGMENT_CANDLES,
            "time_iso": "2024-02-20T01:00:00+00:00",
            "open_points": 120,
            "high_points": 120,
            "low_points": 120,
            "close_points": 120,
            "price_points": 120,
        },
    ])
    workflow = {
        "version": "1",
        "strategy_kind": "workflow_graph",
        "start_node_id": "start",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "buy_cond", "type": "condition", "condition": {"type": "price_below", "value": 100}},
            {"id": "no_pos", "type": "condition", "condition": {"type": "has_position", "value": False}},
            {"id": "buy_and", "type": "logic", "operator": "AND"},
            {"id": "buy_act", "type": "action", "priority": 10, "action": {"type": "buy_percent", "percent": 100, "order_type": "market"}},
            {"id": "sell_cond", "type": "condition", "condition": {"type": "price_above", "value": 110}},
            {"id": "has_pos", "type": "condition", "condition": {"type": "has_position", "value": True}},
            {"id": "sell_and", "type": "logic", "operator": "AND"},
            {"id": "sell_act", "type": "action", "priority": 20, "action": {"type": "sell_percent", "percent": 100, "order_type": "market"}},
        ],
        "edges": [
            {"id": "e1", "from": "start", "from_port": "out", "to": "buy_cond", "to_port": "in"},
            {"id": "e2", "from": "start", "from_port": "out", "to": "no_pos", "to_port": "in"},
            {"id": "e3", "from": "buy_cond", "from_port": "true", "to": "buy_and", "to_port": "in"},
            {"id": "e4", "from": "no_pos", "from_port": "true", "to": "buy_and", "to_port": "in"},
            {"id": "e5", "from": "buy_and", "from_port": "true", "to": "buy_act", "to_port": "in"},
            {"id": "e6", "from": "start", "from_port": "out", "to": "sell_cond", "to_port": "in"},
            {"id": "e7", "from": "start", "from_port": "out", "to": "has_pos", "to_port": "in"},
            {"id": "e8", "from": "sell_cond", "from_port": "true", "to": "sell_and", "to_port": "in"},
            {"id": "e9", "from": "has_pos", "from_port": "true", "to": "sell_and", "to_port": "in"},
            {"id": "e10", "from": "sell_and", "from_port": "true", "to": "sell_act", "to_port": "in"},
        ],
    }

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "BTC/POINTS",
            "strategy": "workflow",
            "workflow_json": workflow,
            "initial_cash_points": 1000,
            "candles": candles,
        },
    )

    assert result["ok"] is True
    assert result["segmented_backtest"] is True
    assert result["trade_count"] == 2
    assert result["end_units"] == 0
    assert result["final_value_points"] > result["initial_cash_points"]


def test_workflow_backtest_does_not_false_trigger_bollinger_on_flat_sequence(tmp_path):
    _, trading = _services(tmp_path)
    candles = [
        {
            "time": i,
            "time_iso": f"2024-01-01T{(i % 24):02d}:00:00+00:00",
            "open_points": 100,
            "high_points": 100,
            "low_points": 100,
            "close_points": 100,
            "price_points": 100,
        }
        for i in range(30)
    ]
    workflow = {
        "version": "1",
        "strategy_kind": "workflow_graph",
        "start_node_id": "start",
        "nodes": [
            {"id": "start", "type": "start"},
            {
                "id": "bb_flat_guard",
                "type": "condition",
                "condition": {"type": "bb_position", "position": "below_lower"},
            },
            {"id": "no_pos", "type": "condition", "condition": {"type": "has_position", "value": False}},
            {"id": "buy_and", "type": "logic", "operator": "AND"},
            {"id": "buy_act", "type": "action", "priority": 10, "action": {"type": "buy_percent", "percent": 100, "order_type": "market"}},
        ],
        "edges": [
            {"id": "e1", "from": "start", "from_port": "out", "to": "bb_flat_guard", "to_port": "in"},
            {"id": "e2", "from": "start", "from_port": "out", "to": "no_pos", "to_port": "in"},
            {"id": "e3", "from": "bb_flat_guard", "from_port": "true", "to": "buy_and", "to_port": "in"},
            {"id": "e4", "from": "no_pos", "from_port": "true", "to": "buy_and", "to_port": "in"},
            {"id": "e5", "from": "buy_and", "from_port": "true", "to": "buy_act", "to_port": "in"},
        ],
    }

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "BTC/POINTS",
            "strategy": "workflow",
            "workflow_json": workflow,
            "initial_cash_points": 1000,
            "candles": candles,
        },
    )

    assert result["ok"] is True
    assert result["trade_count"] == 0
    assert result["final_value_points"] == result["initial_cash_points"]


def test_backtest_skips_outlier_jump_candles_instead_of_booking_fake_profit(tmp_path):
    _, trading = _services(tmp_path)
    candles = [
        {"time_iso": "2024-01-01T00:00:00+00:00", "close_points": 100, "price_points": 100},
        {"time_iso": "2024-01-01T00:15:00+00:00", "close_points": 10, "price_points": 10},
        {"time_iso": "2024-01-01T00:30:00+00:00", "close_points": 150, "price_points": 150},
    ]

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "BTC/POINTS",
            "strategy": "conditional",
            "trigger_type": "price_below",
            "trigger_price_points": 50,
            "initial_cash_points": 1000,
            "order_points": 100,
            "candles": candles,
        },
    )

    assert result["ok"] is True
    assert result["trade_count"] == 0
    assert result["outlier_skipped_count"] == 1
    assert result["final_value_points"] == result["initial_cash_points"]
    assert any("已略過跳價" in warning for warning in result["range_warnings"])


def test_bot_audit_dashboard_marks_new_bot_as_unaudited_until_trade_or_24h(tmp_path):
    _, trading = _services(tmp_path)
    created = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "bot_type": "dca",
            "name": "fresh dca",
            "budget_points": 100,
            "interval_hours": 24,
            "enabled": True,
            "max_runs": -1,
        },
    )

    dashboard = trading.get_bot_audit_dashboard(limit=20)
    item = next(row for row in dashboard["items"] if row["bot_uuid"] == created["bot"]["bot_uuid"])

    assert item["audit_status"] == "unaudited"
    assert item["eligible"] is False
    assert item["eligible_reason"] == "awaiting_first_trade"


def test_bot_audit_runs_green_after_first_successful_trade(tmp_path):
    _, trading = _services(tmp_path)
    created = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "bot_type": "dca",
            "name": "audited dca",
            "budget_points": 100,
            "interval_hours": 24,
            "enabled": True,
            "max_runs": -1,
        },
    )
    bot_uuid = created["bot"]["bot_uuid"]
    trading.run_trading_bot_once(actor=_actor(), bot_uuid=bot_uuid)

    result = trading.run_due_bot_audits(force=True)
    dashboard = trading.get_bot_audit_dashboard(limit=20)
    item = next(row for row in dashboard["items"] if row["bot_uuid"] == bot_uuid)

    assert any(row["bot_uuid"] == bot_uuid for row in result["audited"])
    assert item["audit_status"] == "green"
    assert item["eligible"] is True
    assert item["eligible_reason"] == "has_trade"


def test_bot_competition_ranks_by_percent_and_awards_weekly_winner(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=10_000, action_type="seed")
    points.record_transaction(user_id=2, currency_type="points", direction="credit", amount=10_000, action_type="seed")

    _set_live_price(trading, symbol="ETH/POINTS", price_points=5000)
    alice = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "bot_type": "dca",
            "name": "alice dca",
            "budget_points": 1000,
            "interval_hours": 24,
            "enabled": True,
            "max_runs": -1,
            "share_parameters": True,
        },
    )
    trading.run_trading_bot_once(actor=_actor(), bot_uuid=alice["bot"]["bot_uuid"])

    _set_live_price(trading, symbol="ETH/POINTS", price_points=6000)
    bob = trading.save_trading_bot(
        actor=_actor(2, "bob", "manager"),
        payload={
            "market_symbol": "ETH/POINTS",
            "bot_type": "dca",
            "name": "bob dca",
            "budget_points": 1000,
            "interval_hours": 24,
            "enabled": True,
            "max_runs": -1,
            "share_parameters": False,
        },
    )
    trading.run_trading_bot_once(actor=_actor(2, "bob", "manager"), bot_uuid=bob["bot"]["bot_uuid"])

    competition = trading.get_bot_competition(actor=_actor())
    dca = next(category for category in competition["categories"] if category["category"] == "dca")

    assert dca["leaderboard"][0]["username"] == "alice"
    assert dca["leaderboard"][0]["rank"] == 1
    assert dca["leaderboard"][0]["performance_percent"] > dca["leaderboard"][1]["performance_percent"]
    assert dca["leaderboard"][0]["shared_parameters"]["budget_points"] == 1000
    assert dca["leaderboard"][1]["shared_parameters"] is None

    award = trading.award_bot_competition_week(actor=_actor(3, "root", "super_admin"), week=competition["week"])
    assert award["awarded"][0]["username"] == "alice"
    assert award["awarded"][0]["reward_points"] == 100

    conn = trading.get_db()
    try:
        ledger = conn.execute(
            "SELECT user_id, amount, action_type FROM points_ledger WHERE action_type='trading_bot_weekly_competition_reward'"
        ).fetchone()
    finally:
        conn.close()
    assert ledger["user_id"] == 1
    assert ledger["amount"] == 100


def test_bot_audit_warns_after_24_hours_without_any_trade(tmp_path):
    _, trading = _services(tmp_path)
    created = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "bot_type": "dca",
            "name": "idle dca",
            "budget_points": 100,
            "interval_hours": 24,
            "enabled": True,
            "max_runs": -1,
        },
    )
    bot_uuid = created["bot"]["bot_uuid"]
    conn = trading.get_db()
    try:
        conn.execute("UPDATE trading_bots SET enabled_at='2024-01-01T00:00:00' WHERE bot_uuid=?", (bot_uuid,))
        conn.commit()
    finally:
        conn.close()

    result = trading.run_due_bot_audits(force=True)
    dashboard = trading.get_bot_audit_dashboard(limit=20)
    item = next(row for row in dashboard["items"] if row["bot_uuid"] == bot_uuid)

    assert any(row["bot_uuid"] == bot_uuid for row in result["audited"])
    assert item["audit_status"] == "yellow"
    assert item["warning_count"] >= 1
    assert item["eligible_reason"] == "aged_24h"


def test_grid_bot_audit_marks_orphan_open_orders_as_red(tmp_path):
    _, trading = _services(tmp_path)
    created = trading.create_grid_bot(
        actor=_actor(),
        payload={
            "name": "grid orphan",
            "market_symbol": "ETH/POINTS",
            "upper_price_points": 120,
            "lower_price_points": 80,
            "grid_count": 5,
            "order_amount_points": 100,
        },
    )
    bot_uuid = created["bot"]["bot_uuid"]
    conn = trading.get_db()
    try:
        bot_row = conn.execute("SELECT id FROM trading_grid_bots WHERE bot_uuid=?", (bot_uuid,)).fetchone()
        conn.execute("UPDATE trading_grid_bots SET enabled_at='2024-01-01T00:00:00' WHERE id=?", (int(bot_row["id"]),))
        open_row = conn.execute(
            "SELECT * FROM trading_grid_orders WHERE grid_bot_id=? AND status='open' ORDER BY id ASC LIMIT 1",
            (int(bot_row["id"]),),
        ).fetchone()
        conn.execute("UPDATE trading_orders SET status='cancelled' WHERE order_uuid=?", (open_row["trading_order_uuid"],))
        conn.commit()
    finally:
        conn.close()

    trading.run_due_bot_audits(force=True)
    dashboard = trading.get_bot_audit_dashboard(limit=20)
    item = next(row for row in dashboard["items"] if row["bot_uuid"] == bot_uuid)

    assert item["audit_status"] == "red"
    assert item["blocker_count"] >= 1


def test_grid_bot_stop_loss_disables_bot_and_cancels_open_orders(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.1")
    created = trading.create_grid_bot(
        actor=_actor(),
        payload={
            "name": "grid stop loss",
            "market_symbol": "ETH/POINTS",
            "upper_price_points": 5100,
            "lower_price_points": 4900,
            "grid_count": 3,
            "order_amount_points": 100,
            "stop_loss_percent": 5,
        },
    )

    _set_live_price(trading, symbol="ETH/POINTS", price_points=4700)
    scanned = trading.scan_grid_bots(actor=_actor())
    listed = trading.list_grid_bots(actor=_actor())["bots"][0]

    assert created["bot"]["stop_loss_percent"] == 5.0
    assert scanned["results"][0]["risk_target_triggered"] is True
    assert scanned["results"][0]["risk_target"]["reason"] == "stop_loss"
    assert listed["enabled"] is False
    assert "止損已觸發" in listed["last_error"]
    assert all(order["status"] != "open" for order in listed["orders"])


def test_dca_backtest_matches_exact_math_at_full_20000_candle_limit(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)

    candles = [
        {
            "time_iso": f"2026-01-{1 + (idx // 1440):02d}T{(idx // 60) % 24:02d}:{idx % 60:02d}:00+00:00",
            "open_points": 100,
            "high_points": 100,
            "low_points": 100,
            "close_points": 100,
        }
        for idx in range(trading_engine_module.MAX_BACKTEST_CANDLES)
    ]

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "dca",
            "initial_cash_points": 10_000,
            "order_points": 100,
            "interval_candles": 250,
            "candles": candles,
        },
    )

    expected_trades = ((len(candles) - 1) // 250) + 1
    assert len(candles) == trading_engine_module.MAX_BACKTEST_CANDLES == 20_000
    assert result["segmented_backtest"] is True
    assert result["segmented_backtest_batches"] == 2
    assert result["trade_count"] == expected_trades == 80
    assert result["cash_points"] == 2_000
    assert result["end_units"] == 79.2 * trading_engine_module.ASSET_SCALE
    assert result["position_value_points"] == 7_920
    assert result["final_value_points"] == 9_920
    assert result["pnl_points"] == -80


def test_conditional_backtest_matches_exact_math_at_full_20000_candle_limit(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)

    candles = [
        {
            "time_iso": f"2026-01-{1 + (idx // 1440):02d}T{(idx // 60) % 24:02d}:{idx % 60:02d}:00+00:00",
            "open_points": 100 if idx == 9_999 else 200,
            "high_points": 100 if idx == 9_999 else 200,
            "low_points": 100 if idx == 9_999 else 200,
            "close_points": 100 if idx == 9_999 else 200,
        }
        for idx in range(trading_engine_module.MAX_BACKTEST_CANDLES)
    ]

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "conditional",
            "initial_cash_points": 100,
            "order_points": 100,
            "trigger_type": "price_below",
            "trigger_price_points": 100,
            "candles": candles,
        },
    )

    assert result["segmented_backtest"] is True
    assert result["segmented_backtest_batches"] == 2
    assert result["trade_count"] == 1
    assert result["trades"][0]["price_points"] == 100
    assert result["trades"][0]["fee_points"] == 1
    assert result["cash_points"] == 0
    assert result["end_units"] == int(Decimal("0.99") * trading_engine_module.ASSET_SCALE)
    assert result["position_value_points"] == 198
    assert result["final_value_points"] == 198
    assert result["pnl_points"] == 98


def test_grid_backtest_matches_exact_math_at_full_20000_candle_limit(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)

    candles = [
        {
            "time_iso": f"2026-01-{1 + (idx // 1440):02d}T{(idx // 60) % 24:02d}:{idx % 60:02d}:00+00:00",
            "open_points": 100,
            "high_points": 100,
            "low_points": 100,
            "close_points": 100,
        }
        for idx in range(19_996)
    ] + [
        {"time_iso": "2026-01-07T22:38:00+00:00", "open_points": 100, "high_points": 100, "low_points": 100, "close_points": 100},
        {"time_iso": "2026-01-07T22:39:00+00:00", "open_points": 100, "high_points": 100, "low_points": 90, "close_points": 90},
        {"time_iso": "2026-01-07T22:40:00+00:00", "open_points": 90, "high_points": 110, "low_points": 90, "close_points": 110},
        {"time_iso": "2026-01-07T22:41:00+00:00", "open_points": 110, "high_points": 120, "low_points": 80, "close_points": 100},
    ]

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "grid",
            "initial_cash_points": 1_000,
            "lower_price_points": 80,
            "upper_price_points": 120,
            "grid_count": 5,
            "order_amount_points": 100,
            "candles": candles,
        },
    )

    assert len(candles) == trading_engine_module.MAX_BACKTEST_CANDLES == 20_000
    assert result["segmented_backtest"] is True
    assert result["segmented_backtest_batches"] == 2
    assert result["trade_count"] == 7
    assert [row["side"] for row in result["trades"]] == ["buy", "sell", "sell", "sell", "buy", "buy", "buy"]
    assert result["final_value_points"] == 1_065
    assert result["pnl_points"] == 65


def test_workflow_backtest_matches_exact_math_at_full_20000_candle_limit_without_legacy_indicator_hot_path(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)

    candles = [
        {
            "time_iso": f"2026-01-{1 + (idx // 1440):02d}T{(idx // 60) % 24:02d}:{idx % 60:02d}:00+00:00",
            "open_points": 90 if idx == 9_999 else 120 if idx == 19_999 else 200 if idx < 9_999 else 100,
            "high_points": 90 if idx == 9_999 else 120 if idx == 19_999 else 200 if idx < 9_999 else 100,
            "low_points": 90 if idx == 9_999 else 120 if idx == 19_999 else 200 if idx < 9_999 else 100,
            "close_points": 90 if idx == 9_999 else 120 if idx == 19_999 else 200 if idx < 9_999 else 100,
        }
        for idx in range(trading_engine_module.MAX_BACKTEST_CANDLES)
    ]
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [
            {
                "id": "buy_dip",
                "name": "跌破買入",
                "priority": 100,
                "logic": "AND",
                "cooldown_seconds": 0,
                "max_runs": 1,
                "conditions": [{"type": "price_below", "value": 100}],
                "actions": [{"type": "buy_percent", "percent": 100, "step": 1, "order_type": "market"}],
            },
            {
                "id": "sell_rally",
                "name": "漲破賣出",
                "priority": 10,
                "logic": "AND",
                "cooldown_seconds": 0,
                "max_runs": 1,
                "conditions": [{"type": "price_above", "value": 110}, {"type": "has_position", "value": True}],
                "actions": [{"type": "close_all", "step": 1, "order_type": "market"}],
            },
        ],
    }

    def _legacy_context_should_not_run(*args, **kwargs):
        raise AssertionError("legacy workflow indicator context should not be used during 20k workflow backtest")

    trading._workflow_indicator_context = _legacy_context_should_not_run

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "workflow",
            "workflow_json": workflow,
            "initial_cash_points": 10_000,
            "order_points": 1_000,
            "candles": candles,
        },
    )

    buy_spend = 10_000
    buy_fee = fee_points(buy_spend, 0.1)
    buy_units = int((Decimal(str(buy_spend - buy_fee)) * Decimal(trading_engine_module.ASSET_SCALE) / Decimal("90")).quantize(Decimal("1"), rounding=ROUND_DOWN))
    sell_gross = notional_points(buy_units, 120)
    sell_fee = fee_points(sell_gross, 0.1)
    expected_final = sell_gross - sell_fee

    assert result["segmented_backtest"] is True
    assert result["segmented_backtest_batches"] == 2
    assert result["trade_count"] == 2
    assert result["trades"][0]["price_points"] == 90
    assert result["trades"][1]["price_points"] == 120
    assert result["trades"][0]["fee_points"] == buy_fee
    assert result["trades"][1]["fee_points"] == sell_fee
    assert result["end_units"] == 0
    assert result["final_value_points"] == expected_final


def test_trading_bot_workflow_triggers_existing_order_path(tmp_path):
    _, trading = _services(tmp_path)
    bot = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "name": "ETH dip buyer",
            "market_symbol": "ETH/POINTS",
            "trigger_type": "price_below",
            "trigger_price_points": 5000,
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )

    assert bot["bot"]["bot_uuid"]
    scanned = trading.run_trading_bots(actor=_actor(), limit=10)

    assert scanned["ok"] is True
    assert scanned["scanned"] == 1
    assert len(scanned["triggered"]) == 1
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["bots"][0]["run_count"] == 1
    assert dashboard["orders"][0]["status"] == "filled"
    assert dashboard["bot_runs"][0]["status"] == "triggered"


def test_trading_bot_auto_scan_runs_due_bots_for_all_users(tmp_path):
    _, trading = _services(tmp_path)
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "name": "Alice ETH dip buyer",
            "market_symbol": "ETH/POINTS",
            "trigger_type": "price_below",
            "trigger_price_points": 5000,
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )
    trading.save_trading_bot(
        actor=_actor(user_id=2, username="bob", role="manager"),
        payload={
            "name": "Bob ETH dip buyer",
            "market_symbol": "ETH/POINTS",
            "trigger_type": "price_below",
            "trigger_price_points": 5000,
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )

    scanned = trading.run_due_trading_bots(actor={"username": "system", "role": "system"}, limit=10)

    assert scanned["ok"] is True
    assert scanned["enabled"] is True
    assert scanned["scanned"] == 2
    assert len(scanned["triggered"]) == 2
    assert trading.user_dashboard(user_id=1)["bots"][0]["run_count"] == 1
    assert trading.user_dashboard(user_id=2)["bots"][0]["run_count"] == 1


def test_trading_bot_auto_scan_respects_root_setting(tmp_path):
    _, trading = _services(tmp_path)
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "name": "Disabled auto scan bot",
            "market_symbol": "ETH/POINTS",
            "trigger_type": "price_below",
            "trigger_price_points": 5000,
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )
    trading.update_root_settings(
        actor=_actor(user_id=3, username="root", role="super_admin"),
        settings={"bot_auto_scan_enabled": False},
    )

    scanned = trading.run_due_trading_bots(actor={"username": "system", "role": "system"}, limit=10)

    assert scanned["ok"] is True
    assert scanned["enabled"] is False
    assert scanned["reason"] == "bot_auto_scan_disabled"
    assert trading.user_dashboard(user_id=1)["bots"][0]["run_count"] == 0


def test_trading_bot_workflow_records_skipped_condition(tmp_path):
    _, trading = _services(tmp_path)
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "name": "ETH expensive buyer",
            "market_symbol": "ETH/POINTS",
            "trigger_type": "price_below",
            "trigger_price_points": 1,
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )

    scanned = trading.run_trading_bots(actor=_actor(), limit=10)

    assert scanned["ok"] is True
    assert scanned["triggered"] == []
    assert scanned["skipped"][0]["reason"] == "condition_not_met"
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["bots"][0]["run_count"] == 0
    assert dashboard["bot_runs"][0]["status"] == "skipped"


def test_trading_bot_failure_counts_run_and_notifies_user(tmp_path):
    _, trading = _services(tmp_path)
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "name": "oversized ETH buyer",
            "market_symbol": "ETH/POINTS",
            "trigger_type": "price_below",
            "trigger_price_points": 6000,
            "side": "sell",
            "order_type": "market",
            "quantity": "1",
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )

    first = trading.run_trading_bots(actor=_actor(), limit=10)
    second = trading.run_trading_bots(actor=_actor(), limit=10)

    assert first["ok"] is False
    assert len(first["failed"]) == 1
    assert second["scanned"] == 0
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["bots"][0]["run_count"] == 1
    assert dashboard["bot_runs"][0]["status"] == "failed"
    notes = _notifications(trading, 1)
    assert notes[-1]["type"] == "trading_bot_failed"
    assert "oversized ETH buyer" in notes[-1]["body"]


def test_dca_trading_bot_converts_budget_to_market_order(tmp_path):
    _, trading = _services(tmp_path)
    bot = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "dca",
            "name": "Daily ETH DCA",
            "market_symbol": "ETH/POINTS",
            "budget_points": 100,
            "interval_hours": 24,
            "max_runs": 1,
            "enabled": True,
        },
    )

    assert bot["bot"]["bot_type"] == "dca"
    scanned = trading.run_trading_bots(actor=_actor(), limit=10)

    assert scanned["ok"] is True
    assert len(scanned["triggered"]) == 1
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["orders"][0]["side"] == "buy"
    assert dashboard["orders"][0]["order_type"] == "market"
    total_spend = (
        notional_points(dashboard["orders"][0]["filled_quantity_units"], dashboard["orders"][0]["execution_price_points"])
        + int(dashboard["orders"][0]["fee_points"] or 0)
    )
    assert total_spend <= 100
    assert dashboard["bots"][0]["run_count"] == 1


def test_run_single_dca_bot_once_executes_created_bot_immediately(tmp_path):
    _, trading = _services(tmp_path)
    bot = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "dca",
            "name": "Immediate ETH DCA",
            "market_symbol": "ETH/POINTS",
            "budget_points": 100,
            "interval_hours": 24,
            "max_runs": 2,
            "enabled": True,
        },
    )["bot"]

    result = trading.run_trading_bot_once(actor=_actor(), bot_uuid=bot["bot_uuid"])

    assert result["ok"] is True
    assert result["scanned"] == 1
    assert len(result["triggered"]) == 1
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["orders"][0]["side"] == "buy"
    assert dashboard["bots"][0]["run_count"] == 1
    assert dashboard["bots"][0]["next_run_at"]


def test_workflow_rsi_uses_wilder_smoothing(tmp_path):
    _, trading = _services(tmp_path)
    closes = [44, 44.15, 43.9, 44.35, 44.6, 44.3, 44.8, 45.0, 44.7, 45.2, 45.4, 45.1, 45.6, 45.8, 46.0, 45.7, 46.3]
    candles = [{"close_points": value, "high_points": value + 0.2, "low_points": value - 0.2} for value in closes]
    context = trading._workflow_indicator_context(candles, len(candles) - 1)
    gains = []
    losses = []
    for index in range(1, len(closes)):
        delta = closes[index] - closes[index - 1]
        gains.append(max(delta, 0))
        losses.append(abs(min(delta, 0)))
    period = 14
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
    expected = 100 - (100 / (1 + (avg_gain / avg_loss)))

    assert context["rsi"] == pytest.approx(expected)
    simple_gain = sum(gains[-period:]) / period
    simple_loss = sum(losses[-period:]) / period
    simple_rsi = 100 - (100 / (1 + (simple_gain / simple_loss)))
    assert context["rsi"] != pytest.approx(simple_rsi)


def test_workflow_indicator_series_matches_legacy_context(tmp_path):
    _, trading = _services(tmp_path)
    candles = [
        {
            "close_points": 4000 + (index * 7.5) + ((index % 5) * 0.2),
            "high_points": 4001 + (index * 7.5) + ((index % 3) * 0.4),
            "low_points": 3999 + (index * 7.5) - ((index % 4) * 0.3),
        }
        for index in range(240)
    ]

    series = trading._build_workflow_indicator_series(candles)

    for index in (19, 50, 120, 220):
        legacy = trading._workflow_indicator_context(candles, index)
        built = series[index]
        for key in ("price", "ma20", "ma50", "ma200", "bb_mid", "bb_upper", "bb_lower", "bb_std", "rsi", "kd"):
            if legacy[key] is None:
                assert built[key] is None
            else:
                assert built[key] == pytest.approx(legacy[key])


def test_workflow_bot_uses_branch_priority_and_percent_action(tmp_path):
    _, trading = _services(tmp_path)
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [
            {
                "id": "stop",
                "name": "too low",
                "priority": 100,
                "logic": "AND",
                "conditions": [{"type": "price_below", "value": 1}],
                "actions": [{"type": "close_all", "step": 1}],
            },
            {
                "id": "entry",
                "name": "entry",
                "priority": 10,
                "logic": "AND",
                "cooldown_seconds": 0,
                "conditions": [{"type": "price_below", "value": 6000}],
                "actions": [{"type": "buy_percent", "percent": 10, "step": 1}],
            },
        ],
    }
    bot = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "Workflow ETH buyer",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "workflow_json": workflow,
            "max_runs": 2,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )

    assert bot["bot"]["workflow"]["branches"][0]["id"] == "stop"
    scanned = trading.run_trading_bots(actor=_actor(), limit=10)

    assert scanned["ok"] is True
    assert len(scanned["triggered"]) == 1
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["bots"][0]["workflow"]["strategy_kind"] == "workflow"
    assert dashboard["orders"][0]["side"] == "buy"
    assert dashboard["orders"][0]["status"] == "filled"


def test_dca_grid_and_workflow_bots_trade_with_closed_loop_accounting(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    alice = _actor()
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=20_000,
        action_type="admin_adjust_credit",
    )
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=root,
        settings={
            "bot_auto_scan_enabled": True,
            "grid_fee_discount_percent": 0,
        },
        markets=[{"symbol": "ETH/POINTS", "fee_rate_percent": 0}],
    )

    _set_live_price(trading, symbol="ETH/POINTS", price_points=5000)
    dca_bot = trading.save_trading_bot(
        actor=alice,
        payload={
            "bot_type": "dca",
            "name": "accounting dca",
            "market_symbol": "ETH/POINTS",
            "budget_points": 100,
            "interval_hours": 24,
            "max_runs": 1,
            "enabled": True,
        },
    )["bot"]
    dca_run = trading.run_trading_bot_once(actor=alice, bot_uuid=dca_bot["bot_uuid"])

    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [
            {
                "id": "entry",
                "name": "entry",
                "priority": 10,
                "logic": "AND",
                "cooldown_seconds": 0,
                "conditions": [{"type": "price_below", "value": 6000}],
                "actions": [{"type": "buy_amount", "amount_points": 120, "step": 1}],
            },
        ],
    }
    workflow_bot = trading.save_trading_bot(
        actor=alice,
        payload={
            "bot_type": "conditional",
            "name": "accounting workflow",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "workflow_json": workflow,
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )["bot"]
    workflow_run = trading.run_trading_bot_once(actor=alice, bot_uuid=workflow_bot["bot_uuid"])

    trading.place_order(
        actor=alice,
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.1",
    )
    grid_bot = trading.create_grid_bot(
        actor=alice,
        payload={
            "name": "accounting grid",
            "market_symbol": "ETH/POINTS",
            "upper_price_points": 5100,
            "lower_price_points": 4900,
            "grid_count": 3,
            "order_amount_points": 100,
        },
    )["bot"]
    _set_live_price(trading, symbol="ETH/POINTS", price_points=4900)
    grid_buy_scan = trading.scan_grid_bots(actor=alice)
    _set_live_price(trading, symbol="ETH/POINTS", price_points=5100)
    grid_sell_scan = trading.scan_grid_bots(actor=alice)

    dashboard = trading.user_dashboard(user_id=1)
    conn = trading.get_db()
    try:
        grid_filled = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM trading_grid_orders
            WHERE grid_bot_id=(SELECT id FROM trading_grid_bots WHERE bot_uuid=?)
              AND status='filled'
            """,
            (grid_bot["bot_uuid"],),
        ).fetchone()
        reserve_events = conn.execute("SELECT COUNT(*) AS count FROM trading_reserve_pool_events").fetchone()
    finally:
        conn.close()
    chain = points.verify_chain()
    economy = points.economy_stats(verification=chain)["economy_layer"]

    assert dca_run["ok"] is True
    assert len(dca_run["triggered"]) == 1
    assert workflow_run["ok"] is True
    assert len(workflow_run["triggered"]) == 1
    assert grid_buy_scan["ok"] is True
    assert grid_sell_scan["ok"] is True
    assert int(grid_filled["count"] or 0) >= 1
    assert int(reserve_events["count"] or 0) >= 1
    assert {row["bot_type"] for row in dashboard["bots"]} >= {"dca", "conditional"}
    assert any(row.get("bot_name") == "accounting dca" for row in dashboard["orders"])
    assert any(row.get("bot_name") == "accounting workflow" for row in dashboard["orders"])
    assert economy["supply_equation"]["actual_supply_equation_gap_points"] == 0
    assert chain["ok"] is True
    assert trading.verify_state()["ok"] is True


def test_node_graph_bot_live_scan_uses_indicator_context_and_steps(tmp_path):
    _, trading = _services(tmp_path)
    trading.historical_candles_provider = lambda symbol, interval, limit: [
        {"close_points": 4000, "high_points": 4100, "low_points": 3900}
        for _ in range(60)
    ]
    workflow = {
        "version": 2,
        "strategy_kind": "workflow_graph",
        "start_node_id": "start",
        "nodes": [
            {"id": "start", "type": "start", "label": "Start"},
            {"id": "price", "type": "condition", "label": "價格低於", "condition": {"type": "price_below", "value": 6000}},
            {"id": "ma", "type": "condition", "label": "MA20 上方", "condition": {"type": "ma_position", "period": 20, "position": "above"}},
            {"id": "logic", "type": "logic", "label": "進場 AND", "operator": "AND", "priority": 10},
            {"id": "buy_1", "type": "action", "label": "第一段", "action": {"type": "buy_percent", "percent": 10, "step": 1}, "priority": 10},
            {"id": "buy_2", "type": "action", "label": "第二段", "action": {"type": "buy_percent", "percent": 20, "step": 2}, "priority": 10},
        ],
        "edges": [
            {"from": "start", "from_port": "out", "to": "price", "to_port": "in"},
            {"from": "start", "from_port": "out", "to": "ma", "to_port": "in"},
            {"from": "price", "from_port": "true", "to": "logic", "to_port": "in"},
            {"from": "ma", "from_port": "true", "to": "logic", "to_port": "in"},
            {"from": "logic", "from_port": "true", "to": "buy_1", "to_port": "in"},
            {"from": "logic", "from_port": "true", "to": "buy_2", "to_port": "in"},
        ],
    }
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "Graph live indicator buyer",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "workflow_json": workflow,
            "max_runs": 2,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )

    first = trading.run_trading_bots(actor=_actor(), limit=10)
    second = trading.run_trading_bots(actor=_actor(), limit=10)

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(first["triggered"]) == 1
    assert len(second["triggered"]) == 1
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["bots"][0]["run_count"] == 2
    assert [row["status"] for row in dashboard["orders"]] == ["filled", "filled"]
    assert len(dashboard["fills"]) == 2


def test_workflow_live_scan_uses_window_low_for_stop_loss_after_previous_scan(tmp_path):
    base_ms = 1_700_000_000_000
    candles = [
        {"time_ms": base_ms, "close_points": 100.0, "high_points": 100.0, "low_points": 100.0},
        {"time_ms": base_ms + 60_000, "close_points": 110.0, "high_points": 110.0, "low_points": 94.0},
    ]
    points, trading = _services_with_history(tmp_path, prices={"ETH/POINTS": 100.0}, candles=candles)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="1.0")
    trading.test_prices["ETH/POINTS"] = 110.0
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [
            {
                "id": "stop",
                "name": "stop",
                "priority": 100,
                "logic": "AND",
                "conditions": [{"type": "stop_loss_percent", "value": 5}],
                "actions": [{"type": "close_all", "step": 1}],
            }
        ],
    }
    bot = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "stop loss replay",
            "market_symbol": "ETH/POINTS",
            "side": "sell",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "workflow_json": workflow,
            "cooldown_seconds": 0,
            "max_runs": 2,
            "enabled": True,
        },
    )["bot"]
    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_bots SET last_scan_at=? WHERE bot_uuid=?",
            (datetime.fromtimestamp((base_ms + 30_000) / 1000).isoformat(), bot["bot_uuid"]),
        )
        conn.commit()
    finally:
        conn.close()

    scanned = trading.run_trading_bots(actor=_actor(), limit=10)

    assert scanned["ok"] is True
    assert len(scanned["triggered"]) == 1
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["positions"][0]["quantity_units"] == 0


def test_workflow_live_scan_does_not_false_trigger_from_pre_scan_window_break(tmp_path):
    base_ms = 1_700_000_000_000
    candles = [
        {"time_ms": base_ms + 60_000, "close_points": 110.0, "high_points": 110.0, "low_points": 94.0},
        {"time_ms": base_ms + 120_000, "close_points": 110.0, "high_points": 110.0, "low_points": 105.0},
    ]
    points, trading = _services_with_history(tmp_path, prices={"ETH/POINTS": 100.0}, candles=candles)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="1.0")
    trading.test_prices["ETH/POINTS"] = 110.0
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [
            {
                "id": "stop",
                "name": "stop",
                "priority": 100,
                "logic": "AND",
                "conditions": [{"type": "stop_loss_percent", "value": 5}],
                "actions": [{"type": "close_all", "step": 1}],
            }
        ],
    }
    bot = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "no false stop",
            "market_symbol": "ETH/POINTS",
            "side": "sell",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "workflow_json": workflow,
            "cooldown_seconds": 0,
            "max_runs": 2,
            "enabled": True,
        },
    )["bot"]
    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_bots SET last_scan_at=? WHERE bot_uuid=?",
            (datetime.fromtimestamp((base_ms + 120_000) / 1000).isoformat(), bot["bot_uuid"]),
        )
        conn.commit()
    finally:
        conn.close()

    scanned = trading.run_trading_bots(actor=_actor(), limit=10)

    assert scanned["ok"] is True
    assert scanned["triggered"] == []
    assert scanned["skipped"][0]["reason"] in {"workflow_not_matched", "condition_not_met"}


def test_workflow_live_scan_uses_window_high_for_take_profit(tmp_path):
    base_ms = 1_700_000_000_000
    candles = [
        {"time_ms": base_ms, "close_points": 100.0, "high_points": 100.0, "low_points": 100.0},
        {"time_ms": base_ms + 60_000, "close_points": 96.0, "high_points": 106.0, "low_points": 95.0},
    ]
    points, trading = _services_with_history(tmp_path, prices={"ETH/POINTS": 100.0}, candles=candles)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="1.0")
    trading.test_prices["ETH/POINTS"] = 96.0
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [
            {
                "id": "tp",
                "name": "take profit",
                "priority": 100,
                "logic": "AND",
                "conditions": [{"type": "take_profit_percent", "value": 5}],
                "actions": [{"type": "close_all", "step": 1}],
            }
        ],
    }
    bot = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "take profit replay",
            "market_symbol": "ETH/POINTS",
            "side": "sell",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "workflow_json": workflow,
            "cooldown_seconds": 0,
            "max_runs": 2,
            "enabled": True,
        },
    )["bot"]
    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_bots SET last_scan_at=? WHERE bot_uuid=?",
            (datetime.fromtimestamp((base_ms + 30_000) / 1000).isoformat(), bot["bot_uuid"]),
        )
        conn.commit()
    finally:
        conn.close()

    scanned = trading.run_trading_bots(actor=_actor(), limit=10)

    assert scanned["ok"] is True
    assert len(scanned["triggered"]) == 1


def test_spot_risk_target_scan_auto_closes_position(tmp_path):
    base_ms = 1_700_000_000_000
    candles = [
        {"time_ms": base_ms, "close_points": 100.0, "high_points": 100.0, "low_points": 100.0},
        {"time_ms": base_ms + 60_000, "close_points": 110.0, "high_points": 110.0, "low_points": 94.0},
    ]
    points, trading = _services_with_history(tmp_path, prices={"ETH/POINTS": 100.0}, candles=candles)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="1.0",
        stop_loss_percent=5,
        take_profit_percent=12,
    )
    trading.test_prices["ETH/POINTS"] = 110.0

    result = trading.scan_spot_risk_targets(actor={"username": "system", "role": "system"}, limit=10)

    assert result["ok"] is True
    assert len(result["triggered"]) == 1
    assert result["triggered"][0]["target_type"] == "stop_loss"
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["positions"][0]["quantity_units"] == 0


def test_margin_risk_target_scan_auto_closes_position(tmp_path):
    base_ms = 1_700_000_000_000
    candles = [
        {"time_ms": base_ms, "close_points": 100.0, "high_points": 100.0, "low_points": 100.0},
        {"time_ms": base_ms + 60_000, "close_points": 108.0, "high_points": 108.0, "low_points": 94.0},
    ]
    points, trading = _services_with_history(tmp_path, prices={"ETH/POINTS": 100.0}, candles=candles)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=10000, action_type="admin_adjust_credit")
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="1.0",
        collateral_points=50,
        stop_loss_percent=5,
        take_profit_percent=15,
        idempotency_key="margin-risk-target",
    )
    trading.test_prices["ETH/POINTS"] = 108.0

    result = trading.scan_margin_risk_targets(actor={"username": "system", "role": "system"}, limit=10)

    assert result["ok"] is True
    assert len(result["triggered"]) == 1
    assert result["triggered"][0]["position_uuid"] == opened["position"]["position_uuid"]
    conn = trading.get_db()
    try:
        row = conn.execute(
            "SELECT status FROM trading_margin_positions WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        ).fetchone()
        assert row["status"] in {"closed", "liquidated"}
    finally:
        conn.close()


def test_dca_bot_stop_loss_auto_closes_existing_position(tmp_path):
    base_ms = 1_700_000_000_000
    candles = [
        {"time_ms": base_ms, "close_points": 100.0, "high_points": 100.0, "low_points": 100.0},
        {"time_ms": base_ms + 60_000, "close_points": 108.0, "high_points": 108.0, "low_points": 94.0},
    ]
    points, trading = _services_with_history(tmp_path, prices={"ETH/POINTS": 100.0}, candles=candles)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="1.0")
    trading.test_prices["ETH/POINTS"] = 108.0
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "dca",
            "name": "dca stop",
            "market_symbol": "ETH/POINTS",
            "budget_points": 100,
            "interval_hours": 24,
            "max_runs": 2,
            "cooldown_seconds": 0,
            "enabled": True,
            "stop_loss_percent": 5,
        },
    )

    result = trading.run_trading_bots(actor=_actor(), limit=10)

    assert result["ok"] is True
    assert len(result["triggered"]) == 1
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["positions"][0]["quantity_units"] == 0


def test_trading_bot_failed_scan_does_not_advance_last_scan_at_on_live_price_error(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")

    def _fail_live_price(symbol):
        raise ValueError("live price down")

    trading = TradingEngineService(
        get_db=get_db,
        points_service=points,
        live_price_provider=_fail_live_price,
        historical_candles_provider=lambda symbol, interval, limit: [],
    )
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"max_price_staleness_seconds": 0, "price_source": "binance_public_api"},
        markets=[],
    )
    _stamp_all_markets_boot_ready(trading)
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "name": "price failure bot",
            "market_symbol": "ETH/POINTS",
            "trigger_type": "always",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "max_runs": 2,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )

    result = trading.run_trading_bots(actor=_actor(), limit=10)

    conn = trading.get_db()
    try:
        row = conn.execute("SELECT last_scan_at, last_error FROM trading_bots WHERE user_id=1 LIMIT 1").fetchone()
    finally:
        conn.close()

    assert result["ok"] is False
    assert result["failed"]
    assert row["last_scan_at"] in (None, "")
    assert "live trading price unavailable" in str(row["last_error"] or "")


def test_trading_bot_failed_scan_does_not_advance_last_scan_at_on_conservative_fusion_price(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="seed")
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "name": "conservative fusion bot",
            "market_symbol": "ETH/POINTS",
            "trigger_type": "always",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "max_runs": 2,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )

    original = trading._current_market_price_points

    def conservative_price(conn, market, *, with_meta=False, high_risk=False):
        if with_meta:
            return (
                100.0,
                "fused_weighted",
                {
                    "price_health": "conservative",
                    "fallback_reason": "可用 order book 來源不足",
                    "excluded_sources": ["binance_public_api"],
                    "warnings": [{"code": "provider_count_low", "message": "可用 order book 來源不足", "severity": "critical"}],
                    "high_risk_blocked": True,
                    "high_risk_block_reason": "目前可用來源數不足，只能提供 degraded reference price",
                },
            )
        return original(conn, market, with_meta=with_meta)

    monkeypatch.setattr(trading, "_current_market_price_points", conservative_price)

    result = trading.run_trading_bots(actor=_actor(), limit=10)

    conn = trading.get_db()
    try:
        row = conn.execute("SELECT last_scan_at, last_error FROM trading_bots WHERE user_id=1 LIMIT 1").fetchone()
    finally:
        conn.close()

    assert result["ok"] is True
    assert not result["failed"]
    assert row["last_scan_at"] not in (None, "")
    assert str(row["last_error"] or "") == ""


def test_trading_bot_failed_scan_does_not_advance_last_scan_at_on_candle_fetch_error(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")

    def _history_provider(symbol, interval, limit):
        raise ValueError("history fetch failed")

    trading = TradingEngineService(
        get_db=get_db,
        points_service=points,
        live_price_provider=lambda symbol: 100.0,
        historical_candles_provider=_history_provider,
    )
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"price_source": "binance_public_api"},
        markets=[],
    )
    _stamp_all_markets_boot_ready(trading)
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [
            {
                "id": "entry",
                "name": "entry",
                "priority": 10,
                "logic": "AND",
                "conditions": [{"type": "price_below", "value": 200}],
                "actions": [{"type": "buy_percent", "percent": 10, "step": 1}],
            }
        ],
    }
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "history failure bot",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "workflow_json": workflow,
            "max_runs": 2,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )

    result = trading.run_trading_bots(actor=_actor(), limit=10)

    conn = trading.get_db()
    try:
        row = conn.execute("SELECT last_scan_at, last_error FROM trading_bots WHERE user_id=1 LIMIT 1").fetchone()
    finally:
        conn.close()

    assert result["ok"] is False
    assert result["failed"]
    assert row["last_scan_at"] in (None, "")
    assert "history fetch failed" in str(row["last_error"] or "")


def test_workflow_backtest_can_sell_without_mutating_orders(tmp_path):
    _, trading = _services(tmp_path)
    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "workflow",
            "initial_cash_points": 1000,
            "workflow_json": {
                "version": 1,
                "branches": [
                    {
                        "id": "entry",
                        "name": "buy",
                        "priority": 10,
                        "logic": "AND",
                        "conditions": [{"type": "price_below", "value": 5000}],
                        "actions": [{"type": "buy_percent", "percent": 50, "step": 1}],
                    },
                    {
                        "id": "exit",
                        "name": "sell",
                        "priority": 20,
                        "logic": "AND",
                        "conditions": [{"type": "has_position", "value": True}, {"type": "price_above", "value": 5200}],
                        "actions": [{"type": "sell_percent", "percent": 100, "step": 1}],
                    },
                ],
            },
            "candles": [
                {"time": 1, "close_points": 4900},
                {"time": 2, "close_points": 5300},
            ],
        },
    )

    assert result["ok"] is True
    assert [row["side"] for row in result["trades"]] == ["buy", "sell"]
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["orders"] == []
    assert dashboard["fills"] == []


def test_workflow_graph_backtest_supports_nested_logic_priority_and_steps(tmp_path):
    _, trading = _services(tmp_path)
    workflow = {
        "version": 2,
        "strategy_kind": "workflow_graph",
        "start_node_id": "start",
        "nodes": [
            {"id": "start", "type": "start", "label": "Start"},
                {"id": "entry_price", "type": "condition", "label": "Nested AND/OR", "condition": {"AND": [{"type": "price_below", "value": 5000}, {"OR": [{"type": "always"}, {"type": "rsi_below", "value": 60}]}]}},
                {"id": "entry_logic", "type": "logic", "label": "巢狀進場", "operator": "AND", "priority": 10},
            {"id": "buy_1", "type": "action", "label": "第一段買入", "action": {"type": "buy_percent", "percent": 40, "step": 1}, "priority": 10},
            {"id": "buy_2", "type": "action", "label": "第二段買入", "action": {"type": "buy_percent", "percent": 40, "step": 2}, "priority": 10},
            {"id": "stop", "type": "condition", "label": "強制止損", "condition": {"type": "price_below", "value": 3000}},
            {"id": "close", "type": "action", "label": "全部平倉", "action": {"type": "close_all", "step": 1}, "priority": 100},
        ],
        "edges": [
            {"from": "start", "from_port": "out", "to": "entry_price", "to_port": "in"},
            {"from": "entry_price", "from_port": "true", "to": "entry_logic", "to_port": "in"},
            {"from": "entry_logic", "from_port": "true", "to": "buy_1", "to_port": "in"},
            {"from": "entry_logic", "from_port": "true", "to": "buy_2", "to_port": "in"},
            {"from": "start", "from_port": "out", "to": "stop", "to_port": "in"},
            {"from": "stop", "from_port": "true", "to": "close", "to_port": "in"},
        ],
    }
    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "workflow",
            "initial_cash_points": 1000,
            "workflow_json": workflow,
            "candles": [
                {"time": 1, "close_points": 4900},
                {"time": 2, "close_points": 4800},
                {"time": 3, "close_points": 2900},
            ],
        },
    )

    assert result["ok"] is True
    assert [row["side"] for row in result["trades"]] == ["buy", "buy", "sell"]
    assert result["trade_count"] == 3
    assert result["max_drawdown_percent"] >= 0
    assert len(result["equity_curve"]) >= 3


def test_backtest_trading_bot_does_not_create_orders(tmp_path):
    _, trading = _services(tmp_path)
    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "dca",
            "initial_cash_points": 1000,
            "order_points": 100,
            "interval_candles": 1,
            "candles": [
                {"time": 1, "close_points": 5000},
                {"time": 2, "close_points": 4000},
                {"time": 3, "close_points": 4500},
            ],
        },
    )

    assert result["ok"] is True
    assert result["trade_count"] == 3
    assert result["final_value_points"] > 0
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["orders"] == []
    assert dashboard["fills"] == []


def test_dca_backtest_reports_metrics_and_equity_curve(tmp_path):
    _, trading = _services(tmp_path)
    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "dca",
            "bot_config": {"order_points": 100, "interval_candles": 2},
            "initial_cash_points": 1000,
            "candles": [
                {"time": "2026-01-01T00:00:00+00:00", "close_points": 5000},
                {"time": "2026-01-01T00:15:00+00:00", "close_points": 5100},
                {"time": "2026-01-01T00:30:00+00:00", "close_points": 5200},
                {"time": "2026-01-01T00:45:00+00:00", "close_points": 5300},
            ],
        },
    )

    assert result["ok"] is True
    assert result["strategy"] == "dca"
    assert result["trade_count"] == 2
    assert len(result["equity_curve"]) == 4
    assert "return_percent" in result
    assert "win_rate_percent" in result
    assert result["candle_count"] == 4
    assert result["data_source"] == "provided_candles"
    assert result["first_candle_time"] == "2026-01-01T00:00:00+00:00"
    assert result["last_candle_time"] == "2026-01-01T00:45:00+00:00"


def test_backtest_range_filter_uses_timezone_aware_instants(tmp_path):
    _, trading = _services(tmp_path)
    candles = [
        {"time_iso": "2026-05-13T15:45:00+00:00", "close_points": 100},
        {"time_iso": "2026-05-13T16:00:00+00:00", "close_points": 101},
        {"time_iso": "2026-05-13T16:15:00+00:00", "close_points": 102},
        {"time_iso": "2026-05-13T16:30:00+00:00", "close_points": 103},
    ]

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "conditional",
            "trigger_type": "price_below",
            "trigger_price_points": 0,
            "initial_cash_points": 1000,
            "candles": candles,
            "start_time": "2026-05-14T00:00:00+08:00",
            "end_time": "2026-05-14T00:15:00+08:00",
        },
    )

    assert result["ok"] is True
    assert result["candle_count"] == 2
    assert result["first_candle_time"] == "2026-05-13T16:00:00+00:00"
    assert result["last_candle_time"] == "2026-05-13T16:15:00+00:00"


def test_workflow_backtest_applies_slippage_fee_realized_pnl_and_drawdown(tmp_path):
    _, trading = _services(tmp_path)
    workflow = {
        "version": 1,
        "branches": [
            {
                "id": "exit",
                "priority": 20,
                "logic": "AND",
                "conditions": [{"type": "has_position", "value": True}, {"type": "price_above", "value": 119}],
                "actions": [{"type": "close_all", "step": 1}],
            },
            {
                "id": "entry",
                "priority": 10,
                "logic": "AND",
                "conditions": [{"type": "has_position", "value": False}, {"type": "price_below", "value": 101}],
                "actions": [{"type": "buy_amount", "amount_points": 1000, "step": 1}],
            },
        ],
    }

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "workflow",
            "workflow_json": workflow,
            "initial_cash_points": 1000,
            "slippage_percent": 10,
            "candles": [
                {"time": 1, "high_points": 100, "low_points": 100, "close_points": 100},
                {"time": 2, "high_points": 120, "low_points": 120, "close_points": 120},
            ],
        },
    )

    buy_price = Decimal("110")
    sell_price = Decimal("108")
    notional_cap = int((Decimal("1000") / Decimal("1.001")).quantize(Decimal("1"), rounding=ROUND_DOWN))
    units = int((Decimal(notional_cap) * Decimal(trading_engine_module.ASSET_SCALE) / buy_price).quantize(Decimal("1"), rounding=ROUND_DOWN))
    buy_notional = notional_points(units, float(buy_price))
    buy_fee = fee_points(buy_notional, 0.1)
    sell_gross = notional_points(units, float(sell_price))
    sell_fee = fee_points(sell_gross, 0.1)
    sell_net = sell_gross - sell_fee
    expected_drawdown = round((1000 - notional_points(units, 100)) * 100 / 1000, 4)

    assert result["ok"] is True
    assert result["slippage_percent"] == 10
    assert [row["side"] for row in result["trades"]] == ["buy", "sell"]
    assert result["trades"][0]["price_points"] == pytest.approx(110.0)
    assert result["trades"][0]["spend_points"] == buy_notional + buy_fee == 1000
    assert result["trades"][0]["fee_points"] == buy_fee
    assert result["trades"][1]["price_points"] == pytest.approx(108.0)
    assert result["trades"][1]["fee_points"] == sell_fee
    assert result["trades"][1]["pnl_points"] == sell_net - buy_notional
    assert result["cash_points"] == sell_net
    assert result["final_value_points"] == sell_net
    assert result["pnl_points"] == sell_net - 1000
    assert result["max_drawdown_percent"] == expected_drawdown
    assert result["win_rate_percent"] == 0.0


def test_backtest_respects_market_min_order_and_quantity_precision(tmp_path):
    _, trading = _services(tmp_path)
    conn = trading.get_db()
    try:
        conn.execute(
            """
            UPDATE trading_markets
            SET fee_rate_percent=0,
                min_order_points=50,
                quantity_precision=2,
                min_order_size=0.25,
                max_order_size=1000,
                lot_size=0.25
            WHERE symbol='ETH/POINTS'
            """
        )
        conn.commit()
    finally:
        conn.close()
    candles = [
        {"time": 1, "close_points": 100},
        {"time": 2, "close_points": 100},
    ]

    below_min = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "dca",
            "order_points": 49,
            "interval_candles": 99,
            "initial_cash_points": 100,
            "candles": candles,
        },
    )
    aligned = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "dca",
            "order_points": 99,
            "interval_candles": 99,
            "initial_cash_points": 99,
            "candles": candles,
        },
    )

    assert below_min["trade_count"] == 0
    assert below_min["final_value_points"] == 100
    assert aligned["trade_count"] == 1
    assert aligned["trades"][0]["quantity"] == "0.75"
    assert aligned["trades"][0]["spend_points"] == 75
    assert aligned["cash_points"] == 24
    assert aligned["final_value_points"] == 99


def test_grid_backtest_matches_live_grid_order_lifecycle(tmp_path):
    _, trading = _services(tmp_path)
    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "grid",
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

    assert result["ok"] is True
    assert result["strategy"] == "grid"
    assert result["trade_count"] == 7
    assert result["final_value_points"] == 1065
    assert result["trades"][0]["index"] == 1
    assert [row["side"] for row in result["trades"]] == ["buy", "sell", "sell", "sell", "buy", "buy", "buy"]
    assert all(not (row["index"] == 0 and row["price_points"] == 100) for row in result["trades"])
    assert len(result["equity_curve"]) == 4


def test_grid_bot_scans_and_fills_when_price_crosses_level_in_cfd_mode(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=5000,
        action_type="admin_adjust_credit",
    )
    trading.test_prices["ETH/POINTS"] = 100
    trading.update_market(actor=root, symbol="ETH/POINTS", manual_price_points=100, confirm_jump=True)
    trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="2.0",
    )
    created = trading.create_grid_bot(
        actor=_actor(),
        payload={
            "name": "CFD grid",
            "market_symbol": "ETH/POINTS",
            "lower_price_points": 80,
            "upper_price_points": 120,
            "grid_count": 5,
            "order_amount_points": 100,
        },
    )

    assert created["ok"] is True
    trading.test_prices["ETH/POINTS"] = 85
    trading.update_market(actor=root, symbol="ETH/POINTS", manual_price_points=85, confirm_jump=True)
    scanned = trading.scan_grid_bots(actor=_actor())

    assert scanned["ok"] is True
    assert scanned["results"][0]["fills_processed"] == [
        {"level_index": 1, "side": "buy", "price_points": 90}
    ]
    assert scanned["results"][0]["counter_orders_placed"] == [
        {"level_index": 2, "side": "sell", "price_points": 100}
    ]

    bots = trading.list_grid_bots(actor=_actor())["bots"]
    bot = next(row for row in bots if row["bot_uuid"] == created["bot"]["bot_uuid"])
    level_90 = next(row for row in bot["orders"] if row["level_index"] == 1)
    level_100 = next(row for row in bot["orders"] if row["level_index"] == 2 and row["side"] == "sell")
    assert level_90["status"] == "filled"
    assert level_100["status"] == "open"

    dashboard = trading.user_dashboard(user_id=1)
    grid_fill = next(fill for fill in dashboard["fills"] if fill.get("bot_name") == "CFD grid")
    assert grid_fill["order_uuid"] == level_90["trading_order_uuid"]
    assert grid_fill["price_points"] == 90
    assert trading.verify_state()["ok"] is True


def test_grid_buy_inventory_stays_locked_until_grid_sell_uses_it(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=5000,
        action_type="admin_adjust_credit",
    )

    buy = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="limit",
        quantity="0.1",
        limit_price_points=6000,
        is_grid_order=True,
    )
    assert buy["executed"] is True
    position = trading.user_dashboard(user_id=1)["positions"][0]
    assert position["quantity_units"] == 0
    assert position["locked_quantity_units"] == 10_000_000

    with pytest.raises(ValueError, match="insufficient spot position"):
        trading.place_order(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            side="sell",
            order_type="market",
            quantity="0.1",
        )

    counter = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="sell",
        order_type="limit",
        quantity="0.1",
        limit_price_points=6000,
        is_grid_order=True,
        use_locked_inventory=True,
    )["order"]

    assert counter["status"] == "open"
    assert counter["frozen_points"] >= 1
    assert counter["chain_frozen_points"] == counter["frozen_points"]
    assert points.get_wallet(1)["points_frozen"] == counter["frozen_points"]
    position = trading.user_dashboard(user_id=1)["positions"][0]
    assert position["quantity_units"] == 0
    assert position["locked_quantity_units"] == 10_000_000

    _set_live_price(trading, symbol="ETH/POINTS", price_points=6000)
    matched = trading.match_open_limit_orders(actor={"username": "system", "role": "system"}, limit=10)

    assert matched["ok"] is True
    assert matched["matched"][0]["order_uuid"] == counter["order_uuid"]
    assert points.get_wallet(1)["points_frozen"] == 0
    position = trading.user_dashboard(user_id=1)["positions"][0]
    assert position["quantity_units"] == 0
    assert position["locked_quantity_units"] == 0
    assert trading.verify_state()["ok"] is True


def test_delete_grid_bot_can_sell_or_keep_reserved_base_inventory(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=5000,
        action_type="admin_adjust_credit",
    )
    trading.test_prices["ETH/POINTS"] = 100
    trading.update_market(actor=root, symbol="ETH/POINTS", manual_price_points=100, confirm_jump=True)
    trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="2.0",
    )
    created = trading.create_grid_bot(
        actor=_actor(),
        payload={
            "name": "grid end sell",
            "market_symbol": "ETH/POINTS",
            "lower_price_points": 80,
            "upper_price_points": 120,
            "grid_count": 3,
            "order_amount_points": 100,
        },
    )

    assert points.get_wallet(1)["points_frozen"] > 0
    deleted = trading.delete_grid_bot(
        actor=_actor(),
        bot_uuid=created["bot"]["bot_uuid"],
        base_action="sell",
    )

    assert deleted["ok"] is True
    assert deleted["base_action"] == "sell"
    assert deleted["base_quantity_units"] > 0
    assert deleted["sold_quantity_units"] == deleted["base_quantity_units"]
    assert deleted["sell_error"] == ""
    assert (deleted["sell_order"] or {})["status"] == "filled"
    assert trading.list_grid_bots(actor=_actor())["bots"] == []
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["positions"][0]["locked_quantity_units"] == 0
    assert points.get_wallet(1)["points_frozen"] == 0
    assert trading.verify_state()["ok"] is True


def test_grid_bot_scan_window_low_can_fill_buy_limit(tmp_path):
    candles = [
        {"time_ms": 1_700_000_000_000, "close_points": 100.0, "high_points": 100.0, "low_points": 90.0},
    ]
    points, trading = _services_with_history(tmp_path, prices={"ETH/POINTS": 100.0}, candles=candles)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=5000, action_type="admin_adjust_credit")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="2.0")
    created = trading.create_grid_bot(
        actor=_actor(),
        payload={
            "name": "grid window buy",
            "market_symbol": "ETH/POINTS",
            "lower_price_points": 80,
            "upper_price_points": 120,
            "grid_count": 5,
            "order_amount_points": 100,
        },
    )

    scanned = trading.scan_grid_bots(actor=_actor())

    assert scanned["ok"] is True
    assert scanned["results"][0]["scan_window_low_points"] == pytest.approx(90.0)
    assert scanned["results"][0]["fills_processed"] == [{"level_index": 1, "side": "buy", "price_points": 90}]
    assert scanned["results"][0]["counter_orders_placed"] == [{"level_index": 2, "side": "sell", "price_points": 100}]
    assert created["ok"] is True


def test_grid_bot_scan_window_high_can_fill_sell_limit(tmp_path):
    candles = [
        {"time_ms": 1_700_000_000_000, "close_points": 100.0, "high_points": 110.0, "low_points": 100.0},
    ]
    points, trading = _services_with_history(tmp_path, prices={"ETH/POINTS": 100.0}, candles=candles)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=5000, action_type="admin_adjust_credit")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="2.0")
    trading.create_grid_bot(
        actor=_actor(),
        payload={
            "name": "grid window sell",
            "market_symbol": "ETH/POINTS",
            "lower_price_points": 80,
            "upper_price_points": 120,
            "grid_count": 5,
            "order_amount_points": 100,
        },
    )

    scanned = trading.scan_grid_bots(actor=_actor())

    assert scanned["ok"] is True
    assert scanned["results"][0]["scan_window_high_points"] == pytest.approx(110.0)
    assert scanned["results"][0]["fills_processed"] == [{"level_index": 3, "side": "sell", "price_points": 110}]
    assert scanned["results"][0]["counter_orders_placed"] == [{"level_index": 2, "side": "buy", "price_points": 100}]


def test_grid_bot_create_is_all_or_nothing_when_initial_orders_fail(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=5000, action_type="admin_adjust_credit")
    trading.test_prices["ETH/POINTS"] = 100
    trading.update_market(actor=root, symbol="ETH/POINTS", manual_price_points=100, confirm_jump=True)

    with pytest.raises(ValueError, match="grid bot create failed"):
        trading.create_grid_bot(
            actor=_actor(),
            payload={
                "name": "partial grid should rollback",
                "market_symbol": "ETH/POINTS",
                "lower_price_points": 80,
                "upper_price_points": 120,
                "grid_count": 5,
                "order_amount_points": 100,
            },
        )

    assert trading.list_grid_bots(actor=_actor())["bots"] == []
    dashboard = trading.user_dashboard(user_id=1)
    assert [row for row in dashboard["orders"] if row["status"] in ("open", "partially_filled")] == []
    assert points.get_wallet(1)["points_frozen"] == 0
    assert trading.verify_state()["ok"] is True


def test_increase_trading_bot_max_runs_updates_limit(tmp_path):
    _, trading = _services(tmp_path)
    created = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "name": "runs bot",
            "market_symbol": "ETH/POINTS",
            "trigger_type": "always",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )
    bot_uuid = created["bot"]["bot_uuid"]

    updated = trading.increase_trading_bot_max_runs(actor=_actor(), bot_uuid=bot_uuid, delta=3)

    assert updated["ok"] is True
    assert updated["delta"] == 3
    assert updated["bot"]["max_runs"] == 4


def test_workflow_budget_caps_buy_percent_order_size(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=10000, action_type="admin_adjust_credit")
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [{
            "id": "entry",
            "name": "budget capped entry",
            "priority": 10,
            "logic": "AND",
            "cooldown_seconds": 0,
            "max_runs": 1,
            "conditions": [{"type": "always"}],
            "actions": [{"type": "buy_percent", "percent": 100, "step": 1, "order_type": "market"}],
        }],
    }
    created = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "budget capped workflow",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "budget_points": 100,
            "max_runs": 1,
            "cooldown_seconds": 0,
            "workflow_json": workflow,
            "enabled": True,
        },
    )

    scanned = trading.run_trading_bots(actor=_actor(), limit=10)

    assert scanned["ok"] is True
    assert len(scanned["triggered"]) == 1
    dashboard = trading.user_dashboard(user_id=1)
    order = next(row for row in dashboard["orders"] if row.get("bot_name") == "budget capped workflow")
    total_spend = notional_points(order["filled_quantity_units"], order["execution_price_points"]) + int(order["fee_points"] or 0)
    assert total_spend <= 100
    bot = next(row for row in dashboard["bots"] if row["bot_uuid"] == created["bot"]["bot_uuid"])
    assert bot["budget_cap_enabled"] is True
    assert bot["open_order_frozen_points"] == 0


def test_adjust_workflow_budget_respects_open_order_floor(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=10000, action_type="admin_adjust_credit")
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [{
            "id": "entry",
            "name": "limit entry",
            "priority": 10,
            "logic": "AND",
            "cooldown_seconds": 0,
            "max_runs": 1,
            "conditions": [{"type": "always"}],
            "actions": [{"type": "buy_amount", "amount_points": 200, "step": 1, "order_type": "limit", "limit_price_points": 4000}],
        }],
    }
    created = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "budget floor workflow",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "budget_points": 200,
            "max_runs": 1,
            "cooldown_seconds": 0,
            "workflow_json": workflow,
            "enabled": True,
        },
    )
    bot_uuid = created["bot"]["bot_uuid"]
    scanned = trading.run_trading_bots(actor=_actor(), limit=10)
    assert scanned["ok"] is True

    listed = trading.list_trading_bots(actor=_actor())["bots"][0]
    floor = int(listed["minimum_budget_points"] or 0)
    assert floor > 0
    assert listed["open_order_frozen_points"] == floor

    with pytest.raises(ValueError, match="workflow budget cannot be lower"):
        trading.adjust_trading_bot_budget(actor=_actor(), bot_uuid=bot_uuid, budget_points=floor - 1)

    removed_cap = trading.adjust_trading_bot_budget(actor=_actor(), bot_uuid=bot_uuid, budget_points=0)
    assert removed_cap["bot"]["budget_points"] == 0
    increased = trading.adjust_trading_bot_budget(actor=_actor(), bot_uuid=bot_uuid, budget_points=floor + 50)
    assert increased["bot"]["budget_points"] == floor + 50
    assert increased["bot"]["minimum_budget_points"] == floor


def test_dca_bot_accepts_unlimited_max_runs_and_can_continue_running(tmp_path):
    _, trading = _services(tmp_path)
    created = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "dca",
            "name": "unlimited dca",
            "market_symbol": "ETH/POINTS",
            "budget_points": 100,
            "interval_hours": 24,
            "max_runs": -1,
            "enabled": True,
        },
    )
    bot_uuid = created["bot"]["bot_uuid"]

    assert created["bot"]["max_runs"] == -1

    first = trading.run_trading_bots(actor=_actor(), limit=10)
    assert first["ok"] is True
    assert len(first["triggered"]) == 1

    conn = trading.get_db()
    try:
        conn.execute("UPDATE trading_bots SET last_run_at='2000-01-01T00:00:00' WHERE bot_uuid=?", (bot_uuid,))
        conn.commit()
    finally:
        conn.close()

    second = trading.run_trading_bots(actor=_actor(), limit=10)
    assert second["ok"] is True
    assert len(second["triggered"]) == 1

    dashboard = trading.user_dashboard(user_id=1)
    bot = next(row for row in dashboard["bots"] if row["bot_uuid"] == bot_uuid)
    assert bot["max_runs"] == -1
    assert bot["run_count"] == 2
    assert bot["can_run"] is True


def test_increase_trading_bot_max_runs_is_noop_for_unlimited_dca(tmp_path):
    _, trading = _services(tmp_path)
    created = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "dca",
            "name": "unlimited dca",
            "market_symbol": "ETH/POINTS",
            "budget_points": 100,
            "interval_hours": 24,
            "max_runs": -1,
            "enabled": True,
        },
    )

    updated = trading.increase_trading_bot_max_runs(actor=_actor(), bot_uuid=created["bot"]["bot_uuid"], delta=3)

    assert updated["ok"] is True
    assert updated["delta"] == 0
    assert updated["unlimited"] is True
    assert updated["bot"]["max_runs"] == -1


def test_dca_keeps_outer_risk_targets_but_workflow_does_not(tmp_path):
    _, trading = _services(tmp_path)

    dca = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "dca",
            "name": "risk dca",
            "market_symbol": "ETH/POINTS",
            "budget_points": 100,
            "interval_hours": 24,
            "max_runs": 3,
            "stop_loss_percent": 5,
            "take_profit_percent": 12,
            "enabled": False,
        },
    )["bot"]
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [
            {
                "id": "entry",
                "logic": "AND",
                "conditions": [{"type": "price_below", "value": 1000}],
                "actions": [{"type": "buy_amount", "amount_points": 100, "step": 1}],
            }
        ],
    }
    workflow_bot = trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "workflow-owned-risk",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "workflow_json": workflow,
            "max_runs": 3,
            "cooldown_seconds": 0,
            "stop_loss_percent": 5,
            "take_profit_percent": 12,
            "enabled": False,
        },
    )["bot"]

    assert dca["stop_loss_percent"] == 5.0
    assert dca["take_profit_percent"] == 12.0
    assert workflow_bot["stop_loss_percent"] is None
    assert workflow_bot["take_profit_percent"] is None


def test_workflow_backtest_supports_take_profit_and_stop_loss_percent(tmp_path):
    _, trading = _services(tmp_path)
    workflow = {
        "version": 2,
        "strategy_kind": "workflow_graph",
        "start_node_id": "start",
        "nodes": [
            {"id": "start", "type": "start", "label": "Start"},
            {"id": "entry", "type": "condition", "label": "entry", "condition": {"type": "price_below", "value": 100}},
            {"id": "buy", "type": "action", "label": "buy", "action": {"type": "buy_percent", "percent": 50, "step": 1}},
            {"id": "profit", "type": "condition", "label": "take profit", "condition": {"type": "take_profit_percent", "value": 10}},
            {"id": "sell", "type": "action", "label": "sell half", "priority": 50, "action": {"type": "sell_percent", "percent": 50, "step": 1}},
            {"id": "loss", "type": "condition", "label": "stop loss", "condition": {"type": "stop_loss_percent", "value": 5}},
            {"id": "close", "type": "action", "label": "close", "priority": 100, "action": {"type": "close_all", "step": 1}},
        ],
        "edges": [
            {"from": "start", "from_port": "out", "to": "entry", "to_port": "in"},
            {"from": "entry", "from_port": "true", "to": "buy", "to_port": "in"},
            {"from": "start", "from_port": "out", "to": "profit", "to_port": "in"},
            {"from": "profit", "from_port": "true", "to": "sell", "to_port": "in"},
            {"from": "start", "from_port": "out", "to": "loss", "to_port": "in"},
            {"from": "loss", "from_port": "true", "to": "close", "to_port": "in"},
        ],
    }

    result = trading.backtest_trading_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "strategy": "workflow",
            "initial_cash_points": 1000,
            "workflow_json": workflow,
            "candles": [
                {"time": 1, "close_points": 90},
                {"time": 2, "close_points": 110},
                {"time": 3, "close_points": 80},
            ],
        },
    )

    assert result["ok"] is True
    assert [row["side"] for row in result["trades"]] == ["buy", "sell", "sell"]
    assert result["trade_count"] == 3


def test_backtest_trading_bot_rejects_excessive_candle_count(tmp_path):
    _, trading = _services(tmp_path)

    with pytest.raises(ValueError, match="candles length"):
        trading.backtest_trading_bot(
            actor=_actor(),
            payload={
                "market_symbol": "ETH/POINTS",
                "strategy": "dca",
                "candles": [
                    {"time": index, "close_points": 5000}
                    for index in range(trading_engine_module.MAX_BACKTEST_CANDLES + 1)
                ],
            },
        )


def test_insufficient_trading_balance_creates_notification(tmp_path):
    _, trading = _services(tmp_path)

    with pytest.raises(ValueError, match="交易資金不足"):
        trading.place_order(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            side="buy",
            order_type="market",
            quantity="0.3",
        )

    notes = _notifications(trading, 1)
    assert notes[-1]["type"] == "trading_balance_insufficient"
    assert notes[-1]["title"] == "交易未成立：餘額不足"
    assert "ETH/POINTS buy market 數量 0.3 未成立" in notes[-1]["body"]
    assert "需要" in notes[-1]["body"]
    assert "目前可用" in notes[-1]["body"]


def test_emergency_market_close_sells_all_with_double_fee(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.1",
    )

    close = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="sell",
        order_type="market",
        quantity="0.1",
        emergency_close=True,
    )

    assert close["order"]["status"] == "filled"
    assert close["order"]["reason"] == "EMERGENCY_MARKET_CLOSE"
    assert close["order"]["fee_points"] == 2
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["positions"][0]["quantity"] == "0"
    assert dashboard["fills"][0]["fee_points"] == 2
    assert points.get_wallet(1)["points_balance"] == 2000
    assert dashboard["funding"]["trial_credit"]["available_points"] == 998
    assert dashboard["funding"]["trial_credit"]["deployed_points"] == 0
    with pytest.raises(ValueError, match="emergency close only supports market sell"):
        trading.place_order(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            side="sell",
            order_type="limit",
            quantity="0.1",
            limit_price_points=5000,
            emergency_close=True,
        )


def test_trial_credit_expiry_reclaims_principal_but_keeps_profit(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.1")

    _set_live_price(trading, symbol="ETH/POINTS", price_points=6000)
    conn = trading.get_db()
    conn.execute("UPDATE trading_trial_credits SET expires_at='2000-01-01T00:00:00' WHERE user_id=1")
    conn.commit()
    conn.close()

    dashboard = trading.user_dashboard(user_id=1)

    assert dashboard["funding"]["trial_credit"]["status"] == "expired"
    assert dashboard["funding"]["trial_credit"]["available_points"] == 0
    assert dashboard["funding"]["trial_credit"]["deployed_points"] == 0
    assert dashboard["positions"][0]["quantity"] == "0"
    assert points.get_wallet(1)["points_balance"] == 98
    report = trading.root_report()
    audit_types = [row["event_type"] for row in report["audit_events"]]
    assert "TRADING_TRIAL_CREDIT_FORCED_SELL" in audit_types
    assert "TRADING_TRIAL_CREDIT_RECLAIMED" in audit_types


def test_trial_credit_expiry_cancels_open_sell_orders_before_reclaim(tmp_path):
    points, trading = _services(tmp_path)
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.1")
    sell = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="sell",
        order_type="limit",
        quantity="0.1",
        limit_price_points=9000,
    )["order"]
    assert sell["status"] == "open"
    assert trading.user_dashboard(user_id=1)["positions"][0]["locked_quantity_units"] == 10000000

    conn = trading.get_db()
    conn.execute("UPDATE trading_trial_credits SET expires_at='2000-01-01T00:00:00' WHERE user_id=1")
    conn.commit()
    conn.close()

    dashboard = trading.user_dashboard(user_id=1)

    assert dashboard["funding"]["trial_credit"]["status"] == "expired"
    assert dashboard["funding"]["trial_credit"]["available_points"] == 0
    assert dashboard["funding"]["trial_credit"]["deployed_points"] == 0
    assert dashboard["positions"][0]["quantity"] == "0"
    orders = {row["order_uuid"]: row for row in dashboard["orders"]}
    assert orders[sell["order_uuid"]]["status"] == "cancelled"
    audit_types = [row["event_type"] for row in trading.root_report()["audit_events"]]
    assert "TRADING_TRIAL_CREDIT_SELL_ORDER_CANCELLED" in audit_types
    assert "TRADING_TRIAL_CREDIT_FORCED_SELL" in audit_types
    assert trading.verify_state()["ok"] is True


def test_spot_dashboard_reports_backend_pnl_and_fees(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)

    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.1")
    _set_live_price(trading, symbol="ETH/POINTS", price_points=6000)
    dashboard_after_buy = trading.user_dashboard(user_id=1)
    position = dashboard_after_buy["positions"][0]
    assert position["quantity"] == "0.1"
    assert position["gross_cost_points"] == 500
    assert position["current_value_points"] == 600
    assert position["estimated_buy_fee_points"] == 1
    assert position["estimated_exit_fee_points"] == 1
    assert position["settlement_fee_points"] == 2
    assert position["cost_basis_points"] == 502
    assert position["unrealized_pnl_points"] == 98
    assert dashboard_after_buy["spot_summary"]["unrealized_pnl_points"] == 98
    wallet_after_buy = points.get_wallet(1)
    assert wallet_after_buy["points_balance"] == 1500
    assert wallet_after_buy["total_points_earned"] == 2000
    assert wallet_after_buy["total_points_spent"] == 0

    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="sell", order_type="market", quantity="0.04")

    dashboard_after_sell = trading.user_dashboard(user_id=1)
    position_after_sell = dashboard_after_sell["positions"][0]
    assert position_after_sell["quantity"] == "0.06"
    assert position_after_sell["gross_cost_points"] == 300
    assert position_after_sell["current_value_points"] == 360
    assert position_after_sell["estimated_buy_fee_points"] == 1
    assert position_after_sell["estimated_exit_fee_points"] == 1
    assert position_after_sell["settlement_fee_points"] == 1
    assert position_after_sell["cost_basis_points"] == 301
    assert position_after_sell["unrealized_pnl_points"] == 59
    assert position_after_sell["realized_pnl_points"] == 39
    assert position_after_sell["total_fee_points"] == 1
    assert dashboard_after_sell["spot_summary"]["realized_pnl_points"] == 39
    assert dashboard_after_sell["fills"][0]["realized_pnl_points"] == 39
    wallet_after_sell = points.get_wallet(1)
    assert wallet_after_sell["points_balance"] == 1739
    assert wallet_after_sell["total_points_earned"] == 2039
    assert wallet_after_sell["total_points_spent"] == 0
    assert trading.verify_state()["ok"] is True


def test_spot_wallet_cumulative_statement_counts_realized_loss_not_principal(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)

    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.1")
    wallet_after_buy = points.get_wallet(1)
    assert wallet_after_buy["points_balance"] == 500
    assert wallet_after_buy["total_points_earned"] == 1000
    assert wallet_after_buy["total_points_spent"] == 0

    _set_live_price(trading, symbol="ETH/POINTS", price_points=4000)
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="sell", order_type="market", quantity="0.1")

    wallet_after_loss = points.get_wallet(1)
    assert wallet_after_loss["points_balance"] == 899
    assert wallet_after_loss["total_points_earned"] == 1000
    assert wallet_after_loss["total_points_spent"] == 101
    fill = trading.user_dashboard(user_id=1)["fills"][0]
    assert fill["realized_pnl_points"] == -101
    assert trading.verify_state()["ok"] is True


def test_market_order_uses_live_price_instead_of_stale_manual_price(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=500, action_type="admin_adjust_credit")

    result = trading.place_order(
        actor=_actor(),
        market_symbol="BTC/POINTS",
        side="buy",
        order_type="market",
        quantity="0.001",
    )

    assert result["order"]["status"] == "filled"
    assert result["order"]["execution_price_points"] == 77059
    fills = trading.user_dashboard(user_id=1)["fills"]
    assert fills[0]["price_points"] == 77059
    assert points.get_wallet(1)["points_balance"] == 500


def test_live_price_failure_uses_recent_last_good_price(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")

    first = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.01",
    )
    assert first["order"]["execution_price_points"] == 5000

    def fail_price(symbol):
        raise RuntimeError("price feed down")

    trading.live_price_provider = fail_price
    second = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.01",
    )

    assert second["order"]["status"] == "filled"
    assert trading.verify_state()["ok"] is True


def test_live_price_provider_allows_market_order_when_binance_is_down(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.update_root_settings(actor=_actor(3, "root", "super_admin"), settings={"price_source": "binance_public_api"}, markets=[])
    _stamp_all_markets_boot_ready(trading)
    urls = []

    def fake_urlopen(request, timeout=0):
        urls.append(request.full_url)
        if "api.binance.com" in request.full_url:
            raise OSError("binance down")
        if "api.exchange.coinbase.com" in request.full_url:
            return _FakePriceResponse({"price": "5100"})
        raise AssertionError(f"unexpected fallback URL: {request.full_url}")

    monkeypatch.setattr(trading_engine_module, "urlopen", fake_urlopen)

    result = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.01",
    )

    assert result["order"]["status"] == "filled"
    market = trading.list_markets()[1]
    assert market["symbol"] == "ETH/POINTS"


def test_live_price_provider_allows_market_order_when_public_fallback_chain_is_unhealthy(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.update_root_settings(actor=_actor(3, "root", "super_admin"), settings={"price_source": "binance_public_api"}, markets=[])
    _stamp_all_markets_boot_ready(trading)
    urls = []

    def fake_urlopen(request, timeout=0):
        urls.append(request.full_url)
        if "bitstamp.net" in request.full_url:
            return _FakePriceResponse({"last": "5100"})
        raise OSError("provider unavailable")

    monkeypatch.setattr(trading_engine_module, "urlopen", fake_urlopen)

    result = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.01",
    )

    assert result["order"]["status"] == "filled"
    market = trading.list_markets()[1]
    assert market["symbol"] == "ETH/POINTS"


def test_default_binance_price_source_does_not_fetch_fusion_orderbooks(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    _stamp_all_markets_boot_ready(trading)

    def binance_price(_symbol, *, settings=None, with_meta=False, conn=None):
        meta = {
            "transport": "http_polling",
            "connected": False,
            "fallback": False,
            "stale": False,
            "degraded": False,
            "confidence": "medium",
            "provider_count": 1,
            "last_update_at": trading_engine_module._now(),
            "exclusion_reason": "",
            "latency_ms": 10.0,
        }
        return (5100.0, meta) if with_meta else 5100.0

    def fused_should_not_run(*_args, **_kwargs):
        pytest.fail("default Binance source must not fetch fused order books while Binance is healthy")

    monkeypatch.setattr(trading, "_fetch_binance_price_points", binance_price)
    monkeypatch.setattr(trading, "_fetch_weighted_fused_price_points", fused_should_not_run)

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market(conn, "ETH/POINTS")
        price, source, meta = trading._current_market_price_points(conn, market, with_meta=True, high_risk=True)
    finally:
        conn.close()

    assert price == 5100.0
    assert source == "binance_public_api"
    assert meta["risk_grade_usable"] is True, json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2)
    assert meta["risk_grade_provider_count"] == 1


def test_binance_failure_uses_fused_fallback_with_one_healthy_provider(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "binance_public_api",
            "price_fusion_min_provider_count": 1,
            "price_fusion_trade_min_provider_count": 1,
            "price_stream_ws_enabled": False,
        },
        markets=[],
    )
    _stamp_all_markets_boot_ready(trading)

    def unavailable(*_args, **_kwargs):
        raise OSError("provider unavailable")

    monkeypatch.setattr(trading, "_fetch_binance_price_points", unavailable)
    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", unavailable)
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol, **_kwargs: _depth_snapshot(trading, "okx_public_api", 3, price=5100.0))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", unavailable)
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", unavailable)
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", unavailable)
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", unavailable)

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market(conn, "ETH/POINTS")
        price, source, meta = trading._current_market_price_points(conn, market, with_meta=True, high_risk=True)
    finally:
        conn.close()

    assert price == pytest.approx(5102.55, abs=0.01)
    assert source == "fused_weighted"
    assert meta["risk_grade_usable"] is True, json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2)
    assert meta["risk_grade_provider_count"] == 1
    assert meta["high_risk_blocked"] is False
    assert any(item.get("code") == "primary_price_source_failed_fused_fallback" for item in meta["warnings"])


def test_live_price_fusion_auto_depth_weights_surviving_exchanges(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "fused_weighted",
            "price_fusion_mode": "auto_depth",
            "price_stream_ws_enabled": False,
        },
        markets=[],
    )

    def boom(_market_symbol):
        raise OSError("provider unavailable")

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "binance_public_api", 1, price=100.0))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "okx_public_api", 3, price=101.0))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", boom)

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market(conn, "ETH/POINTS")
        price, source = trading._current_market_price_points(conn, market)
        updated = trading._market(conn, "ETH/POINTS")
    finally:
        conn.close()

    assert 100.0 < price < 101.1
    assert source == "fused_weighted"
    assert updated["manual_price_points"] == pytest.approx(price, abs=0.00000001)
    assert updated["price_source"] == "fused_weighted"


def test_root_trading_settings_default_to_binance_with_fused_fallback_controls(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)

    settings = trading.get_root_settings()["settings"]
    markets = trading.list_markets()

    assert settings["price_source"] == "binance_public_api"
    assert settings["price_fusion_mode"] == "auto_depth"
    assert settings["price_fusion_live_markets"] == ["BTC/POINTS", "ETH/POINTS", "XRP/POINTS", "BNB/POINTS", "PAXG/POINTS"]
    assert settings["price_fusion_depth_band_percent"] == 1.0
    assert settings["price_fusion_depth_levels"] == 100
    assert settings["price_fusion_min_orderbook_coverage_percent"] == 0.5
    assert settings["price_fusion_max_single_provider_weight_percent"] == 40.0
    assert settings["price_fusion_min_provider_count"] == 1
    assert settings["price_fusion_trade_min_provider_count"] == 1
    assert settings["price_fusion_manual_weights"] == {
        "binance_public_api": 40.0,
        "okx_public_api": 25.0,
        "coinbase_exchange": 15.0,
        "kraken_public_api": 10.0,
        "bitstamp_public_api": 8.0,
        "gemini_public_api": 2.0,
    }
    assert [row["symbol"] for row in markets] == ["BTC/POINTS", "ETH/POINTS", "XRP/POINTS", "BNB/POINTS", "PAXG/POINTS"]
    assert markets[0]["display_symbol"] == "BTC/USDT"
    assert markets[0]["live_price_supported"] is True
    assert markets[0]["btc_trade_supported"] is True
    assert markets[1]["display_symbol"] == "ETH/USDT"
    assert markets[1]["btc_trade_supported"] is False
    assert markets[2]["display_symbol"] == "XRP/USDT"
    assert markets[3]["display_symbol"] == "BNB/USDT"
    assert markets[4]["display_symbol"] == "PAXG/USDT"


def test_root_update_market_accepts_display_usdt_symbol(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")

    updated = trading.update_market(actor=root, symbol="ETH/USDT", max_price_jump_percent=123.0)

    assert updated["ok"] is True
    assert updated["market"]["symbol"] == "ETH/POINTS"
    assert updated["market"]["display_symbol"] == "ETH/USDT"
    assert updated["market"]["max_price_jump_percent"] == pytest.approx(123.0)


def test_price_fusion_auto_depth_status_uses_depth_scores_and_sums_to_hundred(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "fused_weighted",
            "price_fusion_mode": "auto_depth",
            "price_stream_ws_enabled": False,
        },
        markets=[],
    )

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "binance_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "okx_public_api", 2))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "coinbase_exchange", 3))
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "kraken_public_api", 4))
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "gemini_public_api", 5))
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "bitstamp_public_api", 6))

    status = trading.get_root_price_fusion_status(market_symbol="BTC/POINTS")
    used = {row["source"]: row for row in status["providers_used"]}

    assert status["configured_source"] == "fused_weighted"
    assert status["requested_mode"] == "auto_depth"
    assert status["resolved_mode"] == "auto_depth"
    assert status["state"] == "healthy"
    assert status["degraded"] is False
    assert status["excluded_providers"] == []
    assert status["depth_levels"] == 100
    assert status["max_single_provider_weight_percent"] == 40.0
    assert status["median_midpoint_points"] == pytest.approx(100.05, abs=0.0001)
    assert status["weights_sum_percent"] == pytest.approx(100.0, abs=0.01)
    assert used["bitstamp_public_api"]["normalized_weight_percent"] > used["gemini_public_api"]["normalized_weight_percent"] > used["kraken_public_api"]["normalized_weight_percent"] > used["coinbase_exchange"]["normalized_weight_percent"] > used["okx_public_api"]["normalized_weight_percent"] > used["binance_public_api"]["normalized_weight_percent"]
    assert used["binance_public_api"]["normalized_weight_percent"] == pytest.approx(100.0 / 21.0, abs=0.01)
    assert used["bitstamp_public_api"]["normalized_weight_percent"] == pytest.approx((6.0 / 21.0) * 100.0, abs=0.01)
    assert used["binance_public_api"]["best_bid_points"] == pytest.approx(100.0, abs=0.0001)
    assert used["binance_public_api"]["best_ask_points"] == pytest.approx(100.1, abs=0.0001)
    assert used["binance_public_api"]["spread_percent"] == pytest.approx(0.09995, abs=0.0002)
    assert used["binance_public_api"]["bid_notional_points"] == pytest.approx(1000.0, abs=0.01)
    assert used["binance_public_api"]["ask_notional_points"] == pytest.approx(1001.0, abs=0.02)
    assert used["binance_public_api"]["bid_coverage_percent"] == pytest.approx(1.0, abs=0.0001)
    assert used["binance_public_api"]["ask_coverage_percent"] == pytest.approx(1.0, abs=0.0001)
    assert used["binance_public_api"]["bid_reached_lower_bound"] is True
    assert used["binance_public_api"]["ask_reached_upper_bound"] is True
    assert used["binance_public_api"]["orderbook_truncated"] is False
    assert used["binance_public_api"]["effective_depth_score"] == pytest.approx(used["binance_public_api"]["depth_score"], abs=0.0001)
    assert used["binance_public_api"]["reference_weight_percent"] == pytest.approx(used["binance_public_api"]["normalized_weight_percent"], abs=0.0001)
    assert used["binance_public_api"]["risk_grade_weight_percent"] == pytest.approx(used["binance_public_api"]["normalized_weight_percent"], abs=0.0001)
    assert used["binance_public_api"]["depth_density_score"] >= used["binance_public_api"]["depth_score"]
    assert used["binance_public_api"]["latency_ms"] == pytest.approx(120.0, abs=0.01)
    assert used["binance_public_api"]["quantity_unit"] == "base_asset"
    assert used["binance_public_api"]["quantity_unit_confirmed"] is True


def test_price_fusion_status_marks_truncated_and_excludes_insufficient_coverage(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "fused_weighted",
            "price_fusion_mode": "auto_depth",
            "price_fusion_depth_band_percent": 1.0,
            "price_fusion_min_orderbook_coverage_percent": 0.5,
            "price_fusion_min_provider_count": 2,
            "price_stream_ws_enabled": False,
        },
        markets=[],
    )

    def partial(source, *, min_bid, max_ask):
        return trading._build_orderbook_snapshot(
            source=source,
            bids=[[100.0, 10.0], [min_bid, 8.0]],
            asks=[[100.1, 10.0], [max_ask, 8.0]],
            fetch_meta={"fetched_at": trading_engine_module._now(), "latency_ms": 100.0},
            max_levels=100,
            band_percent=1.0,
        )

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: partial("binance_public_api", min_bid=99.8, max_ask=100.3))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: partial("okx_public_api", min_bid=99.0, max_ask=101.1))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", lambda _symbol: partial("coinbase_exchange", min_bid=99.0, max_ask=101.1))
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", lambda _symbol: partial("kraken_public_api", min_bid=99.0, max_ask=101.1))
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", lambda _symbol: partial("gemini_public_api", min_bid=99.0, max_ask=101.1))
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", lambda _symbol: partial("bitstamp_public_api", min_bid=99.0, max_ask=101.1))

    status = trading.get_root_price_fusion_status(market_symbol="BTC/POINTS")
    used = {row["source"]: row for row in status["providers_used"]}

    assert "binance_public_api" in used
    assert used["binance_public_api"]["orderbook_truncated"] is True
    assert used["binance_public_api"]["bid_coverage_percent"] < 0.5
    assert used["binance_public_api"]["risk_grade_eligible"] is False
    assert used["binance_public_api"]["risk_grade_weight_percent"] == pytest.approx(0.0, abs=0.0001)
    assert "資料截斷，不代表該交易所真實深度不足" in used["binance_public_api"]["coverage_warning_message"]
    assert "okx_public_api" in used
    assert used["okx_public_api"]["orderbook_truncated"] is False
    assert status["state"] == "healthy"
    assert status["degraded"] is False
    assert {item["code"] for item in status["warnings"]} >= {"provider_coverage_partial"}
    assert status["reference_provider_count"] == 6
    assert status["risk_grade_provider_count"] == 5


def test_price_fusion_effective_score_keeps_zero_without_restoring_raw_depth_score(tmp_path):
    _points, trading = _services(tmp_path)

    assert trading._price_fusion_effective_score({"effective_depth_score": 0.0, "depth_score": 123.45}) == pytest.approx(0.0)


def test_price_fusion_conservative_mode_keeps_coverage_and_provider_count_warnings(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "fused_weighted",
            "price_fusion_mode": "auto_depth",
            "price_fusion_depth_band_percent": 1.0,
            "price_fusion_depth_levels": 100,
            "price_fusion_min_orderbook_coverage_percent": 0.5,
            "price_fusion_min_provider_count": 3,
        },
        markets=[],
    )

    def barely_qualified(source):
        return trading._build_orderbook_snapshot(
            source=source,
            bids=[[100.0, 10.0], [99.22, 8.0]],
            asks=[[100.1, 10.0], [100.95, 8.0]],
            fetch_meta={"fetched_at": trading_engine_module._now(), "latency_ms": 120.0},
            max_levels=100,
            band_percent=1.0,
            request_limit=100,
        )

    def insufficient(source):
        return trading._build_orderbook_snapshot(
            source=source,
            bids=[[100.0, 10.0], [99.95, 8.0]],
            asks=[[100.1, 10.0], [100.15, 8.0]],
            fetch_meta={"fetched_at": trading_engine_module._now(), "latency_ms": 120.0},
            max_levels=100,
            band_percent=1.0,
            request_limit=100,
        )

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: insufficient("binance_public_api"))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: insufficient("okx_public_api"))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", lambda _symbol: insufficient("coinbase_exchange"))
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", lambda _symbol: insufficient("kraken_public_api"))
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", lambda _symbol: insufficient("gemini_public_api"))
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", lambda _symbol: barely_qualified("bitstamp_public_api"))

    status = trading.get_root_price_fusion_status(market_symbol="BTC/POINTS")

    assert status["state"] == "conservative"
    assert status["high_risk_blocked"] is True
    used = {row["source"]: row for row in status["providers_used"]}
    assert used["bitstamp_public_api"]["risk_grade_eligible"] is True
    assert used["bitstamp_public_api"]["risk_grade_weight_percent"] == pytest.approx(100.0, abs=0.01)
    assert used["binance_public_api"]["risk_grade_eligible"] is False
    assert used["binance_public_api"]["reference_weight_percent"] > 0
    assert used["binance_public_api"]["risk_grade_weight_percent"] == pytest.approx(0.0, abs=0.0001)
    assert status["reference_provider_count"] == 6
    assert status["risk_grade_provider_count"] == 1
    assert {item["code"] for item in status["warnings"]} >= {"provider_coverage_partial", "provider_count_low"}
    assert "可用 order book 來源只剩 1 家" in status["message"]


def test_market_order_allows_conservative_fusion_price(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="seed")

    def conservative_price(conn, market, *, with_meta=False, high_risk=False):
        meta = {
            "price_health": "conservative",
            "fallback_reason": "可用 order book 來源不足",
            "excluded_sources": ["binance_public_api", "okx_public_api"],
            "warnings": [{"code": "provider_count_low", "message": "可用 order book 來源不足", "severity": "critical"}],
            "high_risk_blocked": True,
            "high_risk_block_reason": "目前可用來源數不足，只能提供 degraded reference price",
        }
        return (100.0, "fused_weighted", meta) if with_meta else (100.0, "fused_weighted")

    monkeypatch.setattr(trading, "_current_market_price_points", conservative_price)

    result = trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.01")

    assert result["order"]["status"] == "filled"
    assert result["order"]["execution_price_points"] == 100


def test_market_order_pauses_conservative_fusion_price_when_root_policy_enabled(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="seed")
    _set_trading_setting(trading, "trading.price_degrade_pause_market_orders", "true")

    def conservative_price(conn, market, *, with_meta=False, high_risk=False):
        meta = {
            "price_health": "conservative",
            "conservative_mode": True,
            "fallback_reason": "可用 order book 來源不足",
            "warnings": [{"code": "provider_count_low", "message": "可用 order book 來源不足", "severity": "critical"}],
            "high_risk_blocked": True,
            "high_risk_block_reason": "目前可用來源數不足，只能提供 degraded reference price",
            "risk_grade_provider_count": 1,
        }
        return (100.0, "fused_weighted", meta) if with_meta else (100.0, "fused_weighted")

    monkeypatch.setattr(trading, "_current_market_price_points", conservative_price)

    with pytest.raises(ValueError, match="價格降級暫停|price health degraded"):
        trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.01")


def test_grid_bot_create_pauses_conservative_fusion_price_when_root_policy_enabled(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="seed")
    _set_trading_setting(trading, "trading.price_degrade_pause_bots", "true")

    def conservative_price(conn, market, *, with_meta=False, high_risk=False):
        meta = {
            "price_health": "conservative",
            "conservative_mode": True,
            "fallback_reason": "可用 order book 來源不足",
            "warnings": [{"code": "provider_count_low", "message": "可用 order book 來源不足", "severity": "critical"}],
            "high_risk_blocked": True,
            "high_risk_block_reason": "目前可用來源數不足，只能提供 degraded reference price",
            "risk_grade_provider_count": 1,
        }
        return (100.0, "fused_weighted", meta) if with_meta else (100.0, "fused_weighted")

    monkeypatch.setattr(trading, "_current_market_price_points", conservative_price)

    with pytest.raises(ValueError, match="價格降級暫停|price health degraded"):
        trading.create_grid_bot(
            actor=_actor(),
            payload={
                "name": "paused conservative grid",
                "market_symbol": "ETH/POINTS",
                "lower_price_points": 90,
                "upper_price_points": 110,
                "grid_count": 3,
                "order_amount_points": 50,
            },
        )


def test_margin_open_allows_conservative_fusion_price(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=10000, action_type="seed")
    trading.update_root_settings(actor=_actor(3, "root", "super_admin"), settings={"borrowing_enabled": True}, markets=[])

    def conservative_price(conn, market, *, with_meta=False, high_risk=False):
        meta = {
            "price_health": "conservative",
            "fallback_reason": "可用 order book 來源不足",
            "excluded_sources": ["binance_public_api"],
            "warnings": [{"code": "provider_count_low", "message": "可用 order book 來源不足", "severity": "critical"}],
            "high_risk_blocked": True,
            "high_risk_block_reason": "目前可用來源數不足，只能提供 degraded reference price",
        }
        return (5000.0, "fused_weighted", meta) if with_meta else (5000.0, "fused_weighted")

    monkeypatch.setattr(trading, "_current_market_price_points", conservative_price)

    result = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    assert result["position"]["status"] == "open"


def test_root_trading_settings_persist_price_fusion_coverage_controls(tmp_path):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")

    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "fused_weighted",
            "price_fusion_mode": "auto_depth",
            "price_fusion_depth_band_percent": 1.5,
            "price_fusion_depth_levels": 1000,
            "price_fusion_min_orderbook_coverage_percent": 0.75,
            "price_fusion_max_single_provider_weight_percent": 35,
            "price_fusion_min_provider_count": 4,
        },
        markets=[],
    )

    settings = trading.get_root_settings()["settings"]
    assert settings["price_fusion_depth_band_percent"] == 1.5
    assert settings["price_fusion_depth_levels"] == 1000
    assert settings["price_fusion_min_orderbook_coverage_percent"] == 0.75
    assert settings["price_fusion_max_single_provider_weight_percent"] == 35.0
    assert settings["price_fusion_min_provider_count"] == 4


def test_price_fusion_status_and_audit_mark_excluded_failed_sources(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(actor=root, settings={"price_source": "fused_weighted", "price_fusion_mode": "auto_depth"}, markets=[])

    def boom(_symbol):
        raise TimeoutError("timeout")

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "binance_public_api", 2))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "okx_public_api", 3))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "gemini_public_api", 5))
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "bitstamp_public_api", 7))

    status = trading.get_root_price_fusion_status(market_symbol="ETH/POINTS")
    excluded = {row["source"]: row for row in status["excluded_providers"]}
    assert status["state"] == "degraded"
    assert status["degraded"] is True
    assert status["weights_sum_percent"] == pytest.approx(100.0, abs=0.01)
    assert excluded["coinbase_exchange"]["reason"] == "fetch_failed"
    assert excluded["kraken_public_api"]["reason"] == "fetch_failed"

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market(conn, "ETH/POINTS")
        price, source = trading._current_market_price_points(conn, market)
        conn.commit()
    finally:
        conn.close()

    assert price > 0
    assert source == "fused_weighted"
    audit = next(row for row in trading.root_report()["audit_events"] if row["event_type"] == "TRADING_PRICE_FUSION_DEGRADED")
    metadata = json.loads(audit["metadata_json"] or "{}")
    excluded_sources = {row["source"] for row in metadata.get("excluded_providers") or []}
    assert {"coinbase_exchange", "kraken_public_api"} <= excluded_sources


def test_live_price_fusion_manual_weights_renormalize_after_provider_failure(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "fused_weighted",
            "price_fusion_mode": "manual_weights",
            "price_fusion_manual_weights": {
                "binance_public_api": 1,
                "okx_public_api": 3,
                "coinbase_exchange": 0,
                "kraken_public_api": 0,
                "gemini_public_api": 0,
                "bitstamp_public_api": 0,
            },
        },
        markets=[],
    )

    def boom(_market_symbol):
        raise OSError("provider unavailable")

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "binance_public_api", 1, price=123.0))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", boom)

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market(conn, "ETH/POINTS")
        price, source = trading._current_market_price_points(conn, market)
    finally:
        conn.close()

    assert price == pytest.approx(123.0615, abs=0.0001)
    assert source == "fused_weighted"
    settings = trading.get_root_settings()["settings"]
    assert settings["price_fusion_mode"] == "manual_weights"
    assert settings["price_fusion_manual_weights"]["okx_public_api"] == 3


def test_live_quote_keeps_risk_grade_usable_when_only_reference_coverage_is_partial(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "fused_weighted",
            "price_fusion_mode": "auto_depth",
            "price_fusion_depth_band_percent": 1.0,
            "price_fusion_min_orderbook_coverage_percent": 0.5,
            "price_fusion_min_provider_count": 2,
            "price_stream_ws_enabled": False,
        },
        markets=[],
    )

    def partial(source, *, min_bid, max_ask):
        return trading._build_orderbook_snapshot(
            source=source,
            bids=[[100.0, 10.0], [min_bid, 8.0]],
            asks=[[100.1, 10.0], [max_ask, 8.0]],
            fetch_meta={"fetched_at": trading_engine_module._now(), "latency_ms": 100.0},
            max_levels=100,
            band_percent=1.0,
        )

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: partial("binance_public_api", min_bid=99.8, max_ask=100.3))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: partial("okx_public_api", min_bid=99.0, max_ask=101.1))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", lambda _symbol: partial("coinbase_exchange", min_bid=99.0, max_ask=101.1))
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", lambda _symbol: partial("kraken_public_api", min_bid=99.0, max_ask=101.1))
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", lambda _symbol: partial("gemini_public_api", min_bid=99.0, max_ask=101.1))
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", lambda _symbol: partial("bitstamp_public_api", min_bid=99.0, max_ask=101.1))
    _stamp_all_markets_boot_ready(trading)

    payload = trading.get_live_market_quote(market_symbol="BTC/USDT")

    assert payload["degraded"] is False
    assert payload["risk_grade_usable"] is True
    assert payload["reference_price_context"]["warning_only"] is True
    assert payload["reference_price_context"]["degraded"] is False
    assert payload["risk_grade_price_context"]["warning_only"] is True
    assert payload["risk_grade_price_context"]["degraded"] is False
    assert payload["risk_grade_price_context"]["risk_grade_usable"] is True
    assert any(item["code"] == "provider_coverage_partial" for item in payload["warnings"])


def test_price_fusion_manual_weights_default_bias_and_zero_weight_exclusion(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(actor=root, settings={"price_source": "fused_weighted", "price_fusion_mode": "manual_weights"}, markets=[])

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "binance_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "okx_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "coinbase_exchange", 1))
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "kraken_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "gemini_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "bitstamp_public_api", 1))

    equal_status = trading.get_root_price_fusion_status(market_symbol="BTC/POINTS")
    used = {row["source"]: row for row in equal_status["providers_used"]}
    assert equal_status["requested_mode"] == "manual_weights"
    assert equal_status["resolved_mode"] == "manual_weights"
    assert used["binance_public_api"]["reference_weight_percent"] == pytest.approx(40.0, abs=0.05)
    assert used["okx_public_api"]["reference_weight_percent"] == pytest.approx(25.0, abs=0.05)
    assert used["coinbase_exchange"]["reference_weight_percent"] == pytest.approx(15.0, abs=0.05)
    assert used["kraken_public_api"]["reference_weight_percent"] == pytest.approx(10.0, abs=0.05)
    assert used["bitstamp_public_api"]["reference_weight_percent"] == pytest.approx(8.0, abs=0.05)
    assert used["gemini_public_api"]["reference_weight_percent"] == pytest.approx(2.0, abs=0.05)

    trading.update_root_settings(
        actor=root,
        settings={
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
        markets=[],
    )
    partial_status = trading.get_root_price_fusion_status(market_symbol="BTC/POINTS")
    used_sources = {row["source"] for row in partial_status["providers_used"]}
    excluded_sources = {row["source"]: row for row in partial_status["excluded_providers"]}
    assert used_sources == {"binance_public_api", "okx_public_api"}
    assert excluded_sources["coinbase_exchange"]["reason"] == "manual_weight_zero"
    assert excluded_sources["kraken_public_api"]["reason"] == "manual_weight_zero"


def test_price_fusion_manual_all_zero_reports_invalid_and_logs_auto_depth_fallback(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "fused_weighted",
            "price_fusion_mode": "manual_weights",
            "price_fusion_manual_weights": {provider: 0 for provider in trading_engine_module.WEIGHTED_PRICE_PROVIDERS},
        },
        markets=[],
    )

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "binance_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "okx_public_api", 2))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "coinbase_exchange", 3))
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "kraken_public_api", 4))
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "gemini_public_api", 5))
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "bitstamp_public_api", 6))

    status = trading.get_root_price_fusion_status(market_symbol="BTC/POINTS")
    assert status["resolved_mode"] == "auto_depth_fallback"
    assert status["warning_code"] == "manual_weights_invalid"
    assert {item["code"] for item in status["warnings"]} >= {"manual_weights_invalid", "manual_weights_unusable"}
    assert "手動權重全部為 0" in status["message"]

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market(conn, "BTC/POINTS")
        _price, source = trading._current_market_price_points(conn, market)
        conn.commit()
    finally:
        conn.close()

    assert source == "fused_weighted"
    audit = next(row for row in trading.root_report()["audit_events"] if row["event_type"] == "TRADING_PRICE_FUSION_DEGRADED")
    metadata = json.loads(audit["metadata_json"] or "{}")
    assert metadata["warning_code"] == "manual_weights_invalid"
    assert {item["code"] for item in metadata["warnings"]} >= {"manual_weights_invalid", "manual_weights_unusable"}
    assert metadata["resolved_mode"] == "auto_depth_fallback"


def test_current_market_price_uses_risk_grade_value_for_high_risk_paths(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(actor=root, settings={"price_source": "fused_weighted", "price_fusion_mode": "auto_depth"}, markets=[])

    def fake_fusion(_symbol, *, settings):
        return 101.0, {
            "resolved_source": "fused_weighted",
            "reference_price_points": 101.0,
            "risk_grade_price_points": 99.0,
            "reference_provider_count": 6,
            "risk_grade_provider_count": 3,
            "providers_used": [],
            "excluded_providers": [],
            "warnings": [],
            "degraded": False,
            "conservative_mode": False,
            "high_risk_blocked": False,
        }

    monkeypatch.setattr(trading, "_fetch_weighted_fused_price_points", fake_fusion)

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market(conn, "BTC/POINTS")
        reference_price, _source, reference_meta = trading._current_market_price_points(conn, market, with_meta=True)
        risk_price, _source, risk_meta = trading._current_market_price_points(conn, market, with_meta=True, high_risk=True)
    finally:
        conn.close()

    assert reference_price == pytest.approx(101.0, abs=0.0001)
    assert risk_price == pytest.approx(99.0, abs=0.0001)
    assert reference_meta["requested_price_mode"] == "reference"
    assert risk_meta["requested_price_mode"] == "risk_grade"
    assert risk_meta["risk_grade_price_points"] == pytest.approx(99.0, abs=0.0001)


def test_test_live_price_provider_is_marked_synthetic_and_not_risk_grade_usable(tmp_path):
    _points, trading = _services(tmp_path)

    quote = trading.get_live_market_quote(market_symbol="BTC/POINTS")

    assert quote["source"] == "test_live_price_provider"
    assert quote["confidence"] == "low"
    assert quote["risk_grade_usable"] is False
    assert quote["risk_grade_price_context"]["risk_grade_usable"] is False
    assert quote["risk_grade_price_context"]["synthetic_test_provider"] is True
    assert "測試注入 live price provider" in quote["risk_grade_price_context"]["warning_message"]


def test_recent_price_window_uses_synthetic_provider_without_public_candles(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    _set_trading_setting(trading, "trading.price_source", "test_live_price_provider")
    _set_trading_setting(trading, "trading.qa_live_price_provider_enabled", "true")
    monkeypatch.setenv("HACKME_DEV_TRADING_ALLOW_QA_LIVE_PRICE_PROVIDER", "1")

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("synthetic provider price window must not fetch public candles")

    monkeypatch.setattr(trading_engine_module, "urlopen", fail_urlopen)
    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        conn.execute(
            """
            UPDATE trading_markets
            SET manual_price_points=123.45,
                price_source='test_live_price_provider',
                live_price_confirmed_at='2024-01-01T00:00:00',
                updated_at='2024-01-01T00:00:00'
            WHERE symbol='ETH/POINTS'
            """
        )
        conn.commit()
        window = trading._recent_price_window("ETH/POINTS", lookback_seconds=60, interval="1m", conn=conn)
    finally:
        conn.close()

    assert window["source"] == "test_live_price_provider"
    assert window["low_points"] == pytest.approx(123.45)
    assert window["high_points"] == pytest.approx(123.45)


def test_root_settings_reject_test_live_price_provider_as_price_source(tmp_path):
    _points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")

    with pytest.raises(ValueError, match="price_source must be fused_weighted, binance_public_api, or manual_root"):
        trading.update_root_settings(actor=root, settings={"price_source": "test_live_price_provider"}, markets=[])


def test_manual_root_price_is_not_risk_grade_usable(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="seed")
    trading.update_root_settings(actor=root, settings={"price_source": "manual_root"}, markets=[])

    quote = trading.get_live_market_quote(market_symbol="ETH/POINTS")

    assert quote["source"] == "manual_root"
    assert quote["risk_grade_usable"] is False
    assert quote["risk_grade_price_context"]["risk_grade_usable"] is False
    assert quote["risk_grade_price_context"]["warning_message"] == "目前使用手動價格"


def test_cached_live_price_fallback_is_not_risk_grade_usable(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="seed")
    trading.update_root_settings(
        actor=root,
        settings={"price_source": "fused_weighted", "max_price_staleness_seconds": 300},
        markets=[],
    )

    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_markets SET manual_price_points=?, price_source=?, updated_at=? WHERE symbol=?",
            (5000.0, "binance_public_api", datetime.now().isoformat(), "ETH/POINTS"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(trading, "_fetch_weighted_fused_price_points", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("all providers down")))

    conn = trading.get_db()
    try:
        market = trading._market(conn, "ETH/POINTS")
        price, source, meta = trading._current_market_price_points(conn, market, with_meta=True, high_risk=True)
    finally:
        conn.close()

    assert price == pytest.approx(5000.0, abs=0.0001)
    assert source == "binance_public_api_cached"
    assert meta["high_risk_blocked"] is True
    assert meta["risk_grade_usable"] is False
    assert meta["risk_grade_price_points"] is None

    result = trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.01")
    assert result["order"]["status"] == "filled"


def test_direct_provider_fallback_is_not_risk_grade_usable(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="seed")
    trading.update_root_settings(actor=root, settings={"price_source": "binance_public_api"}, markets=[])

    fallback_meta = {
        "transport": "http_polling",
        "connected": False,
        "fallback": True,
        "stale": True,
        "degraded": True,
        "confidence": "low",
        "provider_count": 1,
        "last_update_at": "2026-05-05T00:00:00",
        "exclusion_reason": "websocket_disconnected",
        "latency_ms": 88.0,
    }

    def fake_live_fetch(_symbol, *, with_meta=False, settings=None, conn=None):
        if with_meta:
            return 5000.0, "binance_public_api", dict(fallback_meta)
        return 5000.0, "binance_public_api"

    def fake_binance_fetch(_symbol, *, settings=None, with_meta=False, conn=None):
        if with_meta:
            return 5000.0, dict(fallback_meta)
        return 5000.0

    monkeypatch.setattr(trading, "_fetch_live_price_points", fake_live_fetch)
    monkeypatch.setattr(trading, "_fetch_binance_price_points", fake_binance_fetch)

    conn = trading.get_db()
    try:
        market = trading._market(conn, "ETH/POINTS")
        _price, _source, meta = trading._current_market_price_points(conn, market, with_meta=True, high_risk=True)
    finally:
        conn.close()

    assert meta["high_risk_blocked"] is True
    assert meta["risk_grade_usable"] is False
    assert meta["risk_grade_price_points"] is None

    result = trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.01")
    assert result["order"]["status"] == "filled"


def test_price_fusion_status_applies_single_provider_weight_cap(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={
            "price_source": "fused_weighted",
            "price_fusion_mode": "auto_depth",
            "price_fusion_max_single_provider_weight_percent": 35,
        },
        markets=[],
    )

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "binance_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "okx_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "coinbase_exchange", 1))
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "kraken_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "gemini_public_api", 1))
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", lambda _symbol: _depth_snapshot(trading, "bitstamp_public_api", 20))

    status = trading.get_root_price_fusion_status(market_symbol="BTC/POINTS")
    used = {row["source"]: row for row in status["providers_used"]}

    assert status["warning_code"] == "provider_weight_cap_applied"
    assert any(item["code"] == "provider_weight_cap_applied" for item in status["warnings"])
    assert used["bitstamp_public_api"]["weight_cap_applied"] is True
    assert used["bitstamp_public_api"]["normalized_weight_percent"] == pytest.approx(35.0, abs=0.05)
    assert used["bitstamp_public_api"]["raw_normalized_weight_percent"] > used["bitstamp_public_api"]["normalized_weight_percent"]


def test_price_fusion_status_excludes_midpoint_outlier_and_one_sided_depth(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(actor=root, settings={"price_source": "fused_weighted", "price_fusion_mode": "auto_depth"}, markets=[])

    balanced = lambda source, price: _depth_snapshot(trading, source, 2, price=price)
    one_sided = trading._build_orderbook_snapshot(
        source="coinbase_exchange",
        bids=[[100.0, 10.0] for _ in range(10)],
        asks=[[100.1, 0.01] for _ in range(10)],
        fetch_meta={"fetched_at": trading_engine_module._now(), "latency_ms": 90.0},
        max_levels=100,
    )
    outlier = _depth_snapshot(trading, "kraken_public_api", 2, price=110.0)

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", lambda _symbol: balanced("binance_public_api", 100.0))
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", lambda _symbol: balanced("okx_public_api", 100.0))
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", lambda _symbol: one_sided)
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", lambda _symbol: outlier)
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", lambda _symbol: balanced("gemini_public_api", 100.0))
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", lambda _symbol: balanced("bitstamp_public_api", 100.0))

    status = trading.get_root_price_fusion_status(market_symbol="BTC/POINTS")
    excluded = {row["source"]: row for row in status["excluded_providers"]}
    used_sources = {row["source"] for row in status["providers_used"]}

    assert excluded["coinbase_exchange"]["reason"] == "one_sided_depth"
    assert excluded["kraken_public_api"]["reason"] == "midpoint_deviation_exceeded"
    assert "coinbase_exchange" not in used_sources
    assert "kraken_public_api" not in used_sources


def test_price_fusion_orderbook_total_failure_enters_conservative_single_source_fallback(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(actor=root, settings={"price_source": "fused_weighted", "price_fusion_mode": "auto_depth"}, markets=[])

    def boom(_symbol):
        raise ValueError("malformed response")

    monkeypatch.setattr(trading, "_fetch_binance_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_okx_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_coinbase_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_kraken_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_gemini_orderbook_snapshot", boom)
    monkeypatch.setattr(trading, "_fetch_bitstamp_orderbook_snapshot", boom)
    monkeypatch.setattr(
        trading,
        "_fetch_live_price_points",
        lambda _symbol, *, with_meta=False, settings=None: (
            321,
            "coingecko_simple_price",
            {
                "transport": "http_polling",
                "connected": False,
                "fallback": False,
                "stale": False,
                "degraded": False,
                "confidence": "medium",
                "provider_count": 1,
                "last_update_at": "2026-05-05T00:00:00",
                "exclusion_reason": "",
                "latency_ms": 0.0,
            },
        )
        if with_meta
        else (321, "coingecko_simple_price"),
    )

    status = trading.get_root_price_fusion_status(market_symbol="ETH/POINTS")
    assert status["state"] == "conservative"
    assert status["fallback_active"] is True
    assert status["conservative_mode"] is True
    assert status["resolved_source"] == "coingecko_simple_price"
    assert "降級" in status["message"]

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market(conn, "ETH/POINTS")
        price, source = trading._current_market_price_points(conn, market)
        conn.commit()
    finally:
        conn.close()

    assert price == 321
    assert source == "fused_weighted"
    audit = next(row for row in trading.root_report()["audit_events"] if row["event_type"] == "TRADING_PRICE_FUSION_DEGRADED")
    metadata = json.loads(audit["metadata_json"] or "{}")
    assert audit["event_type"] == "TRADING_PRICE_FUSION_DEGRADED"
    assert metadata["fallback_active"] is True
    assert metadata["conservative_mode"] is True
    assert metadata["resolved_source"] == "coingecko_simple_price"


def test_cached_fallback_preserves_fractional_subunit_price(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(actor=root, settings={"price_source": "binance_public_api", "max_price_staleness_seconds": 900}, markets=[])

    def boom(_symbol, **_kwargs):
        raise OSError("provider unavailable")

    monkeypatch.setattr(trading, "_fetch_live_price_points", boom)
    monkeypatch.setattr(trading, "_fetch_binance_price_points", boom)
    monkeypatch.setattr(trading, "_fetch_weighted_fused_price_points", boom)

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market_payload(trading._market(conn, "BTC/POINTS"))
        market["manual_price_points"] = "0.12345678"
        market["price_source"] = "binance_public_api"
        price, source = trading._current_market_price_points(conn, market)
    finally:
        conn.close()

    assert price == pytest.approx(0.12345678)
    assert source == "binance_public_api_cached"


def test_cached_fallback_preserves_decimal_part_for_whole_point_price(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(actor=root, settings={"price_source": "binance_public_api", "max_price_staleness_seconds": 900}, markets=[])

    def boom(_symbol, **_kwargs):
        raise OSError("provider unavailable")

    monkeypatch.setattr(trading, "_fetch_live_price_points", boom)
    monkeypatch.setattr(trading, "_fetch_binance_price_points", boom)
    monkeypatch.setattr(trading, "_fetch_weighted_fused_price_points", boom)

    conn = trading.get_db()
    try:
        trading.ensure_schema(conn)
        market = trading._market_payload(trading._market(conn, "BTC/POINTS"))
        market["manual_price_points"] = "123.99"
        market["price_source"] = "binance_public_api"
        price, source = trading._current_market_price_points(conn, market)
    finally:
        conn.close()

    assert price == pytest.approx(123.99)
    assert price != 123
    assert source == "binance_public_api_cached"


def test_live_market_quote_with_fractional_cached_fallback_is_json_serializable(tmp_path, monkeypatch):
    get_db = _db(tmp_path)
    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "points_chain_backups")
    trading = TradingEngineService(get_db=get_db, points_service=points)

    monkeypatch.setattr(
        trading,
        "_current_market_price_points",
        lambda conn, market, with_meta=False: (
            (0.12345678, "binance_public_api_cached", {"price_health": "fallback", "fallback_reason": "provider unavailable", "excluded_sources": []})
            if with_meta else
            (0.12345678, "binance_public_api_cached")
        ),
    )

    quote = trading.get_live_market_quote(market_symbol="BTC/POINTS")

    assert quote["market"]["manual_price_points"] == pytest.approx(0.12345678)
    assert quote["price_health"] == "fallback"
    assert json.dumps(quote)


def test_live_price_provider_preserves_fractional_price_points(tmp_path):
    _, trading = _services(tmp_path)
    assert trading._price_points_from_float("123.45678901", source="test_source") == pytest.approx(123.45678901)


def test_update_market_and_limit_order_preserve_decimal_price_points(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    updated = trading.update_market(actor=root, symbol="ETH/POINTS", manual_price_points="5000.125", confirm_jump=True)
    trading.test_prices["ETH/POINTS"] = 5000.125

    assert updated["market"]["manual_price_points"] == pytest.approx(5000.125)

    result = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="limit",
        quantity="0.1",
        limit_price_points="5000.125",
    )

    assert result["executed"] is True
    assert result["order"]["execution_price_points"] == pytest.approx(5000.125)

    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["positions"][0]["avg_cost_points"] == pytest.approx(5000.125)


def test_bot_condition_checks_preserve_decimal_trigger_price(tmp_path):
    _, trading = _services(tmp_path)
    checks = trading._bot_condition_checks(
        {
            "trigger_type": "price_below",
            "trigger_price_points": "5000.55",
            "run_count": 0,
            "max_runs": 1,
        },
        5000.5,
    )

    assert checks[0]["met"] is True
    assert "5000.55" in checks[0]["label"]
    assert "5000.5" in checks[0]["label"]


def test_grid_preview_accepts_decimal_price_bounds(tmp_path):
    _, trading = _services(tmp_path)
    preview = trading.preview_grid_bot(
        actor=_actor(),
        payload={
            "market_symbol": "ETH/POINTS",
            "lower_price_points": "99.5",
            "upper_price_points": "100.5",
            "grid_count": 3,
            "order_amount_points": 1000,
            "spacing_mode": "arithmetic",
            "order_mode": "maker",
        },
    )

    assert preview["levels"][0] == pytest.approx(99.5)
    assert preview["levels"][1] == pytest.approx(100.0)
    assert preview["levels"][2] == pytest.approx(100.5)
    assert Decimal(preview["grid_profit"]["reference_buy_price_points"]) in {Decimal("99.5"), Decimal("100")}
    assert Decimal(preview["grid_profit"]["reference_sell_price_points"]) in {Decimal("100"), Decimal("100.5")}


def test_root_spot_and_contract_use_simulated_points_outside_points_chain(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")

    result = trading.place_order(
        actor=root,
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.1",
    )

    assert result["order"]["status"] == "filled"
    assert result["order"]["funding_mode"] == "root_simulated"
    dashboard = trading.user_dashboard(user_id=3)
    assert dashboard["funding"]["mode"] == "root_simulated"
    assert dashboard["funding"]["available_points"] == 9500
    assert dashboard["funding"]["locked_points"] == 0
    assert dashboard["positions"][0]["quantity"] == "0.1"
    assert trading.root_report()["reserve_pool"]["balance_points"] == trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS
    conn = trading.get_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] == 0
    finally:
        conn.close()
    assert trading.verify_state()["ok"] is True

    trading.update_root_settings(
        actor=root,
        settings={"futures_enabled": True},
        markets=[{"symbol": "ETH/POINTS", "futures_enabled": True}],
    )
    contract = trading.open_root_contract_position(
        actor=root,
        market_symbol="ETH/POINTS",
        side="long",
        quantity="0.01",
        leverage=2,
        margin_points=25,
    )
    assert contract["position"]["status"] == "open"
    assert contract["funding"]["available_points"] == 9475
    conn = trading.get_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] == 0
    finally:
        conn.close()
    closed = trading.close_root_contract_position(actor=root, position_uuid=contract["position"]["position_uuid"])
    assert closed["position"]["status"] == "closed"
    assert closed["credited_points"] == 25

    margin = trading.open_margin_position(
        actor=root,
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=300,
    )
    assert margin["position"]["status"] == "open"
    assert margin["position"]["collateral_trial_points"] == 0
    assert margin["position"]["collateral_chain_points"] == 0
    assert trading.verify_state()["ok"] is True

    with pytest.raises(ValueError, match="only root"):
        trading.open_root_contract_position(
            actor=_actor(1, "alice", "user"),
            market_symbol="ETH/POINTS",
            side="long",
            quantity="0.01",
            leverage=2,
            margin_points=25,
        )

    reset = trading.reset_root_simulated_balance(actor=root)
    assert reset["funding"]["available_points"] == 10000
    assert reset["funding"]["locked_points"] == 0
    assert reset["deleted"]["orders"] >= 1
    assert reset["deleted"]["fills"] >= 1
    assert reset["deleted"]["spot_positions"] >= 1
    assert reset["deleted"]["futures_positions"] >= 1
    assert reset["deleted"]["margin_positions"] >= 1
    dashboard_after_reset = trading.user_dashboard(user_id=3)
    assert dashboard_after_reset["funding"]["mode"] == "root_simulated"
    assert dashboard_after_reset["funding"]["available_points"] == 10000
    assert dashboard_after_reset["funding"]["locked_points"] == 0
    assert dashboard_after_reset["orders"] == []
    assert dashboard_after_reset["fills"] == []
    assert dashboard_after_reset["positions"] == []
    assert dashboard_after_reset["futures_positions"] == []
    conn = trading.get_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM trading_orders WHERE user_id=3").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM trading_fills WHERE user_id=3").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM trading_spot_positions WHERE user_id=3").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM trading_futures_positions WHERE user_id=3").fetchone()[0] == 0
    finally:
        conn.close()


def test_root_contract_open_respects_futures_enabled(tmp_path):
    _points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")

    with pytest.raises(ValueError, match="contract trading is disabled"):
        trading.open_root_contract_position(
            actor=root,
            market_symbol="ETH/POINTS",
            side="long",
            quantity="0.01",
            leverage=2,
            margin_points=25,
        )

    trading.update_root_settings(actor=root, settings={"futures_enabled": True}, markets=[{"symbol": "ETH/POINTS", "futures_enabled": False}])
    with pytest.raises(ValueError, match="contract trading is disabled"):
        trading.open_root_contract_position(
            actor=root,
            market_symbol="ETH/POINTS",
            side="long",
            quantity="0.01",
            leverage=2,
            margin_points=25,
        )


def test_limit_buy_can_be_cancelled_and_unfreezes_points(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")

    order = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="limit",
        quantity="0.1",
        limit_price_points=4000,
    )["order"]
    assert order["status"] == "open"
    assert points.get_wallet(1)["points_frozen"] == 0
    assert order["trial_frozen_points"] == 400

    cancelled = trading.cancel_order(actor=_actor(), order_uuid=order["order_uuid"])
    assert cancelled["status"] == "cancelled"
    wallet = points.get_wallet(1)
    assert wallet["points_balance"] == 2000
    assert wallet["points_frozen"] == 0


def test_limit_order_matcher_executes_when_price_reaches_limit(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")

    order = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="limit",
        quantity="0.1",
        limit_price_points=4000,
    )["order"]
    assert order["status"] == "open"
    assert points.get_wallet(1)["points_frozen"] == 0
    assert order["trial_frozen_points"] == 400

    _set_live_price(trading, symbol="ETH/POINTS", price_points=3900)
    matched = trading.match_open_limit_orders(actor={"username": "system", "role": "system"}, limit=10)

    assert matched["ok"] is True
    assert matched["matched"][0]["order_uuid"] == order["order_uuid"]
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["orders"][0]["status"] == "filled"
    assert dashboard["orders"][0]["execution_price_points"] == 3900
    assert dashboard["positions"][0]["quantity"] == "0.1"
    assert points.get_wallet(1)["points_frozen"] == 0
    assert trading.verify_state()["ok"] is True


def test_spot_cfd_principal_and_payout_settle_through_exchange_reserve_pool(tmp_path):
    points, trading = _services(tmp_path)
    _deplete_trial_credit(trading, user_id=1)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    before_reserve = trading.root_report()["reserve_pool"]["balance_points"]
    buy = trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.1")
    assert buy["order"]["status"] == "filled"
    conn = trading.get_db()
    try:
        buy_fill = conn.execute(
            "SELECT notional_points FROM trading_fills WHERE order_id=?",
            (int(buy["order"]["id"]),),
        ).fetchone()
    finally:
        conn.close()
    buy_notional = int(buy_fill["notional_points"])
    after_buy_reserve = trading.root_report()["reserve_pool"]["balance_points"]
    assert after_buy_reserve == before_reserve + buy_notional

    trading.update_market(
        actor=_actor(3, "root", "super_admin"),
        symbol="ETH/POINTS",
        manual_price_points=6000,
        max_order_points=1000000,
        confirm_jump=True,
    )
    trading.test_prices["ETH/POINTS"] = 6000

    high_price_sell = trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="sell", order_type="market", quantity="0.1")
    assert high_price_sell["order"]["status"] == "filled"
    wallet_credit = next(
        int(row["amount"])
        for row in points.list_ledger(user_id=1, include_user_id=True)
        if row["action_type"] == "trading_spot_sell"
    )
    sell_ledger = next(
        row
        for row in points.list_ledger(user_id=1, include_user_id=True)
        if row["action_type"] == "trading_spot_sell"
    )
    sell_flow = sell_ledger["wallet_flow"]
    expected_hot_wallet = _official_hot_wallet(points, 1)["address"]
    assert sell_flow["source_fund_key"] == "exchange_fund"
    assert sell_flow["source_wallet_address"].startswith("pc0")
    assert sell_flow["destination_wallet_address"] == expected_hot_wallet
    assert sell_flow["settlement_rail"] == "internal_hot_wallet"
    assert sell_flow["chain_required"] is False
    assert sell_flow["network_fee_points"] == 0
    assert sell_flow["service_fee_points"] == sell_ledger["public_metadata"]["fee"]
    explorer_tx = points.explorer_transaction(sell_ledger["ledger_hash"])["transaction"]
    assert explorer_tx["wallet_flow"]["source_wallet_address"]
    assert explorer_tx["finality"]["finality_status"] == "internal_settled"
    assert explorer_tx["finality"]["target_proved_count"] == 0
    assert trading.root_report()["reserve_pool"]["balance_points"] == after_buy_reserve - wallet_credit
    conn = trading.get_db()
    try:
        reserve_events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT event_type, delta_points
                FROM trading_reserve_pool_events
                WHERE event_type IN ('spot_cfd_principal_collected', 'spot_cfd_gross_payout', 'fee_retained')
                ORDER BY id ASC
                """
            ).fetchall()
        ]
    finally:
        conn.close()
    assert reserve_events[0] == {"event_type": "spot_cfd_principal_collected", "delta_points": buy_notional}
    assert any(row["event_type"] == "spot_cfd_gross_payout" and row["delta_points"] < 0 for row in reserve_events)
    assert any(row["event_type"] == "fee_retained" and row["delta_points"] > 0 for row in reserve_events)
    assert trading.verify_state()["ok"] is True


def test_root_reserve_allocation_debits_source_wallet_and_audits(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=2, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    before_exchange = points.economy_stats()["economy_layer"]["funds"]["exchange_fund"]["balance"]

    result = trading.allocate_reserve(
        actor=_actor(3, "root", "super_admin"),
        source_user_id=2,
        amount_points=250,
        reason="ROOT_RESERVE_ALLOCATION",
    )

    assert result["balance_points"] == trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS + 250
    assert points.get_wallet(2)["points_balance"] == 750
    report = trading.root_report()
    assert report["reserve_events"][0]["reason"] == "ROOT_RESERVE_ALLOCATION"
    assert report["audit_events"][0]["event_type"] == "TRADING_RESERVE_ALLOCATED"
    after_exchange = points.economy_stats()["economy_layer"]["funds"]["exchange_fund"]["balance"]
    assert after_exchange == before_exchange + 250

    conn = trading.get_db()
    try:
        events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT transaction_type, amount
                FROM points_economy_events
                WHERE event_type='trading_reserve_pool_flow'
                ORDER BY id ASC
                """
            ).fetchall()
        ]
        double_counted_ledger = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM points_economy_events
            WHERE idempotency_key LIKE 'walletized_ledger:%'
              AND metadata_json LIKE '%trading_reserve_allocation%'
            """
        ).fetchone()
    finally:
        conn.close()
    assert events == [{"transaction_type": "root_reserve_allocation", "amount": 250}]
    assert int(double_counted_ledger["count"] or 0) == 0


def test_official_exchange_fund_transfer_syncs_trading_reserve_pool(tmp_path):
    points, trading = _services(tmp_path)
    exchange_address = points.economy_stats()["economy_layer"]["funds"]["exchange_fund"]["address"]
    before_exchange = points.economy_stats()["economy_layer"]["funds"]["exchange_fund"]["balance"]
    before_reserve = trading.root_report()["reserve_pool"]["balance_points"]

    result, _proposal_uuid = _official_treasury_grant_via_governance(
        points,
        destination_wallet_address=exchange_address,
        amount=250,
        request_uuid="official-exchange-sync-reserve",
        action_type="EXCHANGE_FUND_REPLENISH",
    )
    assert trading.root_report()["reserve_pool"]["balance_points"] == before_reserve + 250

    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_transfer_requests SET created_at='2026-01-01T00:00:00Z' WHERE tx_group_hash=?",
            (result["transaction_hash"],),
        )
        conn.commit()
    finally:
        conn.close()
    proved = points.explorer_transaction(result["transaction_hash"])["transaction"]
    trading.refresh_root_trading_snapshots(source_job_key="unit-test")
    pools = trading.get_root_trading_snapshot(snapshot_key="sitewide_pools")["payload"]["pools"]

    assert proved["status"] == "confirmed"
    assert points.economy_stats()["economy_layer"]["funds"]["exchange_fund"]["balance"] == before_exchange + 250
    assert trading.root_report()["reserve_pool"]["balance_points"] == before_reserve + 250
    assert pools["pointschain_exchange_fund"]["balance_points"] == before_exchange + 250
    assert pools["reserve_pool"]["balance_points"] == pools["pointschain_exchange_fund"]["balance_points"]


def test_price_jump_requires_explicit_confirmation(tmp_path):
    _, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")

    with pytest.raises(ValueError, match="confirmation required"):
        trading.update_market(actor=root, symbol="ETH/POINTS", manual_price_points=20000)

    updated = trading.update_market(actor=root, symbol="ETH/POINTS", manual_price_points=20000, confirm_jump=True)
    assert updated["market"]["manual_price_points"] == 20000
    assert updated["market"]["price_source"] == "manual_root"


def test_root_can_update_trading_billing_settings_and_market_limits(tmp_path):
    _, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")

    updated = trading.update_root_settings(
        actor=root,
        settings={
            "enabled": True,
            "borrowing_enabled": True,
            "borrow_apr_btc_eth_percent": 8.25,
            "borrow_apr_usdt_points_percent": 10.5,
            "borrow_interest_interval_hours": 1,
            "borrow_interest_minimum_hours": 1,
            "margin_max_pool_utilization_percent": 70,
            "exchange_liability_limit_points": 5000,
            "exchange_liability_grace_minutes": 90,
            "profit_settlement_interval_minutes": 30,
            "grid_fee_discount_percent": 25,
            "margin_liquidation_enabled": True,
            "margin_maintenance_percent": 12,
            "futures_enabled": False,
            "pvp_matching_enabled": False,
        },
        markets=[
            {
                "symbol": "ETH/POINTS",
                "enabled": True,
                "fee_rate_percent": 0.45,
                "min_order_points": 10,
                "max_order_points": 500000,
            }
        ],
    )

    assert updated["settings"]["borrowing_enabled"] is True
    assert updated["settings"]["borrow_apr_btc_eth_percent"] == pytest.approx(8.25)
    assert updated["settings"]["borrow_apr_usdt_points_percent"] == pytest.approx(10.5)
    assert updated["settings"]["borrow_interest_percent_daily"] == pytest.approx(10.5 / 365.0)
    assert updated["settings"]["borrow_interest_interval_hours"] == 1
    assert updated["settings"]["borrow_interest_minimum_hours"] == 1
    assert updated["settings"]["margin_max_pool_utilization_percent"] == 70
    assert updated["settings"]["exchange_liability_limit_points"] == 5000
    assert updated["settings"]["exchange_liability_grace_minutes"] == 90
    assert updated["settings"]["profit_settlement_interval_minutes"] == 30
    assert updated["settings"]["grid_fee_discount_percent"] == pytest.approx(25.0)
    assert updated["settings"]["margin_liquidation_enabled"] is True
    assert updated["settings"]["margin_maintenance_percent"] == 12
    market = next(row for row in updated["markets"] if row["symbol"] == "ETH/POINTS")
    assert market["fee_rate_percent"] == 0.45
    assert market["min_order_points"] == 10
    assert market["max_order_points"] == 500000
    report = trading.root_report()
    assert report["settings"]["borrowing_enabled"] is True
    assert report["audit_events"][0]["event_type"] in {"TRADING_MARKET_BILLING_UPDATED", "TRADING_SETTINGS_UPDATED"}


def test_borrowing_trading_is_enabled_by_default(tmp_path):
    _, trading = _services(tmp_path)

    report = trading.get_root_settings()
    settings = report["settings"]
    eth_market = next(row for row in report["markets"] if row["symbol"] == "ETH/POINTS")

    assert settings["borrowing_enabled"] is True
    assert settings["borrow_apr_btc_eth_percent"] == pytest.approx(8.0)
    assert settings["borrow_apr_usdt_points_percent"] == pytest.approx(10.0)
    assert settings["borrow_interest_interval_hours"] == 1
    assert settings["borrow_interest_minimum_hours"] == 1
    assert settings["margin_max_pool_utilization_percent"] == pytest.approx(80.0)
    assert settings["exchange_liability_limit_points"] == 0
    assert settings["exchange_liability_grace_minutes"] == 60
    assert settings["profit_settlement_interval_minutes"] == 0
    assert settings["grid_fee_discount_percent"] == pytest.approx(25.0)
    assert eth_market["fee_rate_percent"] == pytest.approx(0.1)
    assert trading._grid_fee_rate_percent(eth_market["fee_rate_percent"], settings) == pytest.approx(0.075)


def test_margin_positions_use_asset_specific_apr_groups_and_hourly_interest_metadata(tmp_path):
    _, trading = _services(tmp_path)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_apr_btc_eth_percent": 8,
            "borrow_apr_usdt_points_percent": 10,
            "borrow_interest_pool_pressure_multiplier": 0,
            "borrow_interest_interval_hours": 1,
            "borrow_interest_minimum_hours": 1,
        },
        markets=[],
    )

    opened_long = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    long_position = trading.user_dashboard(user_id=1)["margin_positions"][0]

    assert opened_long["position"]["borrowed_asset_symbol"] == "POINTS"
    assert opened_long["position"]["interest_percent_daily"] == pytest.approx(10.0 / 365.0)
    assert long_position["interest_apr_percent"] == pytest.approx(10.0)
    assert long_position["interest_interval_hours"] == 1
    assert long_position["interest_minimum_hours"] == 1
    assert long_position["next_interest_at"]

    trading.close_margin_position(actor=_actor(), position_uuid=opened_long["position"]["position_uuid"])

    opened_short = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="short",
        quantity="0.1",
        collateral_points=300,
    )
    short_position = trading.user_dashboard(user_id=1)["margin_positions"][0]

    assert opened_short["position"]["borrowed_asset_symbol"] == "ETH"
    assert opened_short["position"]["interest_percent_daily"] == pytest.approx(8.0 / 365.0)
    assert short_position["interest_apr_percent"] == pytest.approx(8.0)
    assert short_position["interest_interval_hours"] == 1
    assert short_position["interest_minimum_hours"] == 1


def test_trading_volume_stats_accumulate_spot_and_margin_activity_for_future_vip_logic(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True},
        markets=[],
    )

    spot = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="market",
        quantity="0.1",
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.2",
        collateral_points=500,
    )
    closed = trading.close_margin_position(actor=_actor(), position_uuid=opened["position"]["position_uuid"])

    stats = trading.user_dashboard(user_id=1)["volume_stats"]

    assert stats["spot_notional_points"] == 500
    assert stats["margin_notional_points"] == 2000
    assert stats["total_notional_points"] == 2500
    assert stats["total_trade_count"] == 3
    assert stats["total_fee_points"] == (
        spot["order"]["fee_points"]
        + opened["position"]["open_fee_points"]
        + closed["close_fee_points"]
    )

    root_summary = trading.root_report()["volume_summary"]
    assert root_summary["totals"]["spot_notional_points"] == 500
    assert root_summary["totals"]["margin_notional_points"] == 2000
    assert root_summary["totals"]["total_notional_points"] == 2500
    assert root_summary["totals"]["total_trade_count"] == 3
    assert root_summary["top_users"][0]["username"] == "alice"


def test_margin_profit_allows_collateral_withdrawal_with_lower_maintenance_ratio(tmp_path):
    _, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    trading.update_root_settings(
        actor=root,
        settings={"borrowing_enabled": True, "margin_maintenance_percent": 15},
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=root,
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    _set_live_price(trading, symbol="ETH/POINTS", price_points=7000)
    before = trading.user_dashboard(user_id=3)["margin_positions"][0]["risk"]
    assert before["max_withdrawable_collateral_points"] >= 50
    assert before["withdrawable_collateral_after_ratio_percent"] is not None

    result = trading.withdraw_margin_collateral(
        actor=root,
        position_uuid=opened["position"]["position_uuid"],
        amount_points=50,
        idempotency_key="test-margin-withdraw-profit",
    )

    assert result["ok"] is True
    assert "降低維持率" in result["warning"]
    assert before["unrealized_pnl_points"] > 0
    assert result["risk_after"]["maintenance_ratio_percent"] < before["maintenance_ratio_percent"]
    assert result["position"]["collateral_points"] == opened["position"]["collateral_points"] - 50
    assert result["position"]["principal_points"] == opened["position"]["principal_points"] + 50


def test_margin_long_requires_root_enabled_borrowing_and_closes_with_fee_stats(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")

    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": False},
        markets=[],
    )
    with pytest.raises(ValueError, match="borrow trading is disabled"):
        trading.open_margin_position(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            position_type="margin_long",
            quantity="0.1",
            collateral_points=200,
        )

    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True, "borrow_interest_percent_daily": 10},
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    assert opened["position"]["position_type"] == "margin_long"
    assert opened["position"]["principal_points"] == 300
    assert opened["position"]["collateral_trial_points"] == 200
    assert opened["position"]["open_fee_points"] == 0
    assert opened["position"]["open_fee_micropoints"] == 500_000
    assert opened["position"]["open_fee_trial_points"] == 0
    assert points.get_wallet(1)["points_balance"] == 1000
    assert points.get_wallet(1)["points_frozen"] == 0
    assert opened["funding"]["trial_credit"]["available_points"] == 800
    assert opened["funding"]["trial_credit"]["deployed_points"] == 200
    # Borrow pressure is calculated on the lendable slice of the exchange fund,
    # not on the full fund, so a CFD profit reserve remains outside the loan cap.
    expected_interest_capacity = trading.root_report()["funding_pool"]["max_outstanding_principal_points"]
    expected_interest_percent = 10 * (1 + (opened["position"]["principal_points"] / expected_interest_capacity) * 4.0)
    assert opened["position"]["interest_percent_daily"] == pytest.approx(expected_interest_percent)
    assert (
        trading.root_report()["reserve_pool"]["balance_points"]
        == trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS - 300
    )

    closed = trading.close_margin_position(actor=_actor(), position_uuid=opened["position"]["position_uuid"])
    assert closed["position"]["status"] == "closed"
    assert closed["interest_points"] == 2
    assert closed["close_fee_points"] == 1
    assert closed["position"]["close_fee_micropoints"] == 1_000_000
    assert closed["position"]["interest_paid_points"] == 0
    assert closed["delta_points"] == -3
    assert points.get_wallet(1)["points_frozen"] == 0
    assert closed["funding"]["trial_credit"]["available_points"] == 997
    assert closed["funding"]["trial_credit"]["deployed_points"] == 0
    assert trading.root_report()["reserve_pool"]["balance_points"] == trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS + 3
    assert trading.verify_state()["ok"] is True


def test_margin_cfd_price_loss_is_collected_by_exchange_reserve_pool(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)
    conn = trading.get_db()
    try:
        conn.execute("UPDATE trading_markets SET fee_rate_percent=0 WHERE symbol='ETH/POINTS'")
        conn.commit()
    finally:
        conn.close()
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True, "borrow_interest_percent_daily": 0},
        markets=[],
    )
    reserve_before = trading.root_report()["reserve_pool"]["balance_points"]
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    assert trading.root_report()["reserve_pool"]["balance_points"] == reserve_before - 300

    _set_live_price(trading, symbol="ETH/POINTS", price_points=4500)
    closed = trading.close_margin_position(actor=_actor(), position_uuid=opened["position"]["position_uuid"])

    assert closed["delta_points"] == -50
    assert points.get_wallet(1)["points_balance"] == 1950
    assert trading.root_report()["reserve_pool"]["balance_points"] == reserve_before + 50
    conn = trading.get_db()
    try:
        event = conn.execute(
            "SELECT delta_points FROM trading_reserve_pool_events WHERE event_type='margin_loss_collected' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert event is not None
    assert int(event["delta_points"]) == 50
    assert trading.verify_state()["ok"] is True


def test_margin_principal_is_receivable_not_user_pc0_circulation(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=1000,
        action_type="admin_adjust_credit",
    )
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_interest_percent_daily": 0,
            "margin_long_financing_percent": 80,
        },
        markets=[{"symbol": "ETH/POINTS", "fee_rate_percent": 0}],
    )

    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=100,
    )
    economy = points.economy_stats()["economy_layer"]["supply_equation"]
    breakdown = economy["economy_external_balance_breakdown"]

    assert opened["position"]["principal_points"] == 400
    assert breakdown["exchange_principal_receivable_points"] == 400
    assert breakdown["pc0_adjusted_by_exchange_principal_receivable_points"] == 0
    assert economy["off_wallet_economy_external_points"] == 400
    assert economy["ledger_vs_economy_external_gap_points"] == 0
    assert economy["bridged_supply_equation_balanced"] is True


def test_margin_fee_uses_full_notional_and_settles_on_close(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True, "borrow_interest_percent_daily": 0},
        markets=[],
    )

    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=100,
    )

    assert opened["position"]["principal_points"] == 400
    assert opened["position"]["open_fee_points"] == 0
    assert opened["position"]["open_fee_micropoints"] == 500_000
    assert opened["funding"]["trial_credit"]["available_points"] == 900

    closed = trading.close_margin_position(actor=_actor(), position_uuid=opened["position"]["position_uuid"])

    assert closed["close_fee_points"] == 1
    assert closed["position"]["close_fee_micropoints"] == 1_000_000
    assert closed["delta_points"] == -1


def test_margin_open_and_close_are_included_in_trade_history(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True},
        markets=[],
    )

    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    dashboard_after_open = trading.user_dashboard(user_id=1)

    assert dashboard_after_open["spot_fills"] == []
    assert [row["record_type"] for row in dashboard_after_open["margin_trade_records"]] == ["margin_open"]
    assert dashboard_after_open["fills"][0]["record_type"] == "margin_open"
    assert dashboard_after_open["fills"][0]["side"] == "融資做多開倉"
    assert dashboard_after_open["fills"][0]["position_uuid"] == opened["position"]["position_uuid"]

    closed = trading.close_margin_position(actor=_actor(), position_uuid=opened["position"]["position_uuid"])
    dashboard_after_close = trading.user_dashboard(user_id=1)
    record_types = [row["record_type"] for row in dashboard_after_close["margin_trade_records"]]
    close_record = next(row for row in dashboard_after_close["margin_trade_records"] if row["record_type"] == "margin_close")

    assert record_types == ["margin_close", "margin_open"]
    assert dashboard_after_close["fills"][0]["record_type"] == "margin_close"
    assert close_record["side"] == "融資做多平倉"
    assert close_record["position_uuid"] == opened["position"]["position_uuid"]
    assert close_record["price_points"] == closed["position"]["exit_price_points"]
    assert close_record["realized_pnl_points"] == closed["delta_points"]
    assert close_record["fee_points"] == closed["close_fee_points"]


def test_margin_interest_charges_by_started_hour_not_whole_day(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True, "borrow_interest_percent_daily": 24},
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_margin_positions SET opened_at='2026-05-02T10:00:00' WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        ).fetchone()
        one_hour_interest = trading._margin_interest_points(row, now_text="2026-05-02T10:00:01")
        two_hour_interest = trading._margin_interest_points(row, now_text="2026-05-02T11:00:01")
        day_interest = trading._margin_interest_points(row, now_text="2026-05-03T09:59:59")
        def expected_interest_points(hours):
            micro = trading._margin_interest_due_micropoints(
                principal=opened["position"]["principal_points"],
                rate_percent=opened["position"]["interest_percent_daily"],
                hours=hours,
            )
            return (micro + trading_engine_module.POINT_MICRO_SCALE - 1) // trading_engine_module.POINT_MICRO_SCALE

        assert one_hour_interest == expected_interest_points(1)
        assert trading._margin_interest_points(row, now_text="2026-05-02T10:59:59") == one_hour_interest
        assert trading._margin_interest_points(row, now_text="2026-05-02T11:00:00") == one_hour_interest
        assert two_hour_interest == expected_interest_points(2)
        assert day_interest == expected_interest_points(24)
    finally:
        conn.close()


def test_margin_interest_charges_minimum_point_for_fractional_due(tmp_path):
    _, trading = _services(tmp_path)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_interest_percent_daily": 1,
            "borrow_interest_pool_pressure_multiplier": 0,
        },
        markets=[{"symbol": "ETH/POINTS", "manual_price_points": 5000}],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.02",
        collateral_points=50,
    )

    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_margin_positions SET opened_at='2026-05-02T10:00:00' WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        ).fetchone()
        conn.execute("BEGIN IMMEDIATE")
        accrued = trading._accrue_margin_interest(conn, row, actor={"username": "system"}, now_text="2026-05-03T10:00:00")
        conn.commit()
    finally:
        conn.close()

    assert accrued["interest_points"] == 0
    assert accrued["interest_paid_points"] == 0
    assert accrued["interest_carry_micropoints"] == 500_000

    conn = trading.get_db()
    try:
        row = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        ).fetchone()
        conn.execute("BEGIN IMMEDIATE")
        accrued_again = trading._accrue_margin_interest(conn, row, actor={"username": "system"}, now_text="2026-05-04T10:00:00")
        conn.commit()
    finally:
        conn.close()

    assert accrued_again["interest_points"] == 0
    assert accrued_again["interest_carry_micropoints"] == 1_000_000
    assert trading.verify_state()["ok"] is True


def test_margin_open_rejects_when_funding_pool_is_insufficient(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True},
        markets=[],
    )
    conn = trading.get_db()
    try:
        conn.execute("BEGIN")
        trading._reserve_delta(
            conn,
            delta=-(trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS - 50),
            event_type="test_funding_pool_drain",
            reason="TEST_FUNDING_POOL_DRAIN",
            actor=_actor(3, "root", "super_admin"),
        )
        conn.commit()
    finally:
        conn.close()

    assert trading.user_dashboard(user_id=1)["funding_pool"]["available_points"] == 40
    with pytest.raises(ValueError, match="funding pool is insufficient"):
        trading.open_margin_position(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            position_type="margin_long",
            quantity="0.1",
            collateral_points=200,
        )
    assert trading.verify_state()["ok"] is True


def test_exchange_fund_large_loss_profit_borrow_cap_and_shortfall_preserve_chain(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    alice = _actor()
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=2_500_000,
        action_type="admin_adjust_credit",
    )
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=root,
        settings={
            "borrowing_enabled": True,
            "borrow_interest_percent_daily": 0,
            "margin_long_financing_percent": 90,
            "margin_max_pool_utilization_percent": 80,
            "exchange_liability_limit_points": 25_000,
            "exchange_liability_grace_minutes": 120,
            "profit_settlement_interval_minutes": 30,
            "margin_maintenance_percent": 10,
        },
        markets=[{"symbol": "ETH/POINTS", "fee_rate_percent": 0}],
    )

    initial_reserve = trading.root_report()["reserve_pool"]["balance_points"]
    initial_exchange = points.economy_stats()["economy_layer"]["funds"]["exchange_fund"]["balance"]

    _set_live_price(trading, symbol="ETH/POINTS", price_points=5000)
    loss_open = trading.open_margin_position(
        actor=alice,
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="100",
        collateral_points=100_000,
    )
    assert loss_open["position"]["principal_points"] == 400_000
    _set_live_price(trading, symbol="ETH/POINTS", price_points=4500)
    loss_close = trading.close_margin_position(
        actor=alice,
        position_uuid=loss_open["position"]["position_uuid"],
    )
    assert loss_close["delta_points"] == -50_000
    assert trading.root_report()["reserve_pool"]["balance_points"] == initial_reserve + 50_000
    assert points.economy_stats()["economy_layer"]["funds"]["exchange_fund"]["balance"] == initial_exchange + 50_000

    _set_live_price(trading, symbol="ETH/POINTS", price_points=5000)
    profit_open = trading.open_margin_position(
        actor=alice,
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="100",
        collateral_points=100_000,
    )
    _set_live_price(trading, symbol="ETH/POINTS", price_points=6000)
    profit_close = trading.close_margin_position(
        actor=alice,
        position_uuid=profit_open["position"]["position_uuid"],
    )
    assert profit_close["delta_points"] == 100_000
    assert trading.root_report()["reserve_pool"]["balance_points"] == initial_reserve - 50_000
    assert points.economy_stats()["economy_layer"]["funds"]["exchange_fund"]["balance"] == initial_exchange - 50_000

    _set_live_price(trading, symbol="ETH/POINTS", price_points=5000)
    available_before_cap_check = trading.root_report()["funding_pool"]["available_points"]
    assert available_before_cap_check > 3_900_000
    with pytest.raises(ValueError, match="funding pool utilization limit"):
        trading.open_margin_position(
            actor=alice,
            market_symbol="ETH/POINTS",
            position_type="margin_long",
            quantity="1000",
            collateral_points=800_000,
        )
    assert trading.root_report()["reserve_pool"]["balance_points"] == initial_reserve - 50_000

    _set_live_price(trading, symbol="ETH/POINTS", price_points=5000)
    shortfall_open = trading.open_margin_position(
        actor=alice,
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="100",
        collateral_points=100_000,
    )
    conn = trading.get_db()
    try:
        conn.execute("BEGIN")
        current_reserve = int(trading._reserve(conn)["balance_points"] or 0)
        trading._reserve_delta(
            conn,
            delta=-(current_reserve - 50_000),
            event_type="test_catastrophic_exchange_fund_drain",
            reason="TEST_CATASTROPHIC_EXCHANGE_FUND_DRAIN",
            actor=root,
        )
        conn.commit()
    finally:
        conn.close()
    assert trading.root_report()["reserve_pool"]["balance_points"] == 50_000

    wallet_before_failed_close = points.get_wallet(1)
    _set_live_price(trading, symbol="ETH/POINTS", price_points=10000)
    shortfall_close = trading.close_margin_position(
        actor=alice,
        position_uuid=shortfall_open["position"]["position_uuid"],
    )
    wallet_after_failed_close = points.get_wallet(1)
    dashboard_after_shortfall = trading.user_dashboard(user_id=1)
    closed_position = dashboard_after_shortfall["margin_positions"][0]
    close_record = next(
        row
        for row in dashboard_after_shortfall["margin_trade_records"]
        if row["record_type"] == "margin_close"
        and row["position_uuid"] == shortfall_open["position"]["position_uuid"]
    )
    conn = trading.get_db()
    try:
        pending = conn.execute(
            "SELECT * FROM trading_pending_profit WHERE position_uuid=?",
            (shortfall_open["position"]["position_uuid"],),
        ).fetchone()
        proposal = conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (pending["governance_proposal_uuid"],),
        ).fetchone()
        proposal_payload = json.loads(proposal["payload_json"])
    finally:
        conn.close()
    chain = points.verify_chain()
    economy = points.economy_stats(verification=chain)["economy_layer"]

    assert shortfall_close["delta_points"] == 500_000
    assert shortfall_close["paid_profit_points"] == 450_000
    assert shortfall_close["pending_profit_points"] == 50_000
    assert shortfall_close["governance_proposal_uuid"] == pending["governance_proposal_uuid"]
    assert closed_position["position_uuid"] == shortfall_open["position"]["position_uuid"]
    assert closed_position["status"] == "closed"
    assert wallet_after_failed_close["points_balance"] == wallet_before_failed_close["points_balance"] + 550_000
    assert wallet_after_failed_close["points_frozen"] == wallet_before_failed_close["points_frozen"] - 100_000
    assert close_record["paid_profit_points"] == 450_000
    assert close_record["pending_profit_points"] == 50_000
    assert close_record["governance_proposal_uuid"] == pending["governance_proposal_uuid"]
    assert pending["status"] == "pending"
    assert int(pending["amount_points"]) == 50_000
    assert proposal["action_type"] == "PARAMETER_CHANGE"
    assert proposal_payload["proposal_type"] == "TRADING_EXCHANGE_SHORTFALL_RESOLUTION"
    assert proposal_payload["current_policy"] == {
        "exchange_liability_limit_points": 25_000,
        "exchange_liability_grace_minutes": 120,
        "profit_settlement_interval_minutes": 30,
    }
    assert {option["key"] for option in proposal_payload["options"]} == {
        "official_treasury_replenishment",
        "forced_borrower_repayment",
        "accept_temporary_liability",
    }
    temporary_option = next(option for option in proposal_payload["options"] if option["key"] == "accept_temporary_liability")
    assert temporary_option["suggested_liability_limit_points"] == 50_000
    assert temporary_option["suggested_grace_minutes"] == 120
    assert temporary_option["suggested_settlement_interval_minutes"] == 60
    assert trading.root_report()["reserve_pool"]["balance_points"] == 0
    assert economy["funds"]["exchange_fund"]["balance"] == 0
    assert economy["supply_equation"]["actual_supply_equation_gap_points"] == 0
    assert chain["ok"] is True
    assert trading.verify_state()["ok"] is True


def test_short_borrow_profit_after_exchange_fund_drain_keeps_fund_nonnegative(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    alice = _actor()
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=1_000_000,
        action_type="admin_adjust_credit",
    )
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=root,
        settings={
            "borrowing_enabled": True,
            "borrow_apr_btc_eth_percent": 0,
            "borrow_apr_usdt_points_percent": 0,
            "short_collateral_percent": 60,
            "margin_max_pool_utilization_percent": 80,
        },
        markets=[{"symbol": "ETH/POINTS", "fee_rate_percent": 0}],
    )

    _set_live_price(trading, symbol="ETH/POINTS", price_points=5000)
    opened = trading.open_margin_position(
        actor=alice,
        market_symbol="ETH/POINTS",
        position_type="short",
        quantity="100",
        collateral_points=300_000,
    )
    assert opened["position"]["principal_points"] == 500_000

    conn = trading.get_db()
    try:
        conn.execute("BEGIN")
        current_reserve = int(trading._reserve(conn)["balance_points"] or 0)
        trading._reserve_delta(
            conn,
            delta=-current_reserve,
            event_type="test_short_profit_exchange_fund_drain",
            reason="TEST_SHORT_PROFIT_EXCHANGE_FUND_DRAIN",
            actor=root,
        )
        conn.commit()
    finally:
        conn.close()
    assert trading.root_report()["reserve_pool"]["balance_points"] == 0

    wallet_before = points.get_wallet(1)
    _set_live_price(trading, symbol="ETH/POINTS", price_points=1000)
    closed = trading.close_margin_position(
        actor=alice,
        position_uuid=opened["position"]["position_uuid"],
    )
    wallet_after = points.get_wallet(1)
    conn = trading.get_db()
    try:
        pending_count = conn.execute("SELECT COUNT(*) FROM trading_pending_profit").fetchone()[0]
    finally:
        conn.close()
    chain = points.verify_chain()
    economy = points.economy_stats(verification=chain)["economy_layer"]

    assert closed["delta_points"] == 400_000
    assert closed["paid_profit_points"] == 400_000
    assert closed["pending_profit_points"] == 0
    assert wallet_after["points_balance"] == wallet_before["points_balance"] + 700_000
    assert wallet_after["points_frozen"] == wallet_before["points_frozen"] - 300_000
    assert pending_count == 0
    assert trading.root_report()["reserve_pool"]["balance_points"] == 100_000
    assert economy["funds"]["exchange_fund"]["balance"] == 100_000
    assert economy["supply_equation"]["actual_supply_equation_gap_points"] == 0
    assert chain["ok"] is True
    assert trading.verify_state()["ok"] is True


def test_exchange_shortfall_multiple_positions_stress_keeps_chain_closed_loop(tmp_path):
    points, trading = _services(tmp_path)
    root = _actor(3, "root", "super_admin")
    alice = _actor()
    points.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=1_500_000,
        action_type="admin_adjust_credit",
    )
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=root,
        settings={
            "borrowing_enabled": True,
            "borrow_interest_percent_daily": 0,
            "margin_long_financing_percent": 80,
            "margin_max_pool_utilization_percent": 80,
            "exchange_liability_limit_points": 25_000,
            "exchange_liability_grace_minutes": 120,
            "profit_settlement_interval_minutes": 30,
        },
        markets=[{"symbol": "ETH/POINTS", "fee_rate_percent": 0}],
    )

    _set_live_price(trading, symbol="ETH/POINTS", price_points=5000)
    opened_positions = [
        trading.open_margin_position(
            actor=alice,
            market_symbol="ETH/POINTS",
            position_type="margin_long",
            quantity="20",
            collateral_points=20_000,
        )["position"]
        for _ in range(3)
    ]
    assert [row["principal_points"] for row in opened_positions] == [80_000, 80_000, 80_000]

    conn = trading.get_db()
    try:
        conn.execute("BEGIN")
        current_reserve = int(trading._reserve(conn)["balance_points"] or 0)
        trading._reserve_delta(
            conn,
            delta=-current_reserve,
            event_type="test_multi_position_catastrophic_exchange_fund_drain",
            reason="TEST_MULTI_POSITION_CATASTROPHIC_EXCHANGE_FUND_DRAIN",
            actor=root,
        )
        conn.commit()
    finally:
        conn.close()
    assert trading.root_report()["reserve_pool"]["balance_points"] == 0

    _set_live_price(trading, symbol="ETH/POINTS", price_points=10000)
    closed = [
        trading.close_margin_position(actor=alice, position_uuid=position["position_uuid"])
        for position in opened_positions
    ]

    conn = trading.get_db()
    try:
        pending_rows = conn.execute(
            "SELECT * FROM trading_pending_profit ORDER BY id ASC"
        ).fetchall()
        proposal_payloads = []
        for row in pending_rows:
            proposal = conn.execute(
                "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
                (row["governance_proposal_uuid"],),
            ).fetchone()
            proposal_payloads.append(json.loads(proposal["payload_json"]))
    finally:
        conn.close()
    chain = points.verify_chain()
    economy = points.economy_stats(verification=chain)["economy_layer"]

    assert [row["delta_points"] for row in closed] == [100_000, 100_000, 100_000]
    assert [row["paid_profit_points"] for row in closed] == [80_000, 80_000, 80_000]
    assert [row["pending_profit_points"] for row in closed] == [20_000, 20_000, 20_000]
    assert len(pending_rows) == 3
    assert sum(int(row["amount_points"]) for row in pending_rows) == 60_000
    assert all(row["status"] == "pending" for row in pending_rows)
    assert {payload["proposal_type"] for payload in proposal_payloads} == {"TRADING_EXCHANGE_SHORTFALL_RESOLUTION"}
    assert proposal_payloads[-1]["liability_after_points"] == 60_000
    assert proposal_payloads[-1]["current_policy"] == {
        "exchange_liability_limit_points": 25_000,
        "exchange_liability_grace_minutes": 120,
        "profit_settlement_interval_minutes": 30,
    }
    assert trading.root_report()["reserve_pool"]["balance_points"] == 0
    assert economy["funds"]["exchange_fund"]["balance"] == 0
    assert economy["supply_equation"]["actual_supply_equation_gap_points"] == 0
    assert chain["ok"] is True
    assert trading.verify_state()["ok"] is True


def test_exchange_shortfall_policy_rejects_user_privilege_escalation(tmp_path):
    points, trading = _services(tmp_path)
    alice = _actor()

    with pytest.raises(PermissionError, match="root required"):
        trading.update_root_settings(
            actor=alice,
            settings={
                "exchange_liability_limit_points": 100_000_000,
                "profit_settlement_interval_minutes": 30 * 24 * 60,
            },
            markets=[],
        )
    with pytest.raises(PermissionError, match="root required"):
        trading.update_market(
            actor=alice,
            symbol="ETH/POINTS",
            fee_rate_percent=0,
        )
    with pytest.raises(PermissionError, match=r"manager\+ required"):
        points.create_policy_governance_proposal(
            actor=alice,
            action_type="PARAMETER_CHANGE",
            title="Try to approve exchange fund debt",
            reason="normal user cannot alter protocol-level exchange fund liability policy",
            payload={
                "proposal_type": "TRADING_EXCHANGE_SHORTFALL_RESOLUTION",
                "exchange_liability_limit_points": 100_000_000,
            },
            reference="user-shortfall-escalation-attempt",
        )

    settings = trading.get_root_settings()["settings"]
    assert settings["exchange_liability_limit_points"] == 0
    assert settings["profit_settlement_interval_minutes"] == 0


def test_margin_open_does_not_require_unsettled_fee_upfront(tmp_path):
    points, trading = _services(tmp_path)
    trading.user_dashboard(user_id=1)
    _deplete_trial_credit(trading)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1100, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "margin_long_financing_percent": 90,
            "margin_maintenance_percent": 15,
        },
        markets=[],
    )

    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.4824",
        collateral_points=1100,
    )

    wallet = points.get_wallet(1)
    assert wallet["points_balance"] == 0
    assert wallet["points_frozen"] == 1100
    assert opened["position"]["open_fee_points"] == 0
    assert opened["position"]["open_fee_micropoints"] == 2_412_000
    assert trading.user_dashboard(user_id=1)["margin_positions"][0]["position_uuid"] == opened["position"]["position_uuid"]
    assert trading.verify_state()["ok"] is True


def test_margin_open_requires_buffer_so_liquidation_price_starts_beyond_entry(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "margin_long_financing_percent": 90,
            "short_collateral_percent": 10,
            "margin_maintenance_percent": 15,
        },
        markets=[],
    )

    with pytest.raises(ValueError, match="collateral below minimum 78"):
        trading.open_margin_position(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            position_type="margin_long",
            quantity="0.1",
            collateral_points=77,
        )
    with pytest.raises(ValueError, match="collateral must be lower than notional 500"):
        trading.open_margin_position(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            position_type="margin_long",
            quantity="0.1",
            collateral_points=500,
        )
    long_position = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=78,
    )
    long_risk = trading.user_dashboard(user_id=1)["margin_positions"][0]["risk"]
    assert long_risk["liquidation_price_points"] < long_position["position"]["entry_price_points"]
    trading.close_margin_position(actor=_actor(), position_uuid=long_position["position"]["position_uuid"])

    with pytest.raises(ValueError, match="collateral below minimum 78"):
        trading.open_margin_position(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            position_type="short",
            quantity="0.1",
            collateral_points=77,
        )
    short_position = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="short",
        quantity="0.1",
        collateral_points=78,
    )
    short_risk = trading.user_dashboard(user_id=1)["margin_positions"][0]["risk"]
    assert short_risk["liquidation_price_points"] > short_position["position"]["entry_price_points"]
    assert trading.verify_state()["ok"] is True


def test_margin_risk_includes_break_even_and_interest_raises_thresholds(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_apr_usdt_points_percent": 100,
            "borrow_interest_pool_pressure_multiplier": 0,
            "borrow_interest_interval_hours": 1,
            "borrow_interest_minimum_hours": 1,
        },
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.5",
        collateral_points=1000,
    )

    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_margin_positions SET opened_at='2026-05-04T10:00:00' WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        ).fetchone()
        market = trading._market(conn, "ETH/POINTS")
        before = trading._margin_risk_payload(conn, row, market=market, now_text="2026-05-04T10:30:00")
        after = trading._margin_risk_payload(conn, row, market=market, now_text="2026-05-08T10:00:00")
    finally:
        conn.close()

    assert before["breakeven_price_points"] > opened["position"]["entry_price_points"]
    assert after["breakeven_price_points"] > before["breakeven_price_points"]
    assert after["liquidation_price_points"] > before["liquidation_price_points"]


def test_margin_open_is_idempotent_for_client_key(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")

    first = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
        idempotency_key="open-margin-click-1",
    )
    second = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
        idempotency_key="open-margin-click-1",
    )

    assert second["position"]["position_uuid"] == first["position"]["position_uuid"]
    dashboard = trading.user_dashboard(user_id=1)
    assert len([row for row in dashboard["margin_positions"] if row["status"] == "open"]) == 1
    assert dashboard["margin_positions"][0]["interest_paid_points"] == 0
    assert dashboard["margin_positions"][0]["interest_carry_micropoints"] >= 0
    assert trading.root_report()["reserve_pool"]["balance_points"] == trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS - 300
    assert trading.verify_state()["ok"] is True


def test_hourly_margin_interest_capitalizes_when_wallet_balance_is_insufficient(tmp_path):
    points, trading = _services(tmp_path)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True, "borrow_interest_percent_daily": 24},
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    assert points.get_wallet(1)["points_balance"] == 0

    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_margin_positions SET opened_at='2026-05-02T10:00:00' WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        ).fetchone()
        conn.execute("BEGIN IMMEDIATE")
        accrued = trading._accrue_margin_interest(conn, row, actor={"username": "system"}, now_text="2026-05-02T11:00:01")
        conn.commit()
    finally:
        conn.close()

    assert accrued["interest_accrued_hours"] == 2
    assert accrued["interest_paid_points"] == 0
    assert accrued["interest_points"] == 0
    expected_carry = trading._margin_interest_due_micropoints(
        principal=opened["position"]["principal_points"],
        rate_percent=opened["position"]["interest_percent_daily"],
        hours=2,
    )
    assert accrued["interest_carry_micropoints"] == expected_carry
    conn = trading.get_db()
    try:
        position = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        ).fetchone()
    finally:
        conn.close()
    assert position["interest_points"] == 0
    assert position["interest_carry_micropoints"] == expected_carry
    assert trading.verify_state()["ok"] is True


def test_margin_collateral_add_is_idempotent_with_client_key(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    first = trading.add_margin_collateral(
        actor=_actor(),
        position_uuid=opened["position"]["position_uuid"],
        amount_points=100,
        idempotency_key="same-click-key",
    )
    second = trading.add_margin_collateral(
        actor=_actor(),
        position_uuid=opened["position"]["position_uuid"],
        amount_points=100,
        idempotency_key="same-click-key",
    )

    assert first["position"]["collateral_points"] == 300
    assert second["position"]["collateral_points"] == 300
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["margin_positions"][0]["collateral_points"] == 300
    assert dashboard["funding"]["trial_credit"]["deployed_points"] == 300


def test_margin_collateral_add_is_idempotent_without_client_key(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    first = trading.add_margin_collateral(
        actor=_actor(),
        position_uuid=opened["position"]["position_uuid"],
        amount_points=100,
    )
    second = trading.add_margin_collateral(
        actor=_actor(),
        position_uuid=opened["position"]["position_uuid"],
        amount_points=100,
    )

    assert first["position"]["collateral_points"] == 300
    assert second["position"]["collateral_points"] == 300
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["margin_positions"][0]["collateral_points"] == 300
    assert dashboard["funding"]["trial_credit"]["deployed_points"] == 300


def test_margin_collateral_retry_across_minute_boundary_is_idempotent(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    class BoundaryDateTime(datetime):
        current = datetime(2026, 7, 11, 12, 0, 59, 900000)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(trading_margin_module, "datetime", BoundaryDateTime)
    monkeypatch.setattr(trading_margin_module, "_now_text", lambda: BoundaryDateTime.current.isoformat())
    first = trading.add_margin_collateral(
        actor=_actor(),
        position_uuid=opened["position"]["position_uuid"],
        amount_points=100,
    )
    BoundaryDateTime.current = datetime(2026, 7, 11, 12, 1, 0, 100000)
    second = trading.add_margin_collateral(
        actor=_actor(),
        position_uuid=opened["position"]["position_uuid"],
        amount_points=100,
    )

    assert first["position"]["collateral_points"] == 300
    assert second["position"]["collateral_points"] == 300
    conn = trading.get_db()
    try:
        operation_count = conn.execute(
            "SELECT COUNT(*) FROM trading_operation_idempotency WHERE operation='margin_collateral_add'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert operation_count == 1


def test_expired_trial_margin_collateral_can_be_closed_without_negative_accounting(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    conn = trading.get_db()
    conn.execute("UPDATE trading_trial_credits SET expires_at='2000-01-01T00:00:00' WHERE user_id=1")
    conn.commit()
    conn.close()

    expired = trading.user_dashboard(user_id=1)["funding"]["trial_credit"]
    assert expired["status"] == "expired"
    assert expired["available_points"] == 0
    assert expired["deployed_points"] == 200
    closed = trading.close_margin_position(actor=_actor(), position_uuid=opened["position"]["position_uuid"])

    assert closed["position"]["status"] == "closed"
    assert closed["funding"]["trial_credit"]["status"] == "expired"
    assert closed["funding"]["trial_credit"]["available_points"] == 0
    assert closed["funding"]["trial_credit"]["deployed_points"] == 0
    assert trading.verify_state()["ok"] is True


def test_short_borrow_position_profit_and_interest_enter_reserve_pool(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_apr_btc_eth_percent": 100,
            "borrow_interest_pool_pressure_multiplier": 0,
        },
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="short",
        quantity="0.1",
        collateral_points=300,
    )

    conn = trading.get_db()
    try:
        conn.execute(
            "UPDATE trading_margin_positions SET opened_at=datetime(opened_at, '-2 days') WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        )
        conn.commit()
    finally:
        conn.close()
    _set_live_price(trading, symbol="ETH/POINTS", price_points=4000)
    closed = trading.close_margin_position(actor=_actor(), position_uuid=opened["position"]["position_uuid"])

    assert closed["position"]["status"] == "closed"
    assert closed["interest_points"] == 3
    assert closed["position"]["interest_paid_points"] == 0
    assert closed["delta_points"] == 96
    assert trading.root_report()["reserve_pool"]["balance_points"] == trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS - 92
    economy = points.economy_stats()["economy_layer"]["supply_equation"]
    assert economy["exchange_margin_settlement_withheld_contra_points"] == 4
    assert economy["ledger_vs_economy_external_gap_points"] == 0
    assert economy["bridged_supply_equation_gap_points"] == 0
    assert economy["bridged_supply_equation_balanced"] is True
    assert trading.verify_state()["ok"] is True


def test_margin_liquidation_scan_closes_underwater_position(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=202, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_interest_percent_daily": 0,
            "margin_liquidation_enabled": True,
            "margin_maintenance_percent": 15,
        },
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    assert opened["position"]["interest_percent_daily"] == 0

    _set_live_price(trading, symbol="ETH/POINTS", price_points=3300)
    result = trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=10)

    assert result["ok"] is True
    assert result["scanned"] == 1
    assert result["liquidated"][0]["position_uuid"] == opened["position"]["position_uuid"]
    assert result["liquidated"][0]["account_risk"]["liquidation_required"] is True
    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["margin_positions"][0]["status"] == "liquidated"
    assert points.get_wallet(1)["points_frozen"] == 0
    notices = _notifications(trading, 1)
    assert any(row["type"] == "trading_margin_liquidated" for row in notices)
    assert (
        trading.root_report()["reserve_pool"]["balance_points"]
        == trading_engine_module.TRADING_FUNDING_POOL_INITIAL_POINTS
        + abs(result["liquidated"][0]["delta_points"])
    )
    assert trading.verify_state()["ok"] is True


def test_margin_liquidation_scan_uses_window_low_for_recovered_price(tmp_path):
    candles = [
        {"time_ms": 1_700_000_000_000, "close_points": 5000.0, "high_points": 5000.0, "low_points": 3300.0},
    ]
    points, trading = _services_with_history(tmp_path, prices={"ETH/POINTS": 5000.0}, candles=candles)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=202, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_interest_percent_daily": 0,
            "margin_liquidation_enabled": True,
            "margin_maintenance_percent": 15,
        },
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    result = trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=10)

    assert result["ok"] is True
    assert result["liquidated"][0]["position_uuid"] == opened["position"]["position_uuid"]
    assert result["liquidated"][0]["risk"]["price_points"] == pytest.approx(3300.0)


def test_margin_liquidation_scan_skips_when_fused_price_is_conservative(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_interest_percent_daily": 0,
            "margin_liquidation_enabled": True,
        },
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    original = trading._current_market_price_points

    def conservative_price(conn, market, *, with_meta=False, high_risk=False):
        if with_meta:
            return (
                3300.0,
                "fused_weighted",
                {
                    "price_health": "conservative",
                    "fallback_reason": "可用 order book 來源不足",
                    "excluded_sources": ["binance_public_api"],
                    "warnings": [{"code": "provider_count_low", "message": "可用 order book 來源不足", "severity": "critical"}],
                    "high_risk_blocked": True,
                    "high_risk_block_reason": "目前可用來源數不足，只能提供 degraded reference price",
                },
            )
        return original(conn, market, with_meta=with_meta)

    monkeypatch.setattr(trading, "_current_market_price_points", conservative_price)

    result = trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=10)

    assert result["ok"] is False
    assert result["liquidated"] == []
    assert result["errors"]
    assert result["errors"][0]["position_uuid"] == opened["position"]["position_uuid"]
    assert result["errors"][0]["price_health"] == "conservative"


def test_close_margin_position_rejects_high_risk_blocked_price(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True, "borrow_interest_percent_daily": 0},
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    def blocked_price(conn, market, *, with_meta=False, high_risk=False):
        if with_meta:
            return (
                3300.0,
                "fused_weighted",
                {
                    "price_health": "conservative",
                    "fallback_reason": "degraded",
                    "excluded_sources": [],
                    "warnings": [],
                    "high_risk_blocked": True,
                    "high_risk_block_reason": "risk-grade price unavailable",
                },
            )
        return 3300.0

    monkeypatch.setattr(trading, "_current_market_price_points", blocked_price)

    result = trading.close_margin_position(
        actor=_actor(),
        position_uuid=opened["position"]["position_uuid"],
    )

    assert result["position"]["status"] == "closed"


def test_close_margin_position_rejects_public_price_override(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True, "borrow_interest_percent_daily": 0},
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    with pytest.raises(ValueError, match="internal price override is not allowed"):
        trading.close_margin_position(
            actor=_actor(),
            position_uuid=opened["position"]["position_uuid"],
            price_override_points=3300,
            price_source_override="public_override",
        )


def test_margin_risk_payload_accepts_margin_short_alias(tmp_path):
    _points, trading = _services(tmp_path)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True, "borrow_interest_percent_daily": 0},
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="short",
        quantity="0.1",
        collateral_points=300,
    )

    conn = trading.get_db()
    try:
        row = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        ).fetchone()
        row_dict = dict(row)
        row_dict["position_type"] = "margin_short"
        market = trading._market(conn, row["market_symbol"])
        risk = trading._margin_risk_payload(conn, row_dict, market=market, now_text="2026-05-04T10:30:00")
    finally:
        conn.close()

    assert risk["risk_status"] == "short_price_risk"
    assert "價格上漲" in risk["risk_reason"]


def test_margin_risk_notification_failure_emits_audit(tmp_path, monkeypatch):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "margin_liquidation_enabled": True,
            "margin_maintenance_percent": 15,
        },
        markets=[],
    )
    trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=120,
    )
    _set_live_price(trading, symbol="ETH/POINTS", price_points=4550)

    def fail_notification(*args, **kwargs):
        raise RuntimeError("notification transport failed")

    monkeypatch.setattr(trading_margin_module, "create_trading_user_notification", fail_notification)
    result = trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=10)

    assert result["ok"] is True
    audit = next(
        row
        for row in trading.root_report()["audit_events"]
        if row["event_type"] == "TRADING_MARGIN_RISK_NOTIFY_FAILED"
    )
    assert audit["severity"] == "warning"
    metadata = json.loads(audit["metadata_json"] or "{}")
    assert metadata["error"].startswith("notification transport failed")


def test_internal_test_force_liquidation_cannot_read_production_margin_position(tmp_path):
    points, trading = _services(tmp_path)
    conn = trading.get_db()
    try:
        ensure_snapshot_schema(conn)
        conn.commit()
    finally:
        conn.close()
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=202, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_interest_percent_daily": 0,
            "margin_liquidation_enabled": True,
            "margin_maintenance_percent": 15,
        },
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    conn = trading.get_db()
    try:
        prod_ledger_before = int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0)
        prod_chain_before = int(conn.execute("SELECT COUNT(*) FROM points_chain_blocks").fetchone()[0] or 0)
    finally:
        conn.close()

    with pytest.raises(ValueError, match="margin position not found"):
        trading.close_margin_position(
            actor={"username": "system", "role": "system"},
            position_uuid=opened["position"]["position_uuid"],
            force_liquidation=True,
            ctx=_sm_ctx("internal_test", tester_id=7),
        )

    conn = trading.get_db()
    try:
        row = conn.execute(
            "SELECT status FROM trading_margin_positions WHERE position_uuid=?",
            (opened["position"]["position_uuid"],),
        ).fetchone()
        prod_ledger_after = int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0)
        prod_chain_after = int(conn.execute("SELECT COUNT(*) FROM points_chain_blocks").fetchone()[0] or 0)
    finally:
        conn.close()

    assert row["status"] == "open"
    assert prod_ledger_after == prod_ledger_before
    assert prod_chain_after == prod_chain_before


def test_internal_test_liquidation_rejects_shadow_world_before_prod_mutation(tmp_path):
    _points, trading = _services(tmp_path)
    conn = trading.get_db()
    try:
        ensure_snapshot_schema(conn)
        now = trading_engine_module._now()
        conn.execute(
            """
            INSERT INTO test_shadow_margin_positions (
                position_uuid, tester_user_id, user_id, market_symbol, position_type,
                quantity_units, entry_price_points, principal_points, collateral_points,
                interest_percent_daily, status, opened_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            ("shadow-liquidation-1", 7, 1, "ETH/POINTS", "margin_long", 10_000_000, 5000, 300, 200, 0.0, now, now),
        )
        conn.commit()
        prod_ledger_before = int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0)
        prod_chain_before = int(conn.execute("SELECT COUNT(*) FROM points_chain_blocks").fetchone()[0] or 0)
    finally:
        conn.close()

    with pytest.raises(ValueError, match="shadow liquidation is not supported yet"):
        trading.close_margin_position(
            actor={"username": "system", "role": "system"},
            position_uuid="shadow-liquidation-1",
            force_liquidation=True,
            ctx=_sm_ctx("internal_test", tester_id=7),
        )

    conn = trading.get_db()
    try:
        shadow_row = conn.execute(
            "SELECT status FROM test_shadow_margin_positions WHERE position_uuid='shadow-liquidation-1'"
        ).fetchone()
        prod_ledger_after = int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0)
        prod_chain_after = int(conn.execute("SELECT COUNT(*) FROM points_chain_blocks").fetchone()[0] or 0)
    finally:
        conn.close()

    assert shadow_row["status"] == "open"
    assert prod_ledger_after == prod_ledger_before
    assert prod_chain_after == prod_chain_before


def test_internal_test_liquidation_scan_returns_disabled_without_prod_side_effects(tmp_path):
    _points, trading = _services(tmp_path)
    conn = trading.get_db()
    try:
        ensure_snapshot_schema(conn)
        now = trading_engine_module._now()
        conn.execute(
            """
            INSERT INTO test_shadow_margin_positions (
                position_uuid, tester_user_id, user_id, market_symbol, position_type,
                quantity_units, entry_price_points, principal_points, collateral_points,
                interest_percent_daily, status, opened_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            ("shadow-scan-1", 9, 1, "ETH/POINTS", "margin_long", 10_000_000, 5000, 300, 200, 0.0, now, now),
        )
        conn.commit()
        prod_ledger_before = int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0)
        prod_chain_before = int(conn.execute("SELECT COUNT(*) FROM points_chain_blocks").fetchone()[0] or 0)
    finally:
        conn.close()

    result = trading.scan_margin_liquidations(
        actor={"username": "system", "role": "system"},
        limit=10,
        ctx=_sm_ctx("internal_test", tester_id=9),
    )

    assert result["ok"] is True
    assert result["enabled"] is False
    assert result["reason"] == "shadow_liquidation_unsupported"

    conn = trading.get_db()
    try:
        prod_ledger_after = int(conn.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] or 0)
        prod_chain_after = int(conn.execute("SELECT COUNT(*) FROM points_chain_blocks").fetchone()[0] or 0)
    finally:
        conn.close()

    assert prod_ledger_after == prod_ledger_before
    assert prod_chain_after == prod_chain_before


def test_cross_margin_free_margin_prevents_single_position_liquidation(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    _deplete_trial_credit(trading, user_id=1)
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "borrow_interest_percent_daily": 0,
            "margin_liquidation_enabled": True,
            "margin_maintenance_percent": 15,
        },
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    _set_live_price(trading, symbol="ETH/POINTS", price_points=3300)
    dashboard = trading.user_dashboard(user_id=1)
    position_risk = dashboard["margin_positions"][0]["risk"]
    account_risk = dashboard["margin_summary"]
    result = trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=10)

    assert position_risk["liquidation_required"] is True
    assert account_risk["mode"] == "cross_margin"
    assert account_risk["liquidation_required"] is False
    assert account_risk["free_margin_points"] > 0
    assert result["liquidated"] == []
    assert trading.user_dashboard(user_id=1)["margin_positions"][0]["position_uuid"] == opened["position"]["position_uuid"]
    assert trading.user_dashboard(user_id=1)["margin_positions"][0]["status"] == "open"


def test_margin_scan_notifies_user_when_position_is_near_liquidation(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "margin_liquidation_enabled": True,
            "margin_maintenance_percent": 15,
        },
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=120,
    )

    _set_live_price(trading, symbol="ETH/POINTS", price_points=4550)
    first_scan = trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=10)
    second_scan = trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=10)

    assert first_scan["liquidated"] == []
    assert second_scan["liquidated"] == []
    notes = [row for row in _notifications(trading, 1) if row["type"] == "trading_margin_near_liquidation"]
    assert len(notes) == 1
    assert "進階交易接近強平" == notes[0]["title"]
    assert opened["position"]["position_uuid"] in notes[0]["body"]
    assert "強平價" in notes[0]["body"]


def test_margin_scan_notifies_user_when_price_jumps_sharply(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={
            "borrowing_enabled": True,
            "margin_liquidation_enabled": True,
        },
        markets=[],
    )
    trading.update_market(
        actor=_actor(3, "root", "super_admin"),
        symbol="ETH/POINTS",
        max_price_jump_percent=10,
        confirm_jump=True,
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    _set_live_price(trading, symbol="ETH/POINTS", price_points=5700, max_price_jump_percent=10)
    trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=10)
    trading.scan_margin_liquidations(actor={"username": "system", "role": "system"}, limit=10)

    notes = [row for row in _notifications(trading, 1) if row["type"] == "trading_margin_price_jump"]
    assert len(notes) == 1
    assert "進階交易價格大幅波動" == notes[0]["title"]
    assert opened["position"]["position_uuid"] in notes[0]["body"]
    assert "14.00%" in notes[0]["body"]


def test_force_liquidation_rechecks_recovered_position(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.update_root_settings(
        actor=_actor(3, "root", "super_admin"),
        settings={"borrowing_enabled": True, "margin_liquidation_enabled": True, "margin_maintenance_percent": 15},
        markets=[],
    )
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )

    with pytest.raises(ValueError, match="recovered above liquidation"):
        trading.close_margin_position(
            actor={"username": "system", "role": "system"},
            position_uuid=opened["position"]["position_uuid"],
            force_liquidation=True,
        )
    assert trading.user_dashboard(user_id=1)["margin_positions"][0]["status"] == "open"


def test_margin_collateral_tampering_enters_trading_safe_mode(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1000, action_type="admin_adjust_credit")
    trading.update_root_settings(actor=_actor(3, "root", "super_admin"), settings={"borrowing_enabled": True}, markets=[])
    opened = trading.open_margin_position(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        position_type="margin_long",
        quantity="0.1",
        collateral_points=200,
    )
    conn = trading.get_db()
    conn.execute("UPDATE trading_margin_positions SET collateral_points=1 WHERE position_uuid=?", (opened["position"]["position_uuid"],))
    conn.commit()
    conn.close()

    verification = trading.verify_state()

    assert verification["ok"] is False
    assert any(error["type"] == "margin_collateral_lock_mismatch" for error in verification["errors"])


def test_position_replay_mismatch_enters_trading_safe_mode(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.1")

    conn = trading.get_db()
    conn.execute("UPDATE trading_spot_positions SET quantity_units=1 WHERE user_id=1 AND market_symbol='ETH/POINTS'")
    conn.commit()
    conn.close()

    verification = trading.verify_state()
    assert verification["ok"] is False
    with pytest.raises(ValueError, match="safe mode"):
        trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.01")


def test_reserve_pool_tampering_enters_trading_safe_mode(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.1")

    conn = trading.get_db()
    conn.execute("UPDATE trading_reserve_pool SET balance_points=1 WHERE id=1")
    conn.commit()
    conn.close()

    verification = trading.verify_state()
    assert verification["ok"] is False
    assert any(error["type"] == "reserve_pool_replay_mismatch" for error in verification["errors"])
    updated = trading.update_market(actor=_actor(3, "root", "super_admin"), symbol="ETH/POINTS", enabled=False)
    assert updated["market"]["enabled"] is False


def test_open_order_frozen_tampering_enters_trading_safe_mode(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    order = trading.place_order(
        actor=_actor(),
        market_symbol="ETH/POINTS",
        side="buy",
        order_type="limit",
        quantity="0.1",
        limit_price_points=4000,
    )["order"]

    conn = trading.get_db()
    conn.execute("UPDATE trading_orders SET frozen_points=1 WHERE order_uuid=?", (order["order_uuid"],))
    conn.commit()
    conn.close()

    verification = trading.verify_state()
    assert verification["ok"] is False
    assert any(error["type"] == "open_order_total_frozen_points_mismatch" for error in verification["errors"])


def test_sell_order_rejects_zero_net_credit_before_locking_position(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.00000001")
    trading.update_market(actor=_actor(3, "root", "super_admin"), symbol="ETH/POINTS", fee_rate_percent=50)

    with pytest.raises(ValueError, match="sell notional after fee"):
        trading.place_order(
            actor=_actor(),
            market_symbol="ETH/POINTS",
            side="sell",
            order_type="limit",
            quantity="0.00000001",
            limit_price_points=5000,
        )

    dashboard = trading.user_dashboard(user_id=1)
    assert dashboard["positions"][0]["locked_quantity_units"] == 0


def test_trading_writes_stop_when_pointschain_enters_safe_mode(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=2000, action_type="admin_adjust_credit")

    conn = trading.get_db()
    points.ensure_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO points_chain_recovery_state (
            id, safe_mode, reason, verification_json, forensic_bundle_id,
            restore_plan_json, created_at, updated_at
        ) VALUES (1, 1, 'chain_verification_failed', '{}', 'bundle-test', '{}', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="PointsChain safe mode active"):
        trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="0.01")


def test_workflow_branch_steps_are_persisted_and_do_not_repeat(tmp_path):
    _, trading = _services(tmp_path)
    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [{
            "id": "entry",
            "name": "分批進場",
            "priority": 10,
            "logic": "AND",
            "cooldown_seconds": 0,
            "conditions": [{"type": "price_below", "value": 6000}],
            "actions": [
                {"type": "buy_amount", "amount_points": 50, "step": 1},
                {"type": "buy_amount", "amount_points": 60, "step": 2},
            ],
        }],
    }
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "name": "two step workflow",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "max_runs": 3,
            "cooldown_seconds": 0,
            "workflow_json": workflow,
            "enabled": True,
        },
    )

    first = trading.run_trading_bots(actor=_actor(), limit=10)
    second = trading.run_trading_bots(actor=_actor(), limit=10)
    third = trading.run_trading_bots(actor=_actor(), limit=10)

    assert len(first["triggered"]) == 1
    assert len(second["triggered"]) == 1
    assert third["triggered"] == []
    assert third["skipped"][0]["reason"] == "workflow_not_matched"
    dashboard = trading.user_dashboard(user_id=1)
    assert len(dashboard["orders"]) == 2
    assert dashboard["bots"][0]["run_count"] == 2
    assert dashboard["bots"][0]["execution_state"]["branch_step_counts"]["entry"] == 2


def test_incremental_spot_buys_keep_average_cost_sane(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=15000, action_type="admin_adjust_credit")

    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="2")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="sell", order_type="market", quantity="0.5")

    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "dca",
            "name": "avg cost dca",
            "market_symbol": "ETH/POINTS",
            "budget_points": 100,
            "interval_hours": 1,
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )
    trading.run_trading_bots(actor=_actor(), limit=10)

    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "avg cost conditional",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "trigger_type": "price_below",
            "trigger_price_points": 6000,
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )
    trading.run_trading_bots(actor=_actor(), limit=10)

    dashboard = trading.user_dashboard(user_id=1)
    position = next(row for row in dashboard["positions"] if row["market_symbol"] == "ETH/POINTS")

    assert position["quantity"] == "1.5396"
    assert 5000.0 <= float(position["avg_cost_points"]) < 5100.0


def test_workflow_buy_amount_runs_after_incremental_spot_buys(tmp_path):
    points, trading = _services(tmp_path)
    points.record_transaction(user_id=1, currency_type="points", direction="credit", amount=15000, action_type="admin_adjust_credit")

    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="buy", order_type="market", quantity="2")
    trading.place_order(actor=_actor(), market_symbol="ETH/POINTS", side="sell", order_type="market", quantity="0.5")

    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "dca",
            "name": "workflow dca seed",
            "market_symbol": "ETH/POINTS",
            "budget_points": 100,
            "interval_hours": 1,
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )
    trading.run_trading_bots(actor=_actor(), limit=10)

    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "workflow conditional seed",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.01",
            "trigger_type": "price_below",
            "trigger_price_points": 6000,
            "max_runs": 1,
            "cooldown_seconds": 0,
            "enabled": True,
        },
    )
    trading.run_trading_bots(actor=_actor(), limit=10)

    workflow = {
        "version": 1,
        "strategy_kind": "workflow",
        "branches": [{
            "id": "entry",
            "name": "分批加倉",
            "priority": 10,
            "logic": "AND",
            "cooldown_seconds": 0,
            "conditions": [{"type": "price_below", "value": 6000}],
            "actions": [
                {"type": "buy_amount", "amount_points": 100, "step": 1, "order_type": "market"},
                {"type": "buy_amount", "amount_points": 200, "step": 2, "order_type": "market"},
            ],
        }],
    }
    trading.save_trading_bot(
        actor=_actor(),
        payload={
            "bot_type": "conditional",
            "name": "workflow avg cost guard",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.00000001",
            "trigger_type": "always",
            "max_runs": 3,
            "cooldown_seconds": 0,
            "workflow_json": workflow,
            "enabled": True,
        },
    )

    first = trading.run_trading_bots(actor=_actor(), limit=10)
    second = trading.run_trading_bots(actor=_actor(), limit=10)
    third = trading.run_trading_bots(actor=_actor(), limit=10)

    assert len(first["triggered"]) == 1
    assert first["failed"] == []
    assert len(second["triggered"]) == 1
    assert second["failed"] == []
    assert third["triggered"] == []

    dashboard = trading.user_dashboard(user_id=1)
    workflow_bot = next(row for row in dashboard["bots"] if row["name"] == "workflow avg cost guard")
    workflow_orders = [row for row in dashboard["orders"] if row.get("bot_name") == "workflow avg cost guard"]
    position = next(row for row in dashboard["positions"] if row["market_symbol"] == "ETH/POINTS")

    assert len(workflow_orders) == 2
    assert workflow_bot["run_count"] == 2
    assert workflow_bot["execution_state"]["branch_step_counts"]["entry"] == 2
    assert 5000.0 <= float(position["avg_cost_points"]) < 5100.0


def test_workflow_graph_rejects_action_unreachable_from_start(tmp_path):
    _, trading = _services(tmp_path)
    workflow = {
        "version": 2,
        "strategy_kind": "workflow_graph",
        "start_node_id": "start",
        "nodes": [
            {"id": "start", "type": "start", "outputs": ["out"]},
            {"id": "gate", "type": "condition", "condition": {"type": "price_below", "value": 6000}, "outputs": ["true", "false"]},
            {"id": "buy", "type": "action", "action": {"type": "buy_amount", "amount_points": 50, "step": 1}},
        ],
        "edges": [{"from": "gate", "from_port": "true", "to": "buy", "to_port": "in"}],
    }

    with pytest.raises(ValueError, match="reachable from start"):
        trading.save_trading_bot(
            actor=_actor(),
            payload={
                "name": "unreachable action",
                "market_symbol": "ETH/POINTS",
                "side": "buy",
                "order_type": "market",
                "quantity": "0.01",
                "workflow_json": workflow,
                "enabled": True,
            },
        )
