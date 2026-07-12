"""Trading dashboard/reporting helpers extracted from the engine facade."""

from __future__ import annotations

from services.trading.accounting.core import (
    fee_micropoints,
    points_from_micropoints_ceil,
    notional_points,
    units_to_quantity,
)
from services.trading.constants import POINT_MICRO_SCALE
from services.trading.funding import funding_payload as funding_payload_helper
from services.trading.validators import _apr_percent_from_daily, _billable_interest_hours_from_elapsed_seconds, _to_decimal
from services.points_chain.wallet_identity import wallet_onboarding_status


def funding_payload(service, conn, user_id, *, source_wallet_address=None):
    return funding_payload_helper(
        service,
        conn,
        user_id,
        root_simulated_initial_points=service.ROOT_SIMULATED_INITIAL_POINTS,
        trial_credit_days=service.TRIAL_CREDIT_DAYS,
        source_wallet_address=source_wallet_address,
    )


def _asset_overview_price(market, snapshot):
    if snapshot:
        price = (
            snapshot.get("risk_grade_price_points")
            if hasattr(snapshot, "get")
            else snapshot["risk_grade_price_points"]
        )
        if price is None:
            price = snapshot.get("reference_price_points") if hasattr(snapshot, "get") else snapshot["reference_price_points"]
        if price is not None:
            return float(_to_decimal(price, name="snapshot_price_points", minimum=0))
    return float(_to_decimal((market or {}).get("manual_price_points") or 0, name="manual_price_points", minimum=0))


