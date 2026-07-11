"""Trading margin risk/account/liquidation helpers."""

import hashlib
import json
import math
import os
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from services.system.notifications import create_notification_if_enabled
from services.server_mode.routing import resolve_table
from services.trading.accounting.core import (
    fee_micropoints,
    notional_points,
    points_from_micropoints_ceil,
    quantity_to_units,
    units_to_quantity,
)
from services.trading.constants import ASSET_SCALE, POINT_MICRO_SCALE
from services.trading.notifications import (
    create_trading_user_notification,
    margin_near_liquidation_notification_payload,
    margin_price_jump_notification_payload,
)
from services.trading._clock import now_text as _now_text
from services.trading.validators import _to_decimal, _to_int
from services.trading.mode_gate import (
    assert_same_world,
    liquidation_settle_table,
    liquidation_target_table,
)


POSITION_MARGIN_LONG = "margin_long"
POSITION_MARGIN_SHORT = "short"
POSITION_MARGIN_SHORT_LEGACY = "margin_short"
IMPLICIT_IDEMPOTENCY_BUCKET_SECONDS = 60
IMPLICIT_IDEMPOTENCY_BOUNDARY_GRACE_SECONDS = 10


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


def _implicit_operation_keys(*, value_prefix, key_prefix, request_time=None):
    request_time = request_time or datetime.now()
    bucket = int(request_time.timestamp() // IMPLICIT_IDEMPOTENCY_BUCKET_SECONDS)
    current = _client_idempotency_key(f"{value_prefix}:{bucket}", prefix=key_prefix)
    previous = _client_idempotency_key(f"{value_prefix}:{bucket - 1}", prefix=key_prefix)
    return current, previous, request_time


def _operation_replay_response(
    conn,
    *,
    operation,
    operation_key,
    boundary_operation_key="",
    request_time=None,
):
    current = conn.execute(
        """
        SELECT response_json, created_at, updated_at
        FROM trading_operation_idempotency
        WHERE idempotency_key=? AND operation=?
        """,
        (operation_key, operation),
    ).fetchone()
    if current and current["response_json"]:
        return _json_loads(current["response_json"], {"ok": True})
    if not boundary_operation_key or request_time is None:
        return None
    boundary = conn.execute(
        """
        SELECT response_json, created_at, updated_at
        FROM trading_operation_idempotency
        WHERE idempotency_key=? AND operation=?
        """,
        (boundary_operation_key, operation),
    ).fetchone()
    if not boundary or not boundary["response_json"]:
        return None
    try:
        recorded_at = datetime.fromisoformat(str(boundary["updated_at"] or boundary["created_at"] or ""))
        age_seconds = (request_time - recorded_at).total_seconds()
    except (TypeError, ValueError):
        return None
    if 0 <= age_seconds <= IMPLICIT_IDEMPOTENCY_BOUNDARY_GRACE_SECONDS:
        return _json_loads(boundary["response_json"], {"ok": True})
    return None


def _normalize_optional_risk_percent(value, *, name):
    if value in (None, ""):
        return None
    number = float(_to_decimal(value, name=name, minimum=0.00000001, maximum=1000))
    return number if number > 0 else None


def _is_short_position(position_type):
    return str(position_type or "") in {POSITION_MARGIN_SHORT, POSITION_MARGIN_SHORT_LEGACY}


def margin_withdrawable_collateral_payload(*, collateral, unrealized_pnl, equity_after, maintenance_points):
    collateral = int(collateral or 0)
    unrealized_pnl = int(unrealized_pnl or 0)
    equity_after = int(equity_after or 0)
    maintenance_points = int(maintenance_points or 0)
    profitable_surplus = max(0, unrealized_pnl)
    collateral_cap = max(0, collateral - 1)
    maintenance_cap = max(0, equity_after - maintenance_points - 1)
    max_withdrawable = max(0, min(collateral_cap, profitable_surplus, maintenance_cap))
    if collateral_cap <= 0:
        reason = "collateral_floor"
    elif profitable_surplus <= 0:
        reason = "no_unrealized_profit"
    elif maintenance_cap <= 0:
        reason = "maintenance_buffer"
    else:
        reason = "estimated"
    after_ratio = (
        round(((equity_after - max_withdrawable) / maintenance_points) * 100, 2)
        if max_withdrawable > 0 and maintenance_points > 0
        else None
    )
    return {
        "max_withdrawable_collateral_points": max_withdrawable,
        "withdrawable_collateral_points": max_withdrawable,
        "withdrawable_collateral_reason": reason,
        "withdrawable_collateral_profit_cap_points": profitable_surplus,
        "withdrawable_collateral_collateral_cap_points": collateral_cap,
        "withdrawable_collateral_maintenance_cap_points": maintenance_cap,
        "withdrawable_collateral_after_ratio_percent": after_ratio,
    }


def margin_risk_payload(
    service,
    conn,
    position,
    market=None,
    *,
    now_text=None,
    price_override_points=None,
    price_source_override=None,
    strict_high_risk=True,
    allow_internal_price_override=False,
):
    market = market or service._market(conn, position["market_symbol"])
    if price_override_points is None:
        price, price_source, price_meta = service._current_market_price_points(
            conn, market, with_meta=True, high_risk=True
        )
    else:
        if not allow_internal_price_override:
            raise ValueError("internal price override is not allowed")
        price = float(
            _to_decimal(
                price_override_points,
                name="price_override_points",
                minimum=0.00000001,
            )
        )
        price_source = str(price_source_override or "scan_window_replay")
        price_meta = {
            "price_health": "override_replay",
            "fallback_reason": "",
            "excluded_sources": [],
            "warnings": ["internal_price_override"],
            "high_risk_blocked": False,
            "high_risk_block_reason": "internal replay override",
            "requested_price_mode": "risk_grade",
            "reference_price_points": price,
            "risk_grade_price_points": price,
            "resolved_source": price_source,
            "reference_provider_count": 0,
            "risk_grade_provider_count": 0,
            "stale": False,
            "degraded": False,
            "override_price": True,
        }
    price_confidence_override = str(os.environ.get("HACKME_DEV_TRADING_DISABLE_PRICE_CONFIDENCE_GATES") or "").strip().lower() in {"1", "true", "yes", "on"}
    if not price_confidence_override:
        row = conn.execute(
            "SELECT value FROM trading_settings WHERE key IN (?, ?) ORDER BY CASE key WHEN ? THEN 0 ELSE 1 END LIMIT 1",
            (
                "trading.disable_price_confidence_gates",
                "trading.dev_disable_price_confidence_gates",
                "trading.disable_price_confidence_gates",
            ),
        ).fetchone()
        price_confidence_override = str(row["value"] if row else "").strip().lower() in {"1", "true", "yes", "on"}
    if strict_high_risk and bool(price_meta.get("high_risk_blocked")) and not price_confidence_override:
        raise ValueError(
            str(price_meta.get("high_risk_block_reason") or "risk-grade price unavailable")
        )
    quantity_units = int(position["quantity_units"])
    exit_notional = notional_points(quantity_units, price)
    fee_rate_percent = float(market["fee_rate_percent"] or 0)
    open_fee_micro = int(position["open_fee_micropoints"] or 0) if "open_fee_micropoints" in position.keys() else 0
    close_fee_micro = fee_micropoints(exit_notional, fee_rate_percent)
    settlement_fee_micro = open_fee_micro + close_fee_micro
    close_fee = points_from_micropoints_ceil(settlement_fee_micro)
    interest = service._margin_interest_points(position, now_text=now_text)
    collateral = int(position["collateral_points"] or 0)
    principal = int(position["principal_points"] or 0)
    entry_price = float(
        _to_decimal(
            position["entry_price_points"] or price,
            name="entry_price_points",
            minimum=0.00000001,
        )
    )
    entry_notional = notional_points(quantity_units, entry_price)
    initial_margin_percent = (
        round((collateral * 100.0) / entry_notional, 4) if entry_notional > 0 else 0.0
    )
    if position["position_type"] == POSITION_MARGIN_LONG:
        equity_after = exit_notional - principal - interest - close_fee
        delta = equity_after - collateral
    else:
        delta = principal - exit_notional - interest - close_fee
        equity_after = collateral + delta
    settings = service._settings_payload(conn)
    maintenance_percent = float(settings.get("margin_maintenance_percent") or 0)
    maintenance_points = int(math.ceil(exit_notional * maintenance_percent / 100.0))
    fee_rate_decimal = Decimal(str(fee_rate_percent)) / Decimal("100")
    open_fee_exact = Decimal(open_fee_micro) / Decimal(POINT_MICRO_SCALE)
    break_even_price_points = None
    quantity_decimal = Decimal(quantity_units)
    if quantity_units > 0:
        if position["position_type"] == POSITION_MARGIN_LONG:
            required_exit_value = (
                Decimal(collateral + principal + int(position["open_fee_points"] or 0))
                + open_fee_exact
                + Decimal(str(interest))
            )
            denominator = Decimal("1") - fee_rate_decimal
            if denominator > 0:
                break_even_price_points = float(
                    (
                        required_exit_value
                        * Decimal(ASSET_SCALE)
                        / (quantity_decimal * denominator)
                    ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
                )
        else:
            recoverable_value = (
                Decimal(principal - int(position["open_fee_points"] or 0))
                - open_fee_exact
                - Decimal(str(interest))
            )
            denominator = Decimal("1") + fee_rate_decimal
            if recoverable_value > 0 and denominator > 0:
                break_even_price_points = float(
                    (
                        recoverable_value
                        * Decimal(ASSET_SCALE)
                        / (quantity_decimal * denominator)
                    ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
                )
    denominator_percent = None
    liquidation_notional = None
    if position["position_type"] == POSITION_MARGIN_LONG:
        denominator_percent = 100.0 - fee_rate_percent - maintenance_percent
        if denominator_percent > 0:
            liquidation_notional = int(
                math.ceil((principal + interest) * 100.0 / denominator_percent)
            )
    else:
        denominator_percent = 100.0 + fee_rate_percent + maintenance_percent
        liquidation_base = collateral + principal - interest
        if denominator_percent > 0 and liquidation_base > 0:
            liquidation_notional = int(
                math.ceil(liquidation_base * 100.0 / denominator_percent)
            )
    liquidation_price_points = None
    if liquidation_notional is not None and quantity_units > 0:
        liquidation_price_points = float(
            (
                Decimal(liquidation_notional)
                * Decimal(ASSET_SCALE)
                / Decimal(quantity_units)
            ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        )
    maintenance_ratio_percent = (
        round((equity_after * 100.0) / maintenance_points, 2)
        if maintenance_points > 0
        else 0.0
    )
    if equity_after <= maintenance_points:
        risk_status = "liquidation"
        risk_reason = "權益已低於維持保證金，會被列入強制平倉"
    elif maintenance_ratio_percent < 150.0:
        risk_status = "warning"
        risk_reason = "整體維持率偏低，建議補保證金或降低倉位"
    elif _is_short_position(position["position_type"]):
        risk_status = "short_price_risk"
        risk_reason = "借券放空在價格上漲時會虧損，價格越高維持率越低"
    else:
        risk_status = "normal"
        risk_reason = "融資做多在價格下跌時會虧損，價格越低維持率越低"
    withdrawable = margin_withdrawable_collateral_payload(
        collateral=collateral,
        unrealized_pnl=delta,
        equity_after=equity_after,
        maintenance_points=maintenance_points,
    )
    return {
        "price_points": price,
        "price_source": price_source,
        "price_context": service._build_price_context(
            market_symbol=position["market_symbol"],
            price_type="risk_grade",
            price_points=price,
            price_source=price_source,
            price_meta=price_meta,
        ),
        "exit_notional_points": exit_notional,
        "close_fee_points": close_fee,
        "open_fee_micropoints": open_fee_micro,
        "close_fee_micropoints": close_fee_micro,
        "settlement_fee_micropoints": settlement_fee_micro,
        "interest_points": interest,
        "collateral_points": collateral,
        "initial_margin_points": collateral,
        "original_margin_points": collateral,
        "initial_margin_percent": initial_margin_percent,
        "entry_notional_points": entry_notional,
        "principal_points": principal,
        "delta_points": delta,
        "unrealized_pnl_points": delta,
        "breakeven_price_points": break_even_price_points,
        "equity_after_points": equity_after,
        "maintenance_percent": maintenance_percent,
        "maintenance_points": maintenance_points,
        "maintenance_margin_percent": maintenance_percent,
        "maintenance_margin_points": maintenance_points,
        "liquidation_notional_points": liquidation_notional,
        "liquidation_price_points": liquidation_price_points,
        "maintenance_ratio_percent": maintenance_ratio_percent,
        "risk_status": risk_status,
        "risk_reason": risk_reason,
        "liquidation_required": equity_after <= maintenance_points,
        **withdrawable,
    }


def margin_position_payload_with_risk(
    service,
    conn,
    row,
    *,
    market=None,
    risk_overrides=None,
    strict_risk=False,
    allow_internal_price_override=False,
):
    item = service._margin_position_payload(row)
    try:
        risk = margin_risk_payload(
            service,
            conn,
            row,
            market=market,
            allow_internal_price_override=allow_internal_price_override,
            **(risk_overrides or {}),
        )
    except Exception as exc:
        if strict_risk:
            raise
        risk = {
            "risk_status": "unavailable",
            "risk_reason": f"風險資料暫時無法計算：{str(exc)[:160]}",
            "liquidation_required": False,
        }
    item["risk"] = risk
    item["maintenance_ratio_percent"] = risk.get("maintenance_ratio_percent")
    item["risk_status"] = risk.get("risk_status")
    item["risk_reason"] = risk.get("risk_reason")
    item["equity_after_points"] = risk.get("equity_after_points")
    item["maintenance_points"] = risk.get("maintenance_points")
    item["maintenance_margin_points"] = risk.get("maintenance_margin_points")
    item["maintenance_margin_percent"] = risk.get("maintenance_margin_percent")
    item["initial_margin_points"] = risk.get("initial_margin_points")
    item["original_margin_points"] = risk.get("original_margin_points")
    item["initial_margin_percent"] = risk.get("initial_margin_percent")
    item["entry_notional_points"] = risk.get("entry_notional_points")
    item["current_price_points"] = risk.get("price_points")
    item["unrealized_pnl_points"] = risk.get("unrealized_pnl_points")
    item["max_withdrawable_collateral_points"] = risk.get("max_withdrawable_collateral_points")
    item["withdrawable_collateral_reason"] = risk.get("withdrawable_collateral_reason")
    item["withdrawable_collateral_after_ratio_percent"] = risk.get("withdrawable_collateral_after_ratio_percent")
    item["breakeven_price_points"] = risk.get("breakeven_price_points")
    item["liquidation_price_points"] = risk.get("liquidation_price_points")
    return item


def accrue_margin_interest(service, conn, position, *, actor=None, now_text=None, ctx=None):
    if not position or position["status"] != "open":
        return position
    if service._is_root_user_id(conn, int(position["user_id"])):
        return position
    margin_positions_table, route_ctx = service._resolve_table("margin_positions", ctx, action="margin_interest")
    total_hours = service._margin_interest_total_hours(position, now_text=now_text)
    accrued_hours = int(position["interest_accrued_hours"] or 0) if "interest_accrued_hours" in position.keys() else 0
    due_hours = max(0, total_hours - accrued_hours)
    if due_hours <= 0:
        return position
    carry = int(position["interest_carry_micropoints"] or 0) if "interest_carry_micropoints" in position.keys() else 0
    due_micro = service._margin_interest_due_micropoints(
        principal=int(position["principal_points"] or 0),
        rate_percent=float(position["interest_percent_daily"] or 0),
        hours=due_hours,
    )
    total_micro = carry + due_micro
    user_id = int(position["user_id"])
    now = _now_text()
    conn.execute(
        f"""
        UPDATE {margin_positions_table}
        SET interest_accrued_hours=?,
            interest_carry_micropoints=?,
            updated_at=?
        WHERE id=?
        """,
        (total_hours, total_micro, now, position["id"]),
    )
    service._audit_event(
        conn,
        "TRADING_MARGIN_INTEREST_ACCRUED",
        "margin borrow interest accrued as exact micropoints",
        actor=actor,
        target_user_id=user_id,
        market_symbol=position["market_symbol"],
        severity="info",
        metadata={
            "position_uuid": position["position_uuid"],
            "due_micropoints": due_micro,
            "total_unsettled_micropoints": total_micro,
            "estimated_settlement_points": points_from_micropoints_ceil(total_micro),
            "charged_hours": due_hours,
            "total_accrued_hours": total_hours,
            "settlement_policy": "ceil only on margin close or liquidation",
        },
    )
    return conn.execute(f"SELECT * FROM {margin_positions_table} WHERE id=?", (position["id"],)).fetchone()


def margin_free_margin_points(service, conn, user_id):
    user_id = int(user_id)
    if service._is_root_user_id(conn, user_id):
        account = service._root_sim_account(conn, user_id)
        return int(account["balance_points"] or 0)
    wallet_payload = service._wallet_payload(conn, user_id)
    wallet_available = int(wallet_payload.get("points_balance") or 0)
    trial = service._trial_credit_row(conn, user_id)
    trial_available = (
        int(trial["available_points"] or 0)
        if trial and trial["status"] == "active"
        else 0
    )
    return max(0, wallet_available + trial_available)


def margin_account_payload(service, conn, user_id, rows=None):
    user_id = int(user_id)
    if rows is None:
        rows = [
            margin_position_payload_with_risk(service, conn, row)
            for row in conn.execute(
                "SELECT * FROM trading_margin_positions WHERE user_id=? AND status='open' ORDER BY id ASC",
                (user_id,),
            ).fetchall()
        ]
    active = [row for row in rows if row.get("status") == "open"]
    total_position_equity = 0
    total_maintenance = 0
    total_borrowed = 0
    total_unrealized = 0
    warning_count = 0
    unavailable_count = 0
    for row in active:
        risk = row.get("risk") if isinstance(row.get("risk"), dict) else {}
        total_position_equity += int(
            risk.get("equity_after_points") or row.get("equity_after_points") or 0
        )
        total_maintenance += int(
            risk.get("maintenance_points") or row.get("maintenance_points") or 0
        )
        total_borrowed += int(row.get("principal_points") or 0)
        total_unrealized += int(
            risk.get("unrealized_pnl_points") or row.get("unrealized_pnl_points") or 0
        )
        status = str(risk.get("risk_status") or row.get("risk_status") or "")
        if status == "unavailable":
            unavailable_count += 1
        elif status == "warning":
            warning_count += 1
    free_margin = margin_free_margin_points(service, conn, user_id) if active else 0
    account_equity = total_position_equity + free_margin
    available_margin = account_equity - total_maintenance
    ratio = (
        round((account_equity / total_maintenance) * 100, 2)
        if total_maintenance > 0
        else None
    )
    liquidation_required = bool(
        active and total_maintenance > 0 and account_equity <= total_maintenance
    )
    if liquidation_required:
        status = "liquidation"
        reason = "整戶權益已低於總維持保證金，會依風險順序強制平倉"
    elif unavailable_count:
        status = "unavailable"
        reason = f"{unavailable_count} 筆倉位風險資料無法計算"
    elif active and ratio is not None and ratio < 150.0:
        status = "warning"
        reason = "整戶維持率偏低，建議補保證金、平倉或降低倉位"
    elif warning_count:
        status = "warning"
        reason = f"{warning_count} 筆倉位接近風險區"
    elif active:
        status = "normal"
        reason = "整戶維持率正常"
    else:
        status = "none"
        reason = "目前沒有借貸倉位"
    return {
        "mode": "cross_margin",
        "user_id": user_id,
        "open_count": len(active),
        "account_equity_points": account_equity,
        "total_position_equity_points": total_position_equity,
        "free_margin_points": free_margin,
        "available_margin_points": available_margin,
        "total_borrowed_points": total_borrowed,
        "total_maintenance_requirement_points": total_maintenance,
        "total_maintenance_points": total_maintenance,
        "total_unrealized_pnl_points": total_unrealized,
        "cross_margin_ratio_percent": ratio,
        "maintenance_ratio_percent": ratio,
        "liquidation_required": liquidation_required,
        "liquidation_count": 1 if liquidation_required else 0,
        "warning_count": warning_count + unavailable_count,
        "status": status,
        "reason": reason,
        "auto_transfer_rule": "available wallet/trial/root-simulated balance is counted as free cross margin during risk checks",
    }


def margin_summary_payload(service, conn, user_id, rows):
    return margin_account_payload(service, conn, user_id, rows)


def margin_liquidation_order_key(row):
    risk = row.get("risk") if isinstance(row.get("risk"), dict) else {}
    equity = int(risk.get("equity_after_points") or row.get("equity_after_points") or 0)
    maintenance = int(risk.get("maintenance_points") or row.get("maintenance_points") or 0)
    deficit = equity - maintenance
    ratio = risk.get("maintenance_ratio_percent")
    try:
        ratio_value = float(ratio)
    except Exception:
        ratio_value = -999999.0
    return (deficit, ratio_value, -int(row.get("principal_points") or 0), int(row.get("id") or 0))


def margin_summary_payload_legacy(rows):
    active = [row for row in rows if row.get("status") == "open"]
    total_equity = 0
    total_maintenance = 0
    liquidation_count = 0
    warning_count = 0
    for row in active:
        risk = row.get("risk") if isinstance(row.get("risk"), dict) else {}
        total_equity += int(risk.get("equity_after_points") or 0)
        total_maintenance += int(risk.get("maintenance_points") or 0)
        if risk.get("liquidation_required"):
            liquidation_count += 1
        elif str(risk.get("risk_status") or "") in {"warning", "unavailable"}:
            warning_count += 1
    ratio = round((total_equity / total_maintenance) * 100, 2) if total_maintenance > 0 else None
    if liquidation_count:
        status = "liquidation"
        reason = f"{liquidation_count} 筆倉位低於維持保證金"
    elif warning_count:
        status = "warning"
        reason = f"{warning_count} 筆倉位需要注意"
    elif active:
        status = "normal"
        reason = "整戶維持率正常"
    else:
        status = "none"
        reason = "目前沒有借貸倉位"
    return {
        "open_count": len(active),
        "total_equity_after_points": total_equity,
        "total_maintenance_points": total_maintenance,
        "maintenance_ratio_percent": ratio,
        "status": status,
        "reason": reason,
        "liquidation_count": liquidation_count,
        "warning_count": warning_count,
    }


def notify_margin_risk_alerts(service, conn, *, position, risk, market):
    try:
        user_id = int(position["user_id"])
        position_uuid = str(position["position_uuid"])
        market_symbol = str(position["market_symbol"])
        position_label = "融資做多" if position["position_type"] == "margin_long" else "借券放空"
        price = float(_to_decimal(risk.get("price_points") or 0, name="price_points", minimum=0))
        entry_price = float(
            _to_decimal(position["entry_price_points"] or 0, name="entry_price_points", minimum=0)
        )
        ratio = risk.get("maintenance_ratio_percent")
        liquidation_price = risk.get("liquidation_price_points")
        if not risk.get("liquidation_required") and ratio is not None and float(ratio) <= 150.0:
            alert_type = "trading_margin_near_liquidation"
            if not service._has_unread_margin_alert(
                conn, user_id=user_id, alert_type=alert_type, position_uuid=position_uuid
            ):
                notice = margin_near_liquidation_notification_payload(
                    market_symbol=market_symbol,
                    position_label=position_label,
                    price=price,
                    liquidation_price=liquidation_price,
                    ratio=ratio,
                    position_uuid=position_uuid,
                )
                create_trading_user_notification(
                    conn,
                    user_id=user_id,
                    notification_type=notice["notification_type"],
                    title=notice["title"],
                    body=notice["body"],
                    create_notification=create_notification_if_enabled,
                )
        if entry_price > 0 and price > 0:
            move_percent = abs(price - entry_price) * 100.0 / entry_price
            threshold = float(market["max_price_jump_percent"] or 10)
            if move_percent >= threshold:
                alert_type = "trading_margin_price_jump"
                if not service._has_unread_margin_alert(
                    conn, user_id=user_id, alert_type=alert_type, position_uuid=position_uuid
                ):
                    direction = "上漲" if price > entry_price else "下跌"
                    notice = margin_price_jump_notification_payload(
                        market_symbol=market_symbol,
                        position_label=position_label,
                        direction=direction,
                        move_percent=move_percent,
                        entry_price=entry_price,
                        price=price,
                        position_uuid=position_uuid,
                    )
                    create_trading_user_notification(
                        conn,
                        user_id=user_id,
                        notification_type=notice["notification_type"],
                        title=notice["title"],
                        body=notice["body"],
                        create_notification=create_notification_if_enabled,
                    )
    except Exception as exc:
        service._audit_event(
            conn,
            "TRADING_MARGIN_RISK_NOTIFY_FAILED",
            "margin risk notification failed",
            actor={"username": "system", "role": "system"},
            target_user_id=int(position["user_id"]),
            market_symbol=str(position["market_symbol"]),
            severity="warning",
            metadata={
                "position_uuid": str(position["position_uuid"]),
                "risk_status": str((risk or {}).get("risk_status") or ""),
                "error": str(exc)[:200],
            },
        )


def open_margin_position(
    service,
    *,
    actor,
    market_symbol,
    position_type,
    quantity,
    collateral_points,
    stop_loss_percent=None,
    take_profit_percent=None,
    idempotency_key=None,
    ctx=None,
):
    ctx = service._resolve_trading_ctx(ctx, action="open_margin_position")
    user_id = service._actor_id(actor)
    if not user_id:
        raise ValueError("login required")
    position_type = str(position_type or "").strip().lower()
    if position_type not in {POSITION_MARGIN_LONG, POSITION_MARGIN_SHORT}:
        raise ValueError("position_type must be margin_long or short")
    quantity_units = quantity_to_units(quantity)
    collateral = _to_int(collateral_points, name="collateral_points", minimum=1, maximum=10**12)
    stop_loss_percent = _normalize_optional_risk_percent(stop_loss_percent, name="stop_loss_percent")
    take_profit_percent = _normalize_optional_risk_percent(take_profit_percent, name="take_profit_percent")
    operation_key = _client_idempotency_key(idempotency_key, prefix=f"margin_open:{user_id}")
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        service._ensure_market_price_snapshot_for_write(market_symbol, high_risk=True)
        conn.execute("BEGIN IMMEDIATE")
        service._assert_writable(conn)
        margin_positions_table, route_ctx = service._resolve_table(
            "margin_positions", ctx, action="open_margin_position"
        )
        if operation_key:
            existing_operation = conn.execute(
                """
                SELECT response_json FROM trading_operation_idempotency
                WHERE idempotency_key=? AND operation='margin_open'
                """,
                (operation_key,),
            ).fetchone()
            if existing_operation and existing_operation["response_json"]:
                result = _json_loads(existing_operation["response_json"], {"ok": True})
                conn.rollback()
                return result
            now_text = _now_text()
            insert_cur = conn.execute(
                """
                INSERT OR IGNORE INTO trading_operation_idempotency (
                    idempotency_key, operation, user_id, reference_uuid, response_json, created_at, updated_at
                ) VALUES (?, 'margin_open', ?, '', '', ?, ?)
                """,
                (operation_key, int(user_id), now_text, now_text),
            )
            if insert_cur.rowcount == 0:
                existing_operation = conn.execute(
                    "SELECT response_json FROM trading_operation_idempotency WHERE idempotency_key=?",
                    (operation_key,),
                ).fetchone()
                if existing_operation and existing_operation["response_json"]:
                    result = _json_loads(existing_operation["response_json"], {"ok": True})
                    conn.rollback()
                    return result
                raise ValueError("duplicate margin open request is still processing")
        borrow_settings = service._assert_borrowing_enabled(conn)
        market = service._market(conn, market_symbol)
        service._assert_market_boot_ready(market, usage="margin position open", conn=conn)
        if not int(market.get("allow_margin") or 0):
            raise ValueError("margin trading is disabled for this market")
        service._validate_market_quantity_constraints(market, quantity_units)
        price, price_source, price_meta = service._snapshot_market_price_points(
            conn, market, with_meta=True, high_risk=True
        )
        service._assert_price_meta_allows_high_risk_use(
            conn,
            actor=actor,
            market_symbol=market["symbol"],
            usage="margin financing risk evaluation",
            price_meta=price_meta,
        )
        notional = notional_points(quantity_units, price)
        min_collateral = service._minimum_margin_collateral_points(
            conn,
            position_type=position_type,
            notional=notional,
            fee_rate_percent=float(market["fee_rate_percent"] or 0),
        )
        if collateral < min_collateral:
            raise ValueError(f"collateral below minimum {min_collateral}")
        fee_micro = fee_micropoints(notional, float(market["fee_rate_percent"] or 0))
        estimated_fee = points_from_micropoints_ceil(fee_micro)
        fee = 0
        if position_type == POSITION_MARGIN_LONG and collateral >= notional:
            raise ValueError(f"collateral must be lower than notional {notional} for margin long")
        principal = max(0, notional - collateral) if position_type == POSITION_MARGIN_LONG else notional
        is_root_simulated = service._is_root_actor(actor)
        if not is_root_simulated:
            wallet_payload = service._wallet_payload(conn, user_id, ctx=route_ctx)
            trial = service._ensure_trial_credit(conn, user_id)
            available = int(wallet_payload.get("points_balance") or 0)
            if trial:
                available += int(trial["available_points"] or 0)
            upfront_required = collateral
            if upfront_required > available:
                max_collateral = max(0, available)
                raise ValueError(
                    "margin open funds insufficient "
                    f"required={upfront_required} available={available} "
                    f"collateral={collateral} estimated_fee_on_close={estimated_fee} max_collateral={max_collateral}"
                )
        borrowed_asset_symbol = service._margin_borrowed_asset_symbol(market, position_type)
        interest_interval_hours = int(borrow_settings["interest_interval_hours"] or 0)
        interest_minimum_hours = int(borrow_settings["interest_minimum_hours"] or 0)
        funding_pool = service._funding_pool_payload(
            conn, requested_principal=principal, borrowed_asset=borrowed_asset_symbol
        )
        if (
            not service._is_root_actor(actor)
            and principal > 0
            and bool(funding_pool.get("projected_over_utilization_limit"))
        ):
            raise ValueError(
                "funding pool is insufficient: borrow amount exceeds funding pool utilization limit "
                f"{funding_pool.get('max_pool_utilization_percent')}%"
            )
        if not service._is_root_actor(actor) and principal > int(funding_pool["available_points"] or 0):
            raise ValueError("funding pool is insufficient for requested borrow amount")
        effective_interest_percent_daily = float(
            funding_pool["projected_interest_percent_daily"]
            if principal
            else funding_pool["effective_interest_percent_daily"]
        )
        position_uuid = str(uuid.uuid4())
        ledger_uuids = []
        if is_root_simulated:
            service._sim_delta(conn, user_id, balance_delta=-collateral, locked_delta=collateral)
            trial_fee = 0
            chain_fee = 0
            trial_collateral = 0
            chain_collateral = 0
        else:
            trial_fee = 0
            chain_fee = 0
            trial_collateral = service._trial_deploy(conn, user_id, collateral)
            chain_collateral = collateral - trial_collateral
        if chain_collateral:
            ledger_uuids.append(
                service._ledger(
                    conn,
                    user_id=user_id,
                    currency_type="points",
                    direction="freeze",
                    amount=chain_collateral,
                    action_type="trading_margin_collateral_freeze",
                    reference_type="trading_margin_position",
                    reference_id=position_uuid,
                    idempotency_key=f"trading:margin:collateral:{position_uuid}",
                    reason="TRADING_MARGIN_COLLATERAL",
                    public_metadata={
                        "position_type": position_type,
                        "market": market["symbol"],
                        "quantity": units_to_quantity(quantity_units),
                        "notional": notional,
                        "trial_collateral_points": trial_collateral,
                        "chain_collateral_points": chain_collateral,
                    },
                    actor=actor,
                    ctx=route_ctx,
                )["ledger_uuid"]
            )
        if principal and not is_root_simulated:
            service._reserve_delta(
                conn,
                delta=-principal,
                event_type="margin_principal_lent",
                reason="TRADING_MARGIN_PRINCIPAL_LENT",
                actor=actor,
                source_user_id=user_id,
            )
        now = _now_text()
        if margin_positions_table == "test_shadow_margin_positions":
            cur = conn.execute(
                f"""
                INSERT INTO {margin_positions_table} (
                    position_uuid, tester_user_id, user_id, market_symbol, position_type, quantity_units,
                    entry_price_points, principal_points, collateral_points, open_fee_points,
                    open_fee_micropoints,
                    stop_loss_percent, take_profit_percent,
                    interest_percent_daily, interest_paid_points, interest_accrued_hours, interest_interval_hours,
                    interest_minimum_hours, borrowed_asset_symbol, status, opened_at, updated_at,
                    collateral_trial_points, collateral_chain_points, open_fee_trial_points, open_fee_chain_points
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_uuid,
                    service._shadow_actor_user_id(route_ctx, user_id),
                    user_id,
                    market["symbol"],
                    position_type,
                    quantity_units,
                    price,
                    principal,
                    collateral,
                    fee,
                    fee_micro,
                    stop_loss_percent,
                    take_profit_percent,
                    effective_interest_percent_daily,
                    interest_interval_hours,
                    interest_minimum_hours,
                    borrowed_asset_symbol,
                    now,
                    now,
                    trial_collateral,
                    chain_collateral,
                    trial_fee,
                    chain_fee,
                ),
            )
        else:
            cur = conn.execute(
                f"""
                INSERT INTO {margin_positions_table} (
                    position_uuid, user_id, market_symbol, position_type, quantity_units,
                    entry_price_points, principal_points, collateral_points, open_fee_points,
                    open_fee_micropoints,
                    stop_loss_percent, take_profit_percent,
                    interest_percent_daily, interest_paid_points, interest_accrued_hours, interest_interval_hours,
                    interest_minimum_hours, borrowed_asset_symbol, status, opened_at, updated_at,
                    collateral_trial_points, collateral_chain_points, open_fee_trial_points, open_fee_chain_points
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_uuid,
                    user_id,
                    market["symbol"],
                    position_type,
                    quantity_units,
                    price,
                    principal,
                    collateral,
                    fee,
                    fee_micro,
                    stop_loss_percent,
                    take_profit_percent,
                    effective_interest_percent_daily,
                    interest_interval_hours,
                    interest_minimum_hours,
                    borrowed_asset_symbol,
                    now,
                    now,
                    trial_collateral,
                    chain_collateral,
                    trial_fee,
                    chain_fee,
                ),
            )
        service._audit_event(
            conn,
            "TRADING_MARGIN_POSITION_OPENED",
            "margin borrow position opened",
            actor=actor,
            target_user_id=user_id,
            market_symbol=market["symbol"],
            metadata={
                "position_id": cur.lastrowid,
                "position_uuid": position_uuid,
                "position_type": position_type,
                "quantity": units_to_quantity(quantity_units),
                "entry_price_points": price,
                "price_source": price_source,
                "principal_points": principal,
                "funding_pool_available_before": funding_pool["available_points"],
                "funding_pool_projected_utilization_percent": funding_pool["projected_utilization_percent"],
                "borrowed_asset_symbol": borrowed_asset_symbol,
                "base_interest_apr_percent": funding_pool["base_interest_apr_percent"],
                "effective_interest_apr_percent": funding_pool["projected_interest_apr_percent"]
                if principal
                else funding_pool["effective_interest_apr_percent"],
                "base_interest_percent_daily": funding_pool["base_interest_percent_daily"],
                "effective_interest_percent_daily": effective_interest_percent_daily,
                "interest_interval_hours": interest_interval_hours,
                "interest_minimum_hours": interest_minimum_hours,
                "collateral_points": collateral,
                "open_fee_points": fee,
                "open_fee_micropoints": fee_micro,
                "estimated_open_fee_points": estimated_fee,
                "fee_settlement_policy": "ceil on margin close or liquidation",
                "stop_loss_percent": stop_loss_percent,
                "take_profit_percent": take_profit_percent,
                "trial_collateral_points": trial_collateral,
                "chain_collateral_points": chain_collateral,
                "trial_fee_points": trial_fee,
                "chain_fee_points": chain_fee,
                "funding_mode": "root_simulated"
                if is_root_simulated
                else ("trial_mixed" if (trial_collateral or trial_fee) else "points_chain"),
                "ledger_uuids": ledger_uuids,
            },
        )
        if not is_root_simulated:
            service._record_user_trade_volume(
                conn,
                user_id=user_id,
                trade_kind="margin",
                notional_points=notional,
                fee_points=fee,
                occurred_at=now,
            )
        conn.commit()
        row = conn.execute(
            f"SELECT * FROM {margin_positions_table} WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
        result = {
            "ok": True,
            "position": service._margin_position_payload(row),
            "funding": service._funding_payload(conn, user_id),
        }
        if operation_key:
            conn.execute(
                """
                UPDATE trading_operation_idempotency
                SET reference_uuid=?, response_json=?, updated_at=?
                WHERE idempotency_key=?
                """,
                (position_uuid, _json_dumps(result), _now_text(), operation_key),
            )
            conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_margin_collateral(
    service,
    *,
    actor,
    position_uuid,
    amount_points,
    idempotency_key=None,
    ctx=None,
):
    ctx = service._resolve_trading_ctx(ctx, action="add_margin_collateral")
    actor_user_id = service._actor_id(actor)
    if not actor_user_id:
        raise ValueError("login required")
    amount = _to_int(amount_points, name="collateral_points", minimum=1, maximum=10**12)
    request_time = datetime.now()
    boundary_operation_key = ""
    key_prefix = f"margin_collateral_add:{actor_user_id}:{position_uuid}"
    if idempotency_key:
        operation_key = _client_idempotency_key(idempotency_key, prefix=key_prefix)
    else:
        operation_key, boundary_operation_key, request_time = _implicit_operation_keys(
            value_prefix=f"{position_uuid}:{amount}",
            key_prefix=key_prefix,
            request_time=request_time,
        )
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        service._assert_writable(conn)
        if operation_key:
            replay = _operation_replay_response(
                conn,
                operation="margin_collateral_add",
                operation_key=operation_key,
                boundary_operation_key=boundary_operation_key,
                request_time=request_time,
            )
            if replay is not None:
                conn.rollback()
                return replay
            now_text = _now_text()
            insert_cur = conn.execute(
                """
                INSERT OR IGNORE INTO trading_operation_idempotency (
                    idempotency_key, operation, user_id, reference_uuid, response_json, created_at, updated_at
                ) VALUES (?, 'margin_collateral_add', ?, ?, '', ?, ?)
                """,
                (operation_key, int(actor_user_id), str(position_uuid or ""), now_text, now_text),
            )
            if insert_cur.rowcount == 0:
                existing_operation = conn.execute(
                    "SELECT response_json FROM trading_operation_idempotency WHERE idempotency_key=?",
                    (operation_key,),
                ).fetchone()
                if existing_operation and existing_operation["response_json"]:
                    result = _json_loads(existing_operation["response_json"], {"ok": True})
                    conn.rollback()
                    return result
                raise ValueError("duplicate margin collateral request is still processing")
        position = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE position_uuid=?",
            (str(position_uuid or ""),),
        ).fetchone()
        if not position:
            raise ValueError("margin position not found")
        user_id = int(position["user_id"])
        if int(user_id) != int(actor_user_id):
            raise ValueError("cannot update another user's margin position")
        if position["status"] != "open":
            raise ValueError("margin position is not open")
        is_root_simulated = service._is_root_user_id(conn, user_id)
        ledger_uuids = []
        trial_added = 0
        chain_added = 0
        if is_root_simulated:
            service._sim_delta(conn, user_id, balance_delta=-amount, locked_delta=amount)
        else:
            trial_added = service._trial_deploy(conn, user_id, amount)
            chain_added = amount - trial_added
            if chain_added:
                ledger_uuids.append(
                    service._ledger(
                        conn,
                        user_id=user_id,
                        currency_type="points",
                        direction="freeze",
                        amount=chain_added,
                        action_type="trading_margin_collateral_freeze",
                        reference_type="trading_margin_position",
                        reference_id=position["position_uuid"],
                        idempotency_key=f"trading:margin:collateral_add:{operation_key}",
                        reason="TRADING_MARGIN_COLLATERAL_ADD",
                        public_metadata={
                            "position_type": position["position_type"],
                            "market": position["market_symbol"],
                            "trial_collateral_points": trial_added,
                            "chain_collateral_points": chain_added,
                        },
                        actor=actor,
                    )["ledger_uuid"]
                )
        now = _now_text()
        conn.execute(
            """
            UPDATE trading_margin_positions
            SET collateral_points=collateral_points+?,
                collateral_trial_points=collateral_trial_points+?,
                collateral_chain_points=collateral_chain_points+?,
                updated_at=?
            WHERE id=?
            """,
            (amount, trial_added, chain_added, now, position["id"]),
        )
        service._audit_event(
            conn,
            "TRADING_MARGIN_COLLATERAL_ADDED",
            "margin collateral added",
            actor=actor,
            target_user_id=user_id,
            market_symbol=position["market_symbol"],
            metadata={
                "position_id": position["id"],
                "position_uuid": position["position_uuid"],
                "amount_points": amount,
                "funding_mode": "root_simulated"
                if is_root_simulated
                else ("trial_mixed" if trial_added else "points_chain"),
                "trial_collateral_points": trial_added,
                "chain_collateral_points": chain_added,
                "ledger_uuids": ledger_uuids,
            },
        )
        row = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE id=?",
            (position["id"],),
        ).fetchone()
        result = {
            "ok": True,
            "position": service._margin_position_payload_with_risk(conn, row),
            "funding": service._funding_payload(conn, user_id),
        }
        if operation_key:
            conn.execute(
                """
                UPDATE trading_operation_idempotency
                SET response_json=?, updated_at=?
                WHERE idempotency_key=?
                """,
                (_json_dumps(result), _now_text(), operation_key),
            )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def withdraw_margin_collateral(
    service,
    *,
    actor,
    position_uuid,
    amount_points,
    idempotency_key=None,
    ctx=None,
):
    ctx = service._resolve_trading_ctx(ctx, action="withdraw_margin_collateral")
    actor_user_id = service._actor_id(actor)
    if not actor_user_id:
        raise ValueError("login required")
    amount = _to_int(amount_points, name="collateral_points", minimum=1, maximum=10**12)
    request_time = datetime.now()
    boundary_operation_key = ""
    key_prefix = f"margin_collateral_withdraw:{actor_user_id}:{position_uuid}"
    if idempotency_key:
        operation_key = _client_idempotency_key(idempotency_key, prefix=key_prefix)
    else:
        operation_key, boundary_operation_key, request_time = _implicit_operation_keys(
            value_prefix=f"{position_uuid}:{amount}",
            key_prefix=key_prefix,
            request_time=request_time,
        )
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        service._assert_writable(conn)
        if operation_key:
            replay = _operation_replay_response(
                conn,
                operation="margin_collateral_withdraw",
                operation_key=operation_key,
                boundary_operation_key=boundary_operation_key,
                request_time=request_time,
            )
            if replay is not None:
                conn.rollback()
                return replay
            now_text = _now_text()
            insert_cur = conn.execute(
                """
                INSERT OR IGNORE INTO trading_operation_idempotency (
                    idempotency_key, operation, user_id, reference_uuid, response_json, created_at, updated_at
                ) VALUES (?, 'margin_collateral_withdraw', ?, ?, '', ?, ?)
                """,
                (operation_key, int(actor_user_id), str(position_uuid or ""), now_text, now_text),
            )
            if insert_cur.rowcount == 0:
                existing_operation = conn.execute(
                    "SELECT response_json FROM trading_operation_idempotency WHERE idempotency_key=?",
                    (operation_key,),
                ).fetchone()
                if existing_operation and existing_operation["response_json"]:
                    result = _json_loads(existing_operation["response_json"], {"ok": True})
                    conn.rollback()
                    return result
                raise ValueError("duplicate margin collateral withdrawal is still processing")
        position = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE position_uuid=?",
            (str(position_uuid or ""),),
        ).fetchone()
        if not position:
            raise ValueError("margin position not found")
        user_id = int(position["user_id"])
        if int(user_id) != int(actor_user_id):
            raise ValueError("cannot update another user's margin position")
        if position["status"] != "open":
            raise ValueError("margin position is not open")
        collateral = int(position["collateral_points"] or 0)
        if amount >= collateral:
            raise ValueError("抽出後保證金不可為 0")
        market = service._market(conn, position["market_symbol"])
        risk_before = service._margin_risk_payload(conn, position, market=market)
        withdrawable = margin_withdrawable_collateral_payload(
            collateral=collateral,
            unrealized_pnl=int(risk_before.get("unrealized_pnl_points") or 0),
            equity_after=int(risk_before.get("equity_after_points") or 0),
            maintenance_points=int(
                risk_before.get("maintenance_margin_points")
                or risk_before.get("maintenance_points")
                or 0
            ),
        )
        profitable_surplus = int(withdrawable["withdrawable_collateral_profit_cap_points"])
        max_withdrawable = int(withdrawable["max_withdrawable_collateral_points"])
        if profitable_surplus <= 0:
            raise ValueError("目前沒有可抽出的未實現盈利")
        if amount > max_withdrawable:
            raise ValueError(f"可抽出保證金上限為 {max_withdrawable} 點")
        collateral_trial = int(position["collateral_trial_points"] or 0) if "collateral_trial_points" in position.keys() else 0
        collateral_chain = int(position["collateral_chain_points"] or 0) if "collateral_chain_points" in position.keys() else collateral
        chain_release = min(amount, collateral_chain)
        trial_release = min(collateral_trial, amount - chain_release)
        if chain_release + trial_release != amount and not service._is_root_user_id(conn, user_id):
            raise ValueError("margin collateral accounting is inconsistent")
        is_long = position["position_type"] == POSITION_MARGIN_LONG
        principal_increment = amount if is_long else 0
        now = _now_text()
        conn.execute(
            """
            UPDATE trading_margin_positions
            SET collateral_points=collateral_points-?,
                collateral_trial_points=MAX(0, collateral_trial_points-?),
                collateral_chain_points=MAX(0, collateral_chain_points-?),
                principal_points=principal_points+?,
                updated_at=?
            WHERE id=?
            """,
            (amount, trial_release, chain_release, principal_increment, now, position["id"]),
        )
        updated_for_risk = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE id=?",
            (position["id"],),
        ).fetchone()
        risk_after = service._margin_risk_payload(conn, updated_for_risk, market=market)
        if risk_after.get("liquidation_required"):
            raise ValueError("抽出後權益會低於維持保證金，已拒絕")
        ledger_uuids = []
        is_root_simulated = service._is_root_user_id(conn, user_id)
        route_ctx = service._routing_ctx_for_read(ctx)
        if is_root_simulated:
            service._sim_delta(conn, user_id, balance_delta=amount, locked_delta=-amount)
        else:
            if is_long and principal_increment:
                service._reserve_delta(
                    conn,
                    delta=-principal_increment,
                    event_type="margin_collateral_withdraw_principal_lent",
                    reason="TRADING_MARGIN_COLLATERAL_WITHDRAW_PRINCIPAL_LENT",
                    actor=actor,
                )
            if chain_release:
                ledger_uuids.append(
                    service._ledger(
                        conn,
                        ctx=route_ctx,
                        user_id=user_id,
                        currency_type="points",
                        direction="unfreeze",
                        amount=chain_release,
                        action_type="trading_margin_collateral_unfreeze",
                        reference_type="trading_margin_position",
                        reference_id=position["position_uuid"],
                        idempotency_key=f"trading:margin:collateral_withdraw:{operation_key}",
                        reason="TRADING_MARGIN_COLLATERAL_WITHDRAW",
                        public_metadata={
                            "position_type": position["position_type"],
                            "market": position["market_symbol"],
                            "amount_points": amount,
                            "trial_collateral_points": trial_release,
                            "chain_collateral_points": chain_release,
                        },
                        actor=actor,
                    )["ledger_uuid"]
                )
            if trial_release:
                service._release_trial_margin_collateral(
                    conn,
                    user_id,
                    collateral_trial=trial_release,
                    available_delta_if_active=trial_release,
                )
        row = conn.execute(
            "SELECT * FROM trading_margin_positions WHERE id=?",
            (position["id"],),
        ).fetchone()
        warning = "抽出保證金會降低維持率；價格反向波動時更容易接近強平線，請自行控管風險。"
        service._audit_event(
            conn,
            "TRADING_MARGIN_COLLATERAL_WITHDRAWN",
            "margin collateral withdrawn from profitable position",
            actor=actor,
            target_user_id=user_id,
            market_symbol=position["market_symbol"],
            severity="warning",
            metadata={
                "position_id": position["id"],
                "position_uuid": position["position_uuid"],
                "amount_points": amount,
                "principal_increment_points": principal_increment,
                "maintenance_ratio_before": risk_before.get("maintenance_ratio_percent"),
                "maintenance_ratio_after": risk_after.get("maintenance_ratio_percent"),
                "trial_collateral_points": trial_release,
                "chain_collateral_points": chain_release,
                "ledger_uuids": ledger_uuids,
            },
        )
        result = {
            "ok": True,
            "amount_points": amount,
            "warning": warning,
            "risk_before": risk_before,
            "risk_after": risk_after,
            "position": service._margin_position_payload_with_risk(conn, row),
            "funding": service._funding_payload(conn, user_id),
        }
        if operation_key:
            conn.execute(
                """
                UPDATE trading_operation_idempotency
                SET response_json=?, updated_at=?
                WHERE idempotency_key=?
                """,
                (_json_dumps(result), _now_text(), operation_key),
            )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close_margin_position(
    service,
    *,
    actor,
    position_uuid,
    force_liquidation=False,
    price_override_points=None,
    price_source_override=None,
    allow_internal_price_override=False,
    ctx=None,
):
    ctx = service._resolve_trading_ctx(ctx, action="close_margin_position")
    actor_user_id = service._actor_id(actor)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        service._assert_writable(conn)
        margin_positions_table, route_ctx = service._resolve_table(
            "margin_positions", ctx, action="close_margin_position"
        )
        position = conn.execute(
            f"SELECT * FROM {margin_positions_table} WHERE position_uuid=?",
            (str(position_uuid or ""),),
        ).fetchone()
        if not position:
            raise ValueError("margin position not found")
        user_id = int(position["user_id"])
        if not force_liquidation and int(user_id) != int(actor_user_id or 0):
            raise ValueError("cannot close another user's margin position")
        if position["status"] != "open":
            raise ValueError("margin position is not open")
        if force_liquidation:
            source_table = liquidation_target_table(route_ctx)
            settle_table = liquidation_settle_table(route_ctx)
            assert_same_world(route_ctx, route_ctx, "liquidation")
            if source_table != margin_positions_table:
                raise ValueError("liquidation source table mismatch")
            if settle_table != resolve_table("wallets", route_ctx):
                raise ValueError("liquidation settle table mismatch")
            if route_ctx.mode != "production":
                raise ValueError(
                    "shadow liquidation is not supported yet; production-only until shadow funding world lands"
                )
        position = service._accrue_margin_interest(conn, position, actor=actor, ctx=route_ctx)
        market = service._market(conn, position["market_symbol"])
        risk = margin_risk_payload(
            service,
            conn,
            position,
            market,
            price_override_points=price_override_points,
            price_source_override=price_source_override,
            strict_high_risk=True,
            allow_internal_price_override=allow_internal_price_override,
        )
        account_risk = None
        if force_liquidation:
            open_rows = [
                margin_position_payload_with_risk(
                    service,
                    conn,
                    row,
                    risk_overrides={
                        "price_override_points": (
                            price_override_points
                            if row["market_symbol"] == position["market_symbol"]
                            else None
                        ),
                        "price_source_override": (
                            price_source_override
                            if row["market_symbol"] == position["market_symbol"]
                            else None
                        ),
                    }
                    if price_override_points is not None
                    else None,
                    strict_risk=True,
                    allow_internal_price_override=allow_internal_price_override,
                )
                for row in conn.execute(
                    f"SELECT * FROM {margin_positions_table} WHERE user_id=? AND status='open' ORDER BY id ASC",
                    (user_id,),
                ).fetchall()
            ]
            account_risk = margin_account_payload(service, conn, user_id, open_rows)
            if not account_risk.get("liquidation_required"):
                raise ValueError("margin position recovered above liquidation threshold")
        price = risk["price_points"]
        price_source = risk["price_source"]
        close_fee = risk["close_fee_points"]
        interest = risk["interest_points"]
        principal = int(position["principal_points"] or 0)
        collateral = int(position["collateral_points"] or 0)
        collateral_trial = (
            int(position["collateral_trial_points"] or 0)
            if "collateral_trial_points" in position.keys()
            else 0
        )
        collateral_chain = (
            int(position["collateral_chain_points"] or 0)
            if "collateral_chain_points" in position.keys()
            else collateral
        )
        delta = risk["delta_points"]
        paid_profit_points = 0
        pending_profit_points = 0
        governance_proposal_uuid = ""
        ledger_uuids = []
        is_root_simulated = service._is_root_user_id(conn, user_id)
        if principal and not is_root_simulated:
            service._reserve_delta(
                conn,
                delta=principal,
                event_type="margin_principal_repaid",
                reason="TRADING_MARGIN_PRINCIPAL_REPAID",
                actor=actor,
                source_user_id=user_id,
            )
        if is_root_simulated:
            simulated_return = max(0, collateral + delta)
            service._sim_delta(
                conn, user_id, balance_delta=simulated_return, locked_delta=-collateral
            )
            if collateral + delta < 0:
                service._audit_event(
                    conn,
                    "TRADING_ROOT_SIM_MARGIN_BAD_DEBT",
                    "root simulated margin position closed below collateral",
                    actor=actor,
                    target_user_id=user_id,
                    market_symbol=market["symbol"],
                    severity="warning",
                    metadata={
                        "position_uuid": position["position_uuid"],
                        "simulated_bad_debt_points": abs(collateral + delta),
                        "risk": risk,
                    },
                )
        elif collateral_chain:
            ledger_uuids.append(
                service._ledger(
                    conn,
                    ctx=route_ctx,
                    user_id=user_id,
                    currency_type="points",
                    direction="unfreeze",
                    amount=collateral_chain,
                    action_type="trading_margin_collateral_unfreeze",
                    reference_type="trading_margin_position",
                    reference_id=position["position_uuid"],
                    idempotency_key=f"trading:margin:collateral_unfreeze:{position['position_uuid']}",
                    reason="TRADING_MARGIN_COLLATERAL_RELEASE",
                    public_metadata={
                        "position_type": position["position_type"],
                        "market": market["symbol"],
                        "trial_collateral_points": collateral_trial,
                        "chain_collateral_points": collateral_chain,
                    },
                    actor=actor,
                )["ledger_uuid"]
            )
        if is_root_simulated:
            pass
        elif delta > 0:
            settlement_capacity = service._reserve_profit_settlement_capacity(
                conn,
                requested_points=delta,
                trigger_event_type="margin_profit_paid",
                reason="TRADING_MARGIN_PROFIT_PAID",
                actor=actor,
                source_user_id=user_id,
                position_uuid=position["position_uuid"],
            )
            paid_profit_points = int(settlement_capacity["payable_points"] or 0)
            pending_profit_points = int(settlement_capacity["pending_points"] or 0)
            if paid_profit_points:
                service._reserve_delta(
                    conn,
                    delta=-paid_profit_points,
                    event_type="margin_profit_paid",
                    reason="TRADING_MARGIN_PROFIT_PAID",
                    actor=actor,
                    source_user_id=user_id,
                )
            if collateral_trial:
                service._release_trial_margin_collateral(
                    conn,
                    user_id,
                    collateral_trial=collateral_trial,
                    available_delta_if_active=collateral_trial,
                )
            if paid_profit_points:
                ledger_uuids.append(
                    service._ledger(
                        conn,
                        ctx=route_ctx,
                        user_id=user_id,
                        currency_type="points",
                        direction="credit",
                        amount=paid_profit_points,
                        action_type="trading_margin_profit",
                        reference_type="trading_margin_position",
                        reference_id=position["position_uuid"],
                        idempotency_key=f"trading:margin:profit:{position['position_uuid']}",
                        reason="TRADING_MARGIN_PROFIT",
                        public_metadata={
                            "position_type": position["position_type"],
                            "market": market["symbol"],
                            "exit_price_points": price,
                            "realized_pnl_points": delta,
                            "paid_profit_points": paid_profit_points,
                            "pending_profit_points": pending_profit_points,
                            "settlement_note": "profit paid only from available exchange fund; shortfall becomes a governance-tracked liability",
                        },
                        actor=actor,
                    )["ledger_uuid"]
                )
            if pending_profit_points:
                pending_row = service._record_pending_profit(
                    conn,
                    user_id=user_id,
                    market_symbol=market["symbol"],
                    amount_points=pending_profit_points,
                    reason="TRADING_MARGIN_PROFIT_EXCHANGE_FUND_SHORTFALL",
                    actor=actor,
                    position_uuid=position["position_uuid"],
                )
                if pending_row and "governance_proposal_uuid" in pending_row.keys():
                    governance_proposal_uuid = str(pending_row["governance_proposal_uuid"] or "")
        elif delta < 0:
            remaining_loss = abs(delta)
            if collateral_trial:
                trial_loss = min(collateral_trial, remaining_loss)
                trial_return = collateral_trial - trial_loss
                service._release_trial_margin_collateral(
                    conn,
                    user_id,
                    collateral_trial=collateral_trial,
                    available_delta_if_active=trial_return,
                )
                remaining_loss -= trial_loss
            wallet_payload = service._wallet_payload(conn, user_id, ctx=ctx)
            wallet_balance = int(wallet_payload.get("points_balance") or 0)
            debit_amount = min(remaining_loss, wallet_balance)
            bad_debt = remaining_loss - debit_amount
            if debit_amount:
                ledger_uuids.append(
                    service._ledger(
                        conn,
                        ctx=route_ctx,
                        user_id=user_id,
                        currency_type="points",
                        direction="debit",
                        amount=debit_amount,
                        action_type="trading_margin_loss",
                        reference_type="trading_margin_position",
                        reference_id=position["position_uuid"],
                        idempotency_key=f"trading:margin:loss:{position['position_uuid']}",
                        reason=(
                            "TRADING_MARGIN_LIQUIDATION_LOSS"
                            if force_liquidation
                            else "TRADING_MARGIN_LOSS"
                        ),
                        public_metadata={
                            "position_type": position["position_type"],
                            "market": market["symbol"],
                            "exit_price_points": price,
                            "bad_debt_points": bad_debt,
                        },
                        actor=actor,
                    )["ledger_uuid"]
                )
                collected_price_loss = max(0, debit_amount - int(close_fee or 0) - int(interest or 0))
                if collected_price_loss:
                    service._reserve_delta(
                        conn,
                        delta=collected_price_loss,
                        event_type="margin_loss_collected",
                        reason="TRADING_MARGIN_LOSS_COLLECTED",
                        actor=actor,
                        source_user_id=user_id,
                    )
            if bad_debt:
                service._audit_event(
                    conn,
                    "TRADING_MARGIN_BAD_DEBT",
                    "margin position closed with bad debt",
                    actor=actor,
                    target_user_id=user_id,
                    market_symbol=market["symbol"],
                    severity="critical",
                    metadata={
                        "position_uuid": position["position_uuid"],
                        "bad_debt_points": bad_debt,
                        "risk": risk,
                    },
                )
        elif collateral_trial:
            service._release_trial_margin_collateral(
                conn,
                user_id,
                collateral_trial=collateral_trial,
                available_delta_if_active=collateral_trial,
            )
        if close_fee and not is_root_simulated:
            service._reserve_delta(
                conn,
                delta=close_fee,
                event_type="margin_fee_retained",
                reason="TRADING_MARGIN_CLOSE_FEE",
                actor=actor,
                source_user_id=user_id,
            )
        if interest and not is_root_simulated:
            service._reserve_delta(
                conn,
                delta=interest,
                event_type="margin_interest_retained",
                reason="TRADING_MARGIN_INTEREST",
                actor=actor,
                source_user_id=user_id,
            )
        now = _now_text()
        next_status = "liquidated" if force_liquidation else "closed"
        conn.execute(
            f"""
            UPDATE {margin_positions_table}
            SET close_fee_points=?,
                close_fee_micropoints=?,
                interest_points=?,
                interest_carry_micropoints=0,
                exit_price_points=?,
                realized_pnl_points=?,
                status=?,
                closed_at=?,
                updated_at=?
            WHERE id=?
            """,
            (
                close_fee,
                int(risk.get("settlement_fee_micropoints") or risk.get("close_fee_micropoints") or 0),
                interest,
                price,
                delta,
                next_status,
                now,
                now,
                position["id"],
            ),
        )
        event_type = (
            "TRADING_MARGIN_POSITION_LIQUIDATED"
            if force_liquidation
            else "TRADING_MARGIN_POSITION_CLOSED"
        )
        service._audit_event(
            conn,
            event_type,
            "margin borrow position liquidated"
            if force_liquidation
            else "margin borrow position closed",
            actor=actor,
            target_user_id=user_id,
            market_symbol=market["symbol"],
            severity="warning" if force_liquidation else "info",
            metadata={
                "position_id": position["id"],
                "position_uuid": position["position_uuid"],
                "position_type": position["position_type"],
                "entry_price_points": float(
                    _to_decimal(
                        position["entry_price_points"],
                        name="entry_price_points",
                        minimum=0,
                    )
                ),
                "exit_price_points": price,
                "price_source": price_source,
                "delta_points": delta,
                "paid_profit_points": paid_profit_points,
                "pending_profit_points": pending_profit_points,
                "governance_proposal_uuid": governance_proposal_uuid,
                "interest_points": interest,
                "close_fee_points": close_fee,
                "funding_mode": (
                    "root_simulated"
                    if is_root_simulated
                    else ("trial_mixed" if collateral_trial else "points_chain")
                ),
                "override_price": bool(price_override_points is not None),
                "risk": risk,
                "account_risk": account_risk,
                "ledger_uuids": ledger_uuids,
            },
        )
        if not is_root_simulated:
            service._record_user_trade_volume(
                conn,
                user_id=user_id,
                trade_kind="margin",
                notional_points=int(risk.get("exit_notional_points") or 0),
                fee_points=close_fee,
                occurred_at=now,
            )
        if force_liquidation:
            service._notify_margin_liquidated(conn, user_id=user_id, position=position, risk=risk)
        conn.commit()
        row = conn.execute(
            f"SELECT * FROM {margin_positions_table} WHERE id=?",
            (position["id"],),
        ).fetchone()
        return {
            "ok": True,
            "position": service._margin_position_payload(row),
            "delta_points": delta,
            "paid_profit_points": paid_profit_points,
            "pending_profit_points": pending_profit_points,
            "governance_proposal_uuid": governance_proposal_uuid,
            "interest_points": interest,
            "close_fee_points": close_fee,
            "funding": service._funding_payload(conn, user_id),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def scan_margin_liquidations(service, *, actor=None, limit=100, ctx=None):
    ctx = service._resolve_trading_ctx(ctx, action="scan_margin_liquidations")
    limit = _to_int(limit or 100, name="limit", minimum=1, maximum=500)
    actor = actor or {"username": "system", "role": "system"}
    conn = service.get_db()
    candidates = []
    errors = []
    scanned = 0
    try:
        service.ensure_schema(conn)
        margin_positions_table, route_ctx = service._resolve_table(
            "margin_positions", ctx, action="scan_margin_liquidations"
        )
        liquidation_target_table(route_ctx)
        liquidation_settle_table(route_ctx)
        assert_same_world(route_ctx, route_ctx, "liquidation")
        settings = service._settings_payload(conn)
        if not settings.get("borrowing_enabled"):
            return {
                "ok": True,
                "enabled": False,
                "reason": "borrowing_disabled",
                "scanned": 0,
                "candidates": [],
                "liquidated": [],
                "errors": [],
            }
        if not settings.get("margin_liquidation_enabled"):
            return {
                "ok": True,
                "enabled": False,
                "reason": "liquidation_disabled",
                "scanned": 0,
                "candidates": [],
                "liquidated": [],
                "errors": [],
            }
        state = service._state(conn)
        if state.get("safe_mode"):
            return {
                "ok": True,
                "enabled": False,
                "reason": "trading_safe_mode",
                "scanned": 0,
                "candidates": [],
                "liquidated": [],
                "errors": [],
            }
        if route_ctx.mode != "production":
            return {
                "ok": True,
                "enabled": False,
                "reason": "shadow_liquidation_unsupported",
                "scanned": 0,
                "candidates": [],
                "liquidated": [],
                "errors": [],
            }
        rows = conn.execute(
            f"SELECT * FROM {margin_positions_table} WHERE status='open' ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        scanned = len(rows)
        positions_by_user = {}
        for position in rows:
            try:
                position = service._accrue_margin_interest(conn, position, actor=actor, ctx=route_ctx)
                market = service._market(conn, position["market_symbol"])
                if not service._is_market_boot_ready(market, conn=conn):
                    errors.append(
                        {
                            "position_uuid": position["position_uuid"],
                            "user_id": int(position["user_id"]),
                            "error": (
                                f"market {market['symbol']} 尚未收到任何即時價格更新，"
                                "清算掃描已跳過以避免使用啟動時的預設參考價"
                            ),
                            "price_health": "boot_pending",
                        }
                    )
                    continue
                current_price, price_source, price_meta = service._current_market_price_points(
                    conn, market, with_meta=True, high_risk=True
                )
                if bool(price_meta.get("high_risk_blocked")):
                    errors.append(
                        {
                            "position_uuid": position["position_uuid"],
                            "user_id": int(position["user_id"]),
                            "error": str(
                                price_meta.get("high_risk_block_reason")
                                or "price source is in conservative mode"
                            ),
                            "price_health": str(price_meta.get("price_health") or ""),
                        }
                    )
                    continue
                price_window = service._recent_price_window(
                    market["symbol"], lookback_seconds=65, interval="1m", conn=conn
                )
                replay_price = float(current_price)
                if price_window:
                    if position["position_type"] == "margin_long":
                        replay_price = min(float(current_price), float(price_window["low_points"]))
                    else:
                        replay_price = max(float(current_price), float(price_window["high_points"]))
                payload = margin_position_payload_with_risk(
                    service,
                    conn,
                    position,
                    market=market,
                    risk_overrides={
                        "price_override_points": replay_price,
                        "price_source_override": (
                            f"{price_source}+scan_window"
                            if replay_price != current_price
                            else price_source
                        ),
                    },
                    strict_risk=True,
                    allow_internal_price_override=True,
                )
                risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
                notify_margin_risk_alerts(service, conn, position=position, risk=risk, market=market)
                positions_by_user.setdefault(int(position["user_id"]), []).append(payload)
            except Exception as exc:
                errors.append(
                    {
                        "position_uuid": position["position_uuid"],
                        "user_id": int(position["user_id"]),
                        "error": str(exc),
                    }
                )
        for user_id, user_positions in positions_by_user.items():
            account_risk = margin_account_payload(service, conn, user_id, user_positions)
            if not account_risk.get("liquidation_required"):
                continue
            ordered = sorted(
                [row for row in user_positions if row.get("status") == "open"],
                key=margin_liquidation_order_key,
            )
            if not ordered:
                continue
            first = ordered[0]
            candidates.append(
                {
                    "position_uuid": first["position_uuid"],
                    "user_id": int(first["user_id"]),
                    "market_symbol": first["market_symbol"],
                    "position_type": first["position_type"],
                    "risk": first.get("risk") or {},
                    "account_risk": account_risk,
                    "liquidation_order": [row["position_uuid"] for row in ordered],
                }
            )
        conn.commit()
    finally:
        conn.close()

    liquidated = []
    for candidate in candidates:
        try:
            result = close_margin_position(
                service,
                actor=actor,
                position_uuid=candidate["position_uuid"],
                force_liquidation=True,
                price_override_points=(candidate.get("risk") or {}).get("price_points"),
                price_source_override=(candidate.get("risk") or {}).get("price_source"),
                allow_internal_price_override=True,
                ctx=ctx,
            )
            liquidated.append(
                {
                    "position_uuid": candidate["position_uuid"],
                    "user_id": candidate["user_id"],
                    "market_symbol": candidate["market_symbol"],
                    "delta_points": int(result.get("delta_points") or 0),
                    "interest_points": int(result.get("interest_points") or 0),
                    "close_fee_points": int(result.get("close_fee_points") or 0),
                    "risk": candidate["risk"],
                    "account_risk": candidate.get("account_risk"),
                    "liquidation_order": candidate.get("liquidation_order") or [],
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "position_uuid": candidate["position_uuid"],
                    "user_id": candidate["user_id"],
                    "error": str(exc),
                }
            )
    return {
        "ok": not errors,
        "enabled": True,
        "scanned": scanned,
        "candidates": candidates,
        "liquidated": liquidated,
        "errors": errors,
    }


def _margin_target_hit(position, *, observed_price, observed_low, observed_high):
    entry_price = float(_to_decimal(position["entry_price_points"] or 0, name="entry_price_points", minimum=0.00000001))
    stop_loss_percent = (
        float(position["stop_loss_percent"])
        if "stop_loss_percent" in position.keys() and position["stop_loss_percent"] is not None
        else None
    )
    take_profit_percent = (
        float(position["take_profit_percent"])
        if "take_profit_percent" in position.keys() and position["take_profit_percent"] is not None
        else None
    )
    if entry_price <= 0:
        return None
    is_short = _is_short_position(position["position_type"])
    if is_short:
        low_pnl = round((entry_price - observed_high) * 100.0 / entry_price, 4) if observed_high > 0 else None
        high_pnl = round((entry_price - observed_low) * 100.0 / entry_price, 4) if observed_low > 0 else None
    else:
        low_pnl = round((observed_low - entry_price) * 100.0 / entry_price, 4) if observed_low > 0 else None
        high_pnl = round((observed_high - entry_price) * 100.0 / entry_price, 4) if observed_high > 0 else None
    if stop_loss_percent and low_pnl is not None and low_pnl <= -abs(stop_loss_percent):
        return {
            "target_type": "stop_loss",
            "target_percent": stop_loss_percent,
            "observed_price_points": observed_price,
            "observed_pnl_percent": low_pnl,
        }
    if take_profit_percent and high_pnl is not None and high_pnl >= abs(take_profit_percent):
        return {
            "target_type": "take_profit",
            "target_percent": take_profit_percent,
            "observed_price_points": observed_price,
            "observed_pnl_percent": high_pnl,
        }
    return None


def scan_margin_risk_targets(service, *, actor=None, limit=100, ctx=None):
    ctx = service._resolve_trading_ctx(ctx, action="scan_margin_risk_targets")
    limit = _to_int(limit or 100, name="limit", minimum=1, maximum=1000)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        margin_positions_table, route_ctx = service._resolve_table(
            "margin_positions", ctx, action="scan_margin_risk_targets"
        )
        rows = conn.execute(
            f"""
            SELECT p.*, u.username, u.role
            FROM {margin_positions_table} p
            JOIN users u ON u.id = p.user_id
            WHERE p.status='open'
              AND (p.stop_loss_percent IS NOT NULL OR p.take_profit_percent IS NOT NULL)
            ORDER BY p.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        candidates = []
        skipped = []
        errors = []
        for row in rows:
            try:
                market = service._market(conn, row["market_symbol"])
                if not service._is_market_boot_ready(market, conn=conn):
                    skipped.append({"position_uuid": row["position_uuid"], "reason": "market_boot_pending"})
                    continue
                observed_price, _price_source, price_meta = service._current_market_price_points(
                    conn, market, with_meta=True, high_risk=True
                )
                service._assert_price_meta_allows_high_risk_use(
                    conn,
                    actor={"id": int(row["user_id"]), "username": row["username"], "role": row["role"]},
                    market_symbol=market["symbol"],
                    usage="margin risk target auto close",
                    price_meta=price_meta,
                )
                settings = service._settings_payload(conn)
                price_window = service._recent_price_window(
                    market["symbol"],
                    lookback_seconds=max(60, int(settings.get("bot_auto_scan_interval_seconds") or 30) + 5),
                    conn=conn,
                )
                observed_low = float((price_window or {}).get("low_points") or observed_price)
                observed_high = float((price_window or {}).get("high_points") or observed_price)
                target = _margin_target_hit(
                    row,
                    observed_price=observed_price,
                    observed_low=observed_low,
                    observed_high=observed_high,
                )
                if target is None:
                    continue
                candidates.append(
                    {
                        "actor": {"id": int(row["user_id"]), "username": row["username"], "role": row["role"]},
                        "position_uuid": row["position_uuid"],
                        **target,
                    }
                )
            except Exception as exc:
                errors.append({"position_uuid": row["position_uuid"], "error": str(exc)})
        conn.close()
        conn = None
        triggered = []
        for candidate in candidates:
            try:
                result = close_margin_position(
                    service,
                    actor=candidate["actor"],
                    position_uuid=candidate["position_uuid"],
                    ctx=route_ctx,
                )
                triggered.append(
                    {
                        "position_uuid": candidate["position_uuid"],
                        "target_type": candidate["target_type"],
                        "target_percent": candidate["target_percent"],
                        "observed_pnl_percent": candidate["observed_pnl_percent"],
                        "delta_points": result.get("delta_points"),
                    }
                )
            except Exception as exc:
                errors.append({"position_uuid": candidate["position_uuid"], "target_type": candidate["target_type"], "error": str(exc)})
        return {"ok": not errors, "scanned": len(rows), "triggered": triggered, "skipped": skipped, "errors": errors}
    finally:
        if conn is not None:
            conn.close()