def _legacy_wallet_funding(conn, user_id):
    columns = {
        str(row[1] if not hasattr(row, "keys") else row["name"])
        for row in conn.execute("PRAGMA table_info(points_wallets)").fetchall()
    }
    if {"soft_balance", "hard_balance", "soft_frozen", "hard_frozen"}.issubset(columns):
        row = conn.execute(
            """
            SELECT COALESCE(soft_balance, 0) + COALESCE(hard_balance, 0) AS available_points,
                   COALESCE(soft_frozen, 0) + COALESCE(hard_frozen, 0) AS frozen_points
            FROM points_wallets WHERE user_id=?
            """,
            (int(user_id),),
        ).fetchone()
    elif {"points_balance", "points_frozen"}.issubset(columns):
        row = conn.execute(
            "SELECT points_balance AS available_points, points_frozen AS frozen_points FROM points_wallets WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    else:
        row = None
    return {
        "available_points": int(row["available_points"] or 0) if row else 0,
        "frozen_points": int(row["frozen_points"] or 0) if row else 0,
    }


def _asset_overview_user_funding(service, conn, user_id):
    user_id = int(user_id)
    user = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    if user and str(user["username"] or "") == "root":
        account = conn.execute("SELECT * FROM trading_sim_accounts WHERE user_id=?", (user_id,)).fetchone()
        return {
            "available_points": int(account["balance_points"] or 0) if account else int(service.ROOT_SIMULATED_INITIAL_POINTS or 0),
            "locked_points": int(account["locked_points"] or 0) if account else 0,
            "source": "root_simulated_read_model",
        }

    wallet_available = 0
    wallet_locked = 0
    try:
        points_service = service.points_service
        branch = points_service._canonical_branch_uuid(conn)
        balance_rows = conn.execute(
            """
            SELECT COALESCE(SUM(available_points), 0) AS available_points,
                   COALESCE(SUM(frozen_points), 0) AS frozen_points
            FROM points_wallet_identity_balances
            WHERE chain_branch=? AND user_id=?
            """,
            (branch, user_id),
        ).fetchone()
        wallet_available = int(balance_rows["available_points"] or 0) if balance_rows else 0
        wallet_locked = int(balance_rows["frozen_points"] or 0) if balance_rows else 0
        if wallet_available == 0 and wallet_locked == 0:
            legacy = _legacy_wallet_funding(conn, user_id)
            wallet_available = legacy["available_points"]
            wallet_locked = legacy["frozen_points"]
    except Exception:
        legacy = _legacy_wallet_funding(conn, user_id)
        wallet_available = legacy["available_points"]
        wallet_locked = legacy["frozen_points"]

    trial_available = 0
    trial_locked = 0
    trial = service._trial_credit_row(conn, user_id)
    if trial and str(trial["status"] or "") == "active":
        trial_available = int(trial["available_points"] or 0)
        trial_locked = int(trial["locked_points"] or 0)
    return {
        "available_points": wallet_available + trial_available,
        "locked_points": wallet_locked + trial_locked,
        "source": "durable_wallet_identity_balances",
    }


def user_asset_overview(service, *, user_id):
    """Return the platform-center trading asset overview without loading the full dashboard.

    This read model intentionally avoids recent orders/fills, bot runs, wallet
    onboarding, and live price providers. It uses durable wallet balance state
    plus stored market price snapshots so the route stays off the finance hot
    write path.
    """
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        tables, _route_ctx = service._sql_tables()
        positions_table = tables["positions"]
        funding = _asset_overview_user_funding(service, conn, user_id)
        markets = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM trading_markets WHERE enabled=1 AND spot_enabled=1 ORDER BY sort_order ASC, symbol ASC"
            ).fetchall()
        ]
        market_map = {str(row.get("symbol") or ""): row for row in markets}
        snapshots = {}
        if market_map:
            placeholders = ",".join("?" for _ in market_map)
            for row in conn.execute(
                f"SELECT * FROM trading_market_price_snapshots WHERE market_symbol IN ({placeholders})",
                tuple(market_map.keys()),
            ).fetchall():
                snapshots[str(row["market_symbol"] or "")] = dict(row)

        confidence_low = 0
        for symbol in market_map:
            snapshot = snapshots.get(symbol)
            confidence = str((snapshot or {}).get("confidence") or "").strip().lower()
            if confidence in {"low", "very_low", "untrusted", "unknown"}:
                confidence_low += 1

        spot_value = 0
        open_spot_positions = 0
        for row in conn.execute(
            f"""
            SELECT market_symbol, quantity_units, locked_quantity_units
            FROM {positions_table}
            WHERE user_id=?
            """,
            (int(user_id),),
        ).fetchall():
            symbol = str(row["market_symbol"] or "")
            quantity_units = int(row["quantity_units"] or 0) + int(row["locked_quantity_units"] or 0)
            if quantity_units <= 0:
                continue
            open_spot_positions += 1
            spot_value += notional_points(
                quantity_units,
                _asset_overview_price(market_map.get(symbol), snapshots.get(symbol)),
            )

        margin_value = 0
        unrealized_pnl = 0
        accrued_interest = 0
        open_margin_count = 0
        for row in conn.execute(
            """
            SELECT market_symbol, position_type, quantity_units, entry_price_points,
                   collateral_points, principal_points, interest_points, interest_carry_micropoints,
                   status
            FROM trading_margin_positions
            WHERE user_id=? AND status='open'
            """,
            (int(user_id),),
        ).fetchall():
            symbol = str(row["market_symbol"] or "")
            quantity_units = int(row["quantity_units"] or 0)
            entry_price = float(_to_decimal(row["entry_price_points"] or 0, name="entry_price_points", minimum=0))
            current_price = _asset_overview_price(market_map.get(symbol), snapshots.get(symbol)) or entry_price
            entry_value = notional_points(quantity_units, entry_price)
            current_value = notional_points(quantity_units, current_price)
            if str(row["position_type"] or "") == "short":
                pnl = entry_value - current_value
            else:
                pnl = current_value - entry_value
            interest_due = int(row["interest_points"] or 0) + points_from_micropoints_ceil(int(row["interest_carry_micropoints"] or 0))
            equity = int(row["collateral_points"] or 0) + pnl - interest_due
            open_margin_count += 1
            unrealized_pnl += pnl
            accrued_interest += interest_due
            margin_value += equity

        available = int(funding.get("available_points") or 0)
        locked = int(funding.get("locked_points") or 0)
        overview = {
            "available_points": available,
            "locked_points": locked,
            "spot_market_value_points": int(spot_value),
            "margin_position_equity_points": int(margin_value),
            "unrealized_pnl_points": int(unrealized_pnl),
            "accrued_interest_points": int(accrued_interest),
            "total_equity_points": int(available + locked + spot_value + margin_value),
            "open_spot_positions": int(open_spot_positions),
            "open_margin_positions": int(open_margin_count),
            "market_count": int(len(markets)),
            "low_confidence_price_count": int(confidence_low),
            "confidence_note": "價格可信度只作風險提示，不阻擋積分交易。",
            "read_model_source": "trading_asset_overview_v1",
            "funding_source": funding.get("source") or "",
        }
        conn.commit()
        return overview
    finally:
        conn.close()


def position_payload_with_metrics(service, row, *, market=None, realized_points=0, total_fees=0):
    item = service._position_payload(row)
    quantity_units = int(item["quantity_units"] or 0) + int(item["locked_quantity_units"] or 0)
    avg_cost = float(_to_decimal(item["avg_cost_points"] or 0, name="avg_cost_points", minimum=0))
    reference_price = float(
        _to_decimal(
            (market or {}).get("reference_price_points")
            or (market or {}).get("manual_price_points")
            or 0,
            name="reference_price_points",
            minimum=0,
        )
    )
    risk_grade_price = float(
        _to_decimal(
            (market or {}).get("risk_grade_price_points")
            or (market or {}).get("reference_price_points")
            or (market or {}).get("manual_price_points")
            or 0,
            name="risk_grade_price_points",
            minimum=0,
        )
    )
    fee_rate_percent = float((market or {}).get("fee_rate_percent") or 0)
    gross_cost = notional_points(quantity_units, avg_cost) if quantity_units and avg_cost else 0
    reference_current_value = notional_points(quantity_units, reference_price) if quantity_units and reference_price else 0
    risk_grade_current_value = notional_points(quantity_units, risk_grade_price) if quantity_units and risk_grade_price else 0
    pending_fee_micro = int(item.get("fee_carry_micropoints") or 0)
    reference_exit_fee_micro = fee_micropoints(reference_current_value, fee_rate_percent) if reference_current_value else 0
    risk_grade_exit_fee_micro = fee_micropoints(risk_grade_current_value, fee_rate_percent) if risk_grade_current_value else 0
    estimated_buy_fee = points_from_micropoints_ceil(pending_fee_micro)
    reference_exit_fee = points_from_micropoints_ceil(reference_exit_fee_micro)
    risk_grade_exit_fee = points_from_micropoints_ceil(risk_grade_exit_fee_micro)
    reference_settlement_fee = points_from_micropoints_ceil(pending_fee_micro + reference_exit_fee_micro)
    risk_grade_settlement_fee = points_from_micropoints_ceil(pending_fee_micro + risk_grade_exit_fee_micro)
    reference_cost_basis = gross_cost + reference_settlement_fee
    risk_grade_cost_basis = gross_cost + risk_grade_settlement_fee
    reference_unrealized = reference_current_value - reference_cost_basis if quantity_units else 0
    risk_grade_unrealized = risk_grade_current_value - risk_grade_cost_basis if quantity_units else 0
    item.update({
        "available_quantity_units": int(item["quantity_units"] or 0),
        "total_quantity_units": quantity_units,
        "total_quantity": units_to_quantity(quantity_units),
        "reference_price_points": reference_price,
        "risk_grade_price_points": risk_grade_price,
        "current_price_points": reference_price,
        "gross_cost_points": gross_cost,
        "reference_current_value_points": reference_current_value,
        "current_value_points": reference_current_value,
        "risk_grade_current_value_points": risk_grade_current_value,
        "estimated_buy_fee_points": estimated_buy_fee,
        "pending_fee_micropoints": pending_fee_micro,
        "pending_fee_points_exact": round(pending_fee_micro / POINT_MICRO_SCALE, 8),
        "reference_estimated_exit_fee_points": reference_exit_fee,
        "estimated_exit_fee_points": risk_grade_exit_fee,
        "reference_settlement_fee_points": reference_settlement_fee,
        "settlement_fee_points": risk_grade_settlement_fee,
        "reference_cost_basis_points": reference_cost_basis,
        "cost_basis_points": risk_grade_cost_basis,
        "reference_unrealized_pnl_points": reference_unrealized,
        "risk_grade_unrealized_pnl_points": risk_grade_unrealized,
        "unrealized_pnl_points": risk_grade_unrealized,
        "realized_pnl_points": int(realized_points or 0),
        "total_pnl_points": int(realized_points or 0) + risk_grade_unrealized,
        "total_fee_points": int(total_fees or 0),
        "reference_price_context": (market or {}).get("reference_price_context") if isinstance(market, dict) else None,
        "risk_grade_price_context": (market or {}).get("risk_grade_price_context") if isinstance(market, dict) else None,
    })
    item["pnl_percent"] = round((risk_grade_unrealized / risk_grade_cost_basis) * 100, 4) if risk_grade_cost_basis else 0
    return item


def margin_trade_records(service, conn, user_id, *, limit=50):
    records = []
    rows = conn.execute(
        "SELECT * FROM trading_margin_positions WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (int(user_id), int(limit)),
    ).fetchall()
    position_uuids = [str(row["position_uuid"] or "") for row in rows if row["position_uuid"]]
    pending_by_position = {}
    if position_uuids:
        placeholders = ",".join("?" for _ in position_uuids)
        for pending in conn.execute(
            f"""
            SELECT position_uuid, SUM(amount_points) AS amount_points,
                   MAX(governance_proposal_uuid) AS governance_proposal_uuid
            FROM trading_pending_profit
            WHERE position_uuid IN ({placeholders}) AND status='pending'
            GROUP BY position_uuid
            """,
            tuple(position_uuids),
        ).fetchall():
            pending_by_position[str(pending["position_uuid"] or "")] = {
                "amount_points": int(pending["amount_points"] or 0),
                "governance_proposal_uuid": str(pending["governance_proposal_uuid"] or ""),
            }
    for row in rows:
        payload = service._margin_position_payload(row)
        label = payload["position_label"]
        entry_price = float(_to_decimal(row["entry_price_points"] or 0, name="entry_price_points", minimum=0))
        notional = notional_points(int(row["quantity_units"] or 0), entry_price)
        records.append({
            "record_type": "margin_open",
            "fill_uuid": f"margin-open:{row['position_uuid']}",
            "position_uuid": row["position_uuid"],
            "side": f"{label}開倉",
            "market_symbol": row["market_symbol"],
            "quantity": payload["quantity"],
            "price_points": entry_price,
            "notional_points": notional,
            "fee_points": int(row["open_fee_points"] or 0),
            "interest_points": 0,
            "realized_pnl_points": 0,
            "status": "open",
            "created_at": row["opened_at"],
        })
        if row["closed_at"]:
            close_type = "margin_liquidation" if row["status"] == "liquidated" else "margin_close"
            realized_pnl = int(row["realized_pnl_points"] or 0)
            pending_profit = pending_by_position.get(str(row["position_uuid"] or ""), {})
            pending_profit_points = int(pending_profit.get("amount_points") or 0)
            paid_profit_points = max(0, realized_pnl) - pending_profit_points
            records.append({
                "record_type": close_type,
                "fill_uuid": f"{close_type}:{row['position_uuid']}",
                "position_uuid": row["position_uuid"],
                "side": f"{label}{'強平' if row['status'] == 'liquidated' else '平倉'}",
                "market_symbol": row["market_symbol"],
                "quantity": payload["quantity"],
                "price_points": float(_to_decimal(row["exit_price_points"] or 0, name="exit_price_points", minimum=0)),
                "notional_points": notional_points(
                    int(row["quantity_units"] or 0),
                    float(_to_decimal(row["exit_price_points"] or 0, name="exit_price_points", minimum=0)),
                ) if row["exit_price_points"] else 0,
                "fee_points": int(row["close_fee_points"] or 0),
                "interest_points": int(row["interest_points"] or 0),
                "realized_pnl_points": realized_pnl,
                "paid_profit_points": max(0, paid_profit_points),
                "pending_profit_points": pending_profit_points,
                "governance_proposal_uuid": pending_profit.get("governance_proposal_uuid", ""),
                "status": row["status"],
                "created_at": row["closed_at"],
            })
    return sorted(records, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:int(limit)]


def user_dashboard(service, *, user_id, source_wallet_address=None):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        service._ensure_trial_credit(conn, user_id)
        conn.commit()
        tables, _route_ctx = service._sql_tables()
        positions_table = tables["positions"]
        orders_table = tables["orders"]
        state = service._state(conn)
        markets = []
        for row in conn.execute("SELECT * FROM trading_markets WHERE enabled=1").fetchall():
            market_item = service._market_payload(row)
            reference_context, risk_grade_context = service._stored_market_price_contexts(market_item)
            markets.append(
                service._attach_market_price_contexts(
                    market_item,
                    reference_context=reference_context,
                    risk_grade_context=risk_grade_context,
                )
            )
        markets = sorted(markets, key=service._runtime_market_sort_key)
        market_map = {row["symbol"]: row for row in markets}
        realized_map = service._spot_realized_map(conn, user_id)
        fee_map = service._spot_fee_map(conn, user_id)
        positions = [
            service._position_payload_with_metrics(
                row,
                market=market_map.get(row["market_symbol"]),
                realized_points=realized_map.get(row["market_symbol"], 0),
                total_fees=fee_map.get(row["market_symbol"], 0),
            )
            for row in conn.execute(
                f"SELECT * FROM {positions_table} WHERE user_id=? ORDER BY market_symbol",
                (int(user_id),),
            ).fetchall()
        ]
        futures_positions = [
            service._futures_position_payload(row)
            for row in conn.execute("SELECT * FROM trading_futures_positions WHERE user_id=? ORDER BY id DESC LIMIT 50", (int(user_id),)).fetchall()
        ]
        margin_positions = [
            service._margin_position_payload_with_risk(conn, row, market=market_map.get(row["market_symbol"]))
            for row in conn.execute("SELECT * FROM trading_margin_positions WHERE user_id=? ORDER BY id DESC LIMIT 50", (int(user_id),)).fetchall()
        ]
        try:
            wallet_status = wallet_onboarding_status(conn, points_service=service.points_service, user_id=user_id)
            wallets = wallet_status.get("wallets") or []
        except Exception:
            wallets = []
        bot_order_map = {
            row["order_uuid"]: row["bot_name"]
            for row in conn.execute(
                """
                SELECT r.order_uuid, b.name AS bot_name
                FROM trading_bot_runs r
                JOIN trading_bots b ON b.id = r.bot_id
                WHERE r.user_id=? AND r.order_uuid IS NOT NULL
                """,
                (int(user_id),),
            ).fetchall()
        }
        for row in conn.execute(
            """
            SELECT go.trading_order_uuid AS order_uuid, gb.name AS bot_name
            FROM trading_grid_orders go
            JOIN trading_grid_bots gb ON gb.id = go.grid_bot_id
            WHERE go.user_id=? AND go.trading_order_uuid IS NOT NULL
            """,
            (int(user_id),),
        ).fetchall():
            bot_order_map[row["order_uuid"]] = row["bot_name"]
        raw_orders = conn.execute(
            f"SELECT * FROM {orders_table} WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (int(user_id),),
        ).fetchall()
        orders = []
        for row in raw_orders:
            item = service._order_payload(row)
            if item.get("order_uuid") in bot_order_map:
                item["bot_name"] = bot_order_map[item["order_uuid"]]
            orders.append(item)
        fill_rows = conn.execute("SELECT * FROM trading_fills WHERE user_id=? ORDER BY id DESC LIMIT 50", (int(user_id),)).fetchall()
        pnl_by_fill = {
            row["fill_id"]: row
            for row in conn.execute(
                """
                SELECT *
                FROM trading_spot_realized_pnl
                WHERE user_id=? AND fill_id IN (
                    SELECT id FROM trading_fills WHERE user_id=? ORDER BY id DESC LIMIT 50
                )
                """,
                (int(user_id), int(user_id)),
            ).fetchall()
        }
        fill_order_uuid_map = {
            row["id"]: row["order_uuid"]
            for row in conn.execute(
                f"SELECT f.id, o.order_uuid FROM trading_fills f JOIN {orders_table} o ON o.id=f.order_id WHERE f.user_id=? ORDER BY f.id DESC LIMIT 50",
                (int(user_id),),
            ).fetchall()
        }
        fills = []
        for row in fill_rows:
            item = service._fill_payload(row, realized=pnl_by_fill.get(row["id"]))
            order_uuid = fill_order_uuid_map.get(row["id"])
            if order_uuid:
                item["order_uuid"] = order_uuid
            if order_uuid and order_uuid in bot_order_map:
                item["bot_name"] = bot_order_map[order_uuid]
            fills.append(item)
        margin_trade_records_items = service._margin_trade_records(conn, user_id)
        combined_fills = sorted(
            [*fills, *margin_trade_records_items],
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )[:50]
        market_prices = {
            m["symbol"]: float(_to_decimal(m.get("manual_price_points") or 0, name="manual_price_points", minimum=0))
            for m in markets
        }
        bots = []
        for row in conn.execute("SELECT * FROM trading_bots WHERE user_id=? ORDER BY id DESC LIMIT 50", (int(user_id),)).fetchall():
            bot = service._bot_payload_with_budget_meta(conn, row)
            current_price = market_prices.get(str(bot.get("market_symbol") or ""), 0)
            try:
                bot["condition_checks"] = service._bot_condition_checks(bot, current_price)
            except Exception:
                bot["condition_checks"] = []
            bots.append(bot)
        bot_runs = [
            service._bot_run_payload(row)
            for row in conn.execute("SELECT * FROM trading_bot_runs WHERE user_id=? ORDER BY id DESC LIMIT 50", (int(user_id),)).fetchall()
        ]
        funding = None
        wallet_selection_warning = ""
        try:
            funding = service._funding_payload(conn, user_id, source_wallet_address=source_wallet_address)
        except ValueError as exc:
            if source_wallet_address:
                wallet_selection_warning = str(exc)
                funding = service._funding_payload(conn, user_id, source_wallet_address=None)
            else:
                raise
        conn.commit()
        if wallet_selection_warning and isinstance(funding, dict):
            funding["wallet_selection_warning"] = wallet_selection_warning
        return {
            "state": state,
            "settings": service._settings_payload(conn),
            "funding_pool": service._funding_pool_payload(conn),
            "funding": funding,
            "wallets": wallets,
            "volume_stats": dict(service._user_volume_stats(conn, user_id)),
            "markets": markets,
            "positions": positions,
            "spot_summary": service._spot_summary_payload(positions),
            "futures_positions": futures_positions,
            "margin_positions": margin_positions,
            "margin_summary": service._margin_summary_payload(conn, user_id, margin_positions),
            "orders": orders,
            "fills": combined_fills,
            "spot_fills": fills,
            "margin_trade_records": margin_trade_records_items,
            "bots": bots,
            "bot_runs": bot_runs,
        }
    finally:
        conn.close()


def root_report(service):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        state = service._state(conn)
        verification = state.get("verification") if isinstance(state.get("verification"), dict) else {}
        if not verification:
            verification = {
                "ok": not bool(state.get("safe_mode")),
                "errors": [],
                "checked_at": state.get("updated_at"),
                "source": "trading_state_snapshot",
                "stale": True,
            }
        else:
            verification = dict(verification)
            verification.setdefault("source", "trading_state_snapshot")
        reserve = service._reserve(conn)
        markets = [
            service._market_payload(row)
            for row in conn.execute("SELECT * FROM trading_markets ORDER BY sort_order ASC, symbol ASC").fetchall()
        ]
        reserve_events = [dict(row) for row in conn.execute("SELECT * FROM trading_reserve_pool_events ORDER BY id DESC LIMIT 50").fetchall()]
        audit_events = [dict(row) for row in conn.execute("SELECT * FROM trading_audit_events ORDER BY id DESC LIMIT 80").fetchall()]
        volume_summary = {
            "totals": dict(
                conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(total_notional_points), 0) AS total_notional_points,
                        COALESCE(SUM(spot_notional_points), 0) AS spot_notional_points,
                        COALESCE(SUM(margin_notional_points), 0) AS margin_notional_points,
                        COALESCE(SUM(total_fee_points), 0) AS total_fee_points,
                        COALESCE(SUM(total_trade_count), 0) AS total_trade_count
                    FROM trading_user_volume_stats
                    """
                ).fetchone()
            ),
            "top_users": [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT s.*, u.username
                    FROM trading_user_volume_stats s
                    JOIN users u ON u.id=s.user_id
                    ORDER BY s.total_notional_points DESC, s.user_id ASC
                    LIMIT 20
                    """
                ).fetchall()
            ],
        }
        return {
            "state": state,
            "settings": service._settings_payload(conn),
            "reserve_pool": dict(reserve),
            "funding_pool": service._funding_pool_payload(conn),
            "volume_summary": volume_summary,
            "markets": markets,
            "reserve_events": reserve_events,
            "audit_events": audit_events,
            "bot_audit_dashboard": service._bot_audit_dashboard_on_conn(conn, limit=80),
            "verification": verification,
        }
    finally:
        conn.close()
