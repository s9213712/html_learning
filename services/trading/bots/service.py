"""Trading bot service orchestration.

This module owns trading-bot CRUD, runtime execution, and audit
orchestration. Pure indicator, workflow, and audit reduction logic stays in
the sibling bot modules.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN

from services.system.notifications import create_notification_if_enabled, create_root_notification_if_enabled
from services.trading.accounting.core import fee_points, notional_points, quantity_to_units, units_to_quantity
from services.trading.bots.audit import (
    bot_audit_enabled_at,
    bot_audit_is_eligible,
    bot_audit_latest_map,
    bot_audit_result,
    build_bot_audit_dashboard_item,
    increment_audit_summary,
)
from services.trading.bots.workflow import condition_label
from services.trading.constants import (
    ASSET_SCALE,
    TRADING_BOT_AUDIT_INTERVAL_SECONDS,
    TRADING_BOT_AUDIT_LIMIT,
    TRADING_BOT_AUDIT_MIN_ENABLED_SECONDS,
    TRADING_BOT_TRIGGER_TYPES,
    TRADING_BOT_TYPES,
    UNLIMITED_BOT_MAX_RUNS,
)
from services.trading.notifications import (
    bot_audit_notification_payload,
    create_trading_root_notification,
    create_trading_user_notification,
)
from services.trading._clock import now_text as _now_text
from services.trading.payloads import bot_audit_eligibility_reason_label, bot_audit_label
from services.trading.validators import _decimal_text, _to_decimal, _to_int, _to_price_float

_LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc


def _json_dumps(value):
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value, default=None):
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except Exception:
        return default if default is not None else {}


def _normalize_optional_risk_percent(value, *, name):
    if value in (None, ""):
        return None
    number = float(_to_decimal(value, name=name, minimum=0.00000001, maximum=1000))
    return number if number > 0 else None


def _normalize_optional_price_limit(value, *, name):
    if value in (None, ""):
        return None
    return _to_price_float(value, name=name, minimum=0.00000001, maximum=10**12)


def bot_max_runs_from_storage(value):
    number = int(value or 0)
    return -1 if number >= UNLIMITED_BOT_MAX_RUNS else number


def bot_max_runs_to_storage(value, *, allow_unlimited=False, maximum=1000):
    number = _to_int(value, name="max_runs", minimum=(-1 if allow_unlimited else 1), maximum=maximum)
    if allow_unlimited and number == -1:
        return UNLIMITED_BOT_MAX_RUNS
    return number


def bot_max_runs_has_remaining(run_count, max_runs):
    max_runs = int(max_runs or 0)
    if max_runs >= UNLIMITED_BOT_MAX_RUNS:
        return True
    return int(run_count or 0) < max_runs


def _active_user_sql(conn):
    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    active_clauses = []
    if "status" in user_cols:
        active_clauses.append("COALESCE(u.status, 'active') = 'active'")
    if "deleted_at" in user_cols:
        active_clauses.append("COALESCE(u.deleted_at, '') = ''")
    return " AND ".join(active_clauses) if active_clauses else "1=1"


def _bot_actor(row):
    return {
        "id": int(row["user_id"]),
        "username": row["username"],
        "role": row["role"],
    }


def _workflow_state(value):
    state = _json_loads(value, {}) if value else {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("executed_action_ids", [])
    state.setdefault("branch_step_counts", {})
    return state


def _bot_open_order_frozen_points(conn, bot_id):
    row = conn.execute(
        """
        SELECT COALESCE(SUM(o.frozen_points), 0) AS total
        FROM trading_bot_runs r
        JOIN trading_orders o ON o.order_uuid = r.order_uuid
        WHERE r.bot_id=?
          AND o.status IN ('open', 'partially_filled')
        """,
        (int(bot_id),),
    ).fetchone()
    return int((row or {})["total"] or 0)


def _bot_budget_floor(row, open_order_frozen_points):
    frozen = max(0, int(open_order_frozen_points or 0))
    if str(row["bot_type"] or "conditional") == "dca":
        return max(1, frozen)
    return frozen


def _bot_payload_with_budget_meta(service, conn, row):
    item = service._bot_payload(row)
    open_frozen = _bot_open_order_frozen_points(conn, row["id"])
    budget = int(row["budget_points"] or 0)
    item["open_order_frozen_points"] = open_frozen
    item["minimum_budget_points"] = _bot_budget_floor(row, open_frozen)
    item["budget_cap_enabled"] = str(row["bot_type"] or "conditional") == "dca" or budget > 0
    item["budget_remaining_points"] = max(0, budget - open_frozen) if budget > 0 else None
    return item


def _utc_now():
    return datetime.now(timezone.utc)


def _coerce_utc_datetime(value):
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_LOCAL_TZ).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def legacy_workflow(
    *,
    trigger_type,
    trigger_price,
    side,
    quantity_text,
    order_type,
    limit_price,
    max_runs,
    cooldown_seconds,
):
    condition = {"type": "always"}
    if trigger_type == "price_above":
        condition = {"type": "price_above", "value": int(trigger_price or 0)}
    elif trigger_type == "price_below":
        condition = {"type": "price_below", "value": int(trigger_price or 0)}
    action = {"type": "buy_amount", "amount_points": 100, "step": 1}
    if side == "sell":
        action = {"type": "sell_percent", "percent": 100, "step": 1}
    return {
        "version": 1,
        "strategy_kind": "workflow",
        "source": "legacy_condition",
        "branches": [{
            "id": "branch_1",
            "name": "預設策略",
            "priority": 10,
            "logic": "AND",
            "cooldown_seconds": int(cooldown_seconds or 0),
            "max_runs": int(max_runs or 1),
            "conditions": [condition],
            "actions": [{**action, "order_type": order_type, "limit_price_points": limit_price, "quantity": quantity_text}],
        }],
    }


def validate_bot_payload(service, conn, payload):
    payload = payload or {}
    market = service._market(conn, payload.get("market_symbol"))
    if not int(market.get("allow_bots") or 0):
        raise ValueError("bots are disabled for this market")
    bot_type = str(payload.get("bot_type") or "conditional").strip().lower()
    if bot_type not in TRADING_BOT_TYPES:
        raise ValueError("bot_type must be conditional or dca")
    side = str(payload.get("side") or "").strip().lower()
    order_type = str(payload.get("order_type") or "").strip().lower()
    has_workflow_payload = payload.get("workflow_json") is not None or payload.get("workflow") is not None
    trigger_type = str(payload.get("trigger_type") or ("always" if has_workflow_payload else "")).strip().lower()
    if bot_type == "dca":
        side = "buy"
        order_type = "market"
        trigger_type = "always"
    if side not in {"buy", "sell"}:
        raise ValueError("bot side must be buy or sell")
    if order_type not in {"market", "limit"}:
        raise ValueError("bot order_type must be market or limit")
    if trigger_type not in TRADING_BOT_TRIGGER_TYPES:
        raise ValueError("bot trigger_type must be always, price_above, or price_below")
    budget_points = _to_int(payload.get("budget_points", 0), name="budget_points", minimum=0, maximum=10**12)
    if bot_type == "dca":
        if budget_points <= 0:
            raise ValueError("dca budget_points must be positive")
        quantity_text = "0.00000001"
    else:
        quantity_text = str(payload.get("quantity") or payload.get("quantity_text") or "").strip()
        quantity_to_units(quantity_text)
    limit_price = None
    if order_type == "limit":
        limit_price = _to_price_float(payload.get("limit_price_points"), name="limit_price_points", minimum=0.00000001, maximum=10**12)
    trigger_price = None
    if trigger_type != "always":
        trigger_price = _to_price_float(payload.get("trigger_price_points"), name="trigger_price_points", minimum=0.00000001, maximum=10**12)
    max_runs = bot_max_runs_to_storage(
        payload.get("max_runs", 1),
        allow_unlimited=(bot_type == "dca"),
        maximum=1000,
    )
    cooldown_seconds = _to_int(payload.get("cooldown_seconds", 300), name="cooldown_seconds", minimum=0, maximum=86400)
    interval_hours = _to_int(payload.get("interval_hours", 24), name="interval_hours", minimum=1, maximum=8760)
    if bot_type == "dca":
        cooldown_seconds = max(cooldown_seconds, interval_hours * 3600)
        price_upper_limit = _normalize_optional_price_limit(
            payload.get("price_upper_limit"), name="price_upper_limit"
        )
        price_lower_limit = _normalize_optional_price_limit(
            payload.get("price_lower_limit"), name="price_lower_limit"
        )
        if (
            price_lower_limit is not None
            and price_upper_limit is not None
            and price_lower_limit > price_upper_limit
        ):
            raise ValueError("price_lower_limit must not exceed price_upper_limit")
        max_daily_runs = 0
    else:
        # A Workflow already carries its executable logic in workflow_json.
        # Do not retain a second, ambiguous top-level strategy mode.  The
        # daily cap is only exposed for Workflow bots and has a finite UI
        # range; missing values remain unlimited for existing API clients.
        price_upper_limit = None
        price_lower_limit = None
        max_daily_runs = _to_int(
            payload.get("max_daily_runs", 0),
            name="max_daily_runs",
            # Existing API callers did not send this field.  Preserve their
            # unlimited behaviour, while an explicit setting must satisfy
            # the UI contract of 1..100 rather than silently becoming 0.
            minimum=1 if "max_daily_runs" in payload else 0,
            maximum=100,
        )
    workflow = None
    if bot_type == "conditional":
        workflow = service._validate_workflow(payload.get("workflow_json") or payload.get("workflow"))
        if workflow is None:
            workflow = legacy_workflow(
                trigger_type=trigger_type,
                trigger_price=trigger_price,
                side=side,
                quantity_text=quantity_text,
                order_type=order_type,
                limit_price=limit_price,
                max_runs=max_runs,
                cooldown_seconds=cooldown_seconds,
            )
    if bot_type == "dca":
        stop_loss_percent = _normalize_optional_risk_percent(payload.get("stop_loss_percent"), name="stop_loss_percent")
        take_profit_percent = _normalize_optional_risk_percent(payload.get("take_profit_percent"), name="take_profit_percent")
    else:
        stop_loss_percent = None
        take_profit_percent = None
    name = str(payload.get("name") or "").strip()[:80] or f"{market['symbol']} {bot_type}"
    return {
        "bot_type": bot_type,
        "name": name,
        "market_symbol": market["symbol"],
        "side": side,
        "order_type": order_type,
        "quantity_text": quantity_text,
        "limit_price_points": limit_price,
        "trigger_type": trigger_type,
        "trigger_price_points": trigger_price,
        "enabled": bool(payload.get("enabled", True)),
        "max_runs": max_runs,
        "cooldown_seconds": cooldown_seconds,
        "interval_hours": interval_hours,
        "budget_points": budget_points,
        "price_upper_limit": price_upper_limit,
        "price_lower_limit": price_lower_limit,
        "max_daily_runs": max_daily_runs,
        "stop_loss_percent": stop_loss_percent,
        "take_profit_percent": take_profit_percent,
        "share_parameters": bool(payload.get("share_parameters", False)),
        "workflow": workflow,
    }


def bot_trigger_hit(bot, observed_price, *, observed_low=None, observed_high=None):
    if str(bot["bot_type"] or "conditional") == "dca":
        return True
    trigger_type = bot["trigger_type"]
    if trigger_type == "always":
        return True
    trigger_price = float(bot["trigger_price_points"] or 0)
    low_price = float(observed_low or observed_price or 0)
    high_price = float(observed_high or observed_price or 0)
    if trigger_type == "price_above":
        return high_price > 0 and high_price >= trigger_price
    if trigger_type == "price_below":
        return low_price > 0 and low_price <= trigger_price
    return False


def _units_for_point_budget(*, budget_points, price_points, fee_rate_percent=0.0, label="budget"):
    budget = int(budget_points or 0)
    price = _to_decimal(price_points, name="price_points", minimum=0)
    if budget <= 0 or price <= 0:
        raise ValueError(f"{label} or price is invalid")
    units = int((Decimal(budget) * Decimal(ASSET_SCALE) / price).quantize(Decimal("1"), rounding=ROUND_DOWN))
    low = 0
    high = max(0, units)
    while low < high:
        mid = (low + high + 1) // 2
        gross = notional_points(mid, price)
        total = gross + fee_points(gross, fee_rate_percent)
        if total <= budget:
            low = mid
        else:
            high = mid - 1
    units = low
    if units <= 0:
        raise ValueError(f"{label} is too small for current price")
    return units


def quantity_text_from_budget(*, budget_points, price_points, fee_rate_percent=0.0):
    units = _units_for_point_budget(
        budget_points=budget_points,
        price_points=price_points,
        fee_rate_percent=fee_rate_percent,
        label="dca budget",
    )
    return units_to_quantity(units)


def bot_condition_checks(service, bot, current_price):
    checks = []
    bot_type = str(bot.get("bot_type") or "conditional")
    if bot_type == "dca":
        interval = int(bot.get("interval_hours") or 24)
        last_run = bot.get("last_run_at")
        if last_run:
            try:
                next_dt = datetime.fromisoformat(str(last_run)) + timedelta(hours=interval)
                met = datetime.now() >= next_dt
                checks.append({"label": f"距上次定投已滿 {interval}h", "met": met})
            except Exception:
                checks.append({"label": f"定投間隔 {interval}h", "met": True})
        else:
            checks.append({"label": f"定投間隔 {interval}h（尚未執行過）", "met": True})
        stop_loss_percent = _normalize_optional_risk_percent(bot.get("stop_loss_percent"), name="stop_loss_percent")
        take_profit_percent = _normalize_optional_risk_percent(bot.get("take_profit_percent"), name="take_profit_percent")
        if stop_loss_percent:
            checks.append({"label": f"持倉跌幅達 {stop_loss_percent:g}% 時自動停損", "met": False})
        if take_profit_percent:
            checks.append({"label": f"持倉漲幅達 {take_profit_percent:g}% 時自動停利", "met": False})
        return checks
    workflow = bot.get("workflow")
    if workflow and isinstance(workflow, dict):
        branches = workflow.get("branches") or []
        if branches:
            for index, branch in enumerate(branches):
                cond = branch.get("condition")
                if cond:
                    ctx = {"price": float(current_price or 0)}
                    met = service._workflow_condition_hit(cond, ctx)
                    label = condition_label(cond)
                    checks.append({"label": f"分支{index + 1}: {label}", "met": met})
            return checks
        nodes = workflow.get("nodes") or []
        for node in nodes:
            if node.get("type") == "condition":
                cond = node.get("condition")
                if cond:
                    ctx = {"price": float(current_price or 0)}
                    met = service._workflow_condition_hit(cond, ctx)
                    label = condition_label(cond)
                    checks.append({"label": f"節點 {node.get('id', '')}: {label}", "met": met})
        if not checks:
            checks.append({"label": "Workflow（無條件節點）", "met": True})
        return checks
    trigger_type = str(bot.get("trigger_type") or "always")
    if trigger_type == "always":
        checks.append({"label": "無條件觸發", "met": True})
    elif trigger_type == "price_above":
        threshold = float(_to_decimal(bot.get("trigger_price_points") or 0, name="trigger_price_points", minimum=0))
        live_price = float(_to_decimal(current_price or 0, name="current_price", minimum=0))
        met = live_price >= threshold
        checks.append({"label": f"價格 ≥ {_decimal_text(threshold)} 點（現價 {_decimal_text(live_price)}）", "met": met})
    elif trigger_type == "price_below":
        threshold = float(_to_decimal(bot.get("trigger_price_points") or 0, name="trigger_price_points", minimum=0))
        live_price = float(_to_decimal(current_price or 0, name="current_price", minimum=0))
        met = live_price <= threshold
        checks.append({"label": f"價格 ≤ {_decimal_text(threshold)} 點（現價 {_decimal_text(live_price)}）", "met": met})
    if bot.get("run_count") is not None and bot.get("max_runs") is not None:
        run_count = int(bot["run_count"])
        max_runs = int(bot["max_runs"])
        if max_runs == -1:
            checks.append({"label": f"執行次數 {run_count}/不限制", "met": True})
        else:
            checks.append({"label": f"執行次數 {run_count}/{max_runs}", "met": bot_max_runs_has_remaining(run_count, max_runs)})
    cooldown = int(bot.get("cooldown_seconds") or 0)
    if cooldown > 0 and bot.get("last_run_at"):
        try:
            next_dt = datetime.fromisoformat(str(bot["last_run_at"])) + timedelta(seconds=cooldown)
            met = datetime.now() >= next_dt
            checks.append({"label": f"冷卻 {cooldown}s（{'已解除' if met else '冷卻中'}）", "met": met})
        except Exception:
            pass
    stop_loss_percent = _normalize_optional_risk_percent(bot.get("stop_loss_percent"), name="stop_loss_percent")
    take_profit_percent = _normalize_optional_risk_percent(bot.get("take_profit_percent"), name="take_profit_percent")
    if stop_loss_percent:
        checks.append({"label": f"持倉跌幅達 {stop_loss_percent:g}% 時自動停損", "met": False})
    if take_profit_percent:
        checks.append({"label": f"持倉漲幅達 {take_profit_percent:g}% 時自動停利", "met": False})
    return checks


def workflow_live_context(service, conn, *, market, user_id, observed_price, observed_low=None, observed_high=None):
    position = service._position(conn, int(user_id), market["symbol"])
    qty = int(position["quantity_units"] or 0)
    sellable_units = max(0, qty)
    avg_cost = float(_to_decimal(position["avg_cost_points"] or 0, name="avg_cost_points", minimum=0))
    has_pos = sellable_units > 0
    low_price = float(observed_low or observed_price or 0)
    high_price = float(observed_high or observed_price or 0)
    pnl_percent = None
    pnl_low_percent = None
    pnl_high_percent = None
    if has_pos and avg_cost > 0 and observed_price and observed_price > 0:
        pnl_percent = round((observed_price - avg_cost) * 100.0 / avg_cost, 4)
        if low_price > 0:
            pnl_low_percent = round((low_price - avg_cost) * 100.0 / avg_cost, 4)
        if high_price > 0:
            pnl_high_percent = round((high_price - avg_cost) * 100.0 / avg_cost, 4)
    context = {
        "price": observed_price,
        "window_low_price": low_price or observed_price,
        "window_high_price": high_price or observed_price,
        "has_position": has_pos,
        "sellable_units": sellable_units,
        "avg_cost": avg_cost,
        "pnl_percent": pnl_percent,
        "pnl_low_percent": pnl_low_percent,
        "pnl_high_percent": pnl_high_percent,
    }
    try:
        candles = service._fetch_indicator_candles(market["symbol"], conn=conn)
        if candles:
            latest = dict(candles[-1])
            latest["close_points"] = observed_price
            candles = [*candles[:-1], latest]
            context.update(service._workflow_indicator_context(candles, len(candles) - 1))
            context["price"] = observed_price
            context["window_low_price"] = low_price or observed_price
            context["window_high_price"] = high_price or observed_price
            context["has_position"] = int(position["quantity_units"] or 0) > 0
            context["pnl_percent"] = pnl_percent
            context["pnl_low_percent"] = pnl_low_percent
            context["pnl_high_percent"] = pnl_high_percent
    except Exception as exc:
        service._audit_event(
            conn,
            "TRADING_BOT_INDICATOR_CONTEXT_UNAVAILABLE",
            "trading bot indicator context unavailable; price-only context used",
            target_user_id=int(user_id),
            market_symbol=market["symbol"],
            severity="warning",
            metadata={"error": str(exc)[:200]},
        )
    return context


def workflow_order_from_decision(service, conn, *, user_id, actor, market, decision, price_points, budget_points=None, bot_id=None):
    action = decision.get("action") or {}
    atype = str(action.get("type") or "hold")
    if atype == "hold":
        return None
    funding = service._funding_payload(conn, user_id)
    position = service._position(conn, user_id, market["symbol"])
    order_type = str(action.get("order_type") or "market").lower()
    limit_price = float(_to_decimal(action.get("limit_price_points") or 0, name="limit_price_points", minimum=0)) or None
    if atype in {"buy_percent", "buy_amount"}:
        wallet_available = int(funding.get("available_points") or 0)
        budget_limit = int(budget_points or 0)
        if budget_limit > 0:
            committed = _bot_open_order_frozen_points(conn, bot_id) if bot_id is not None else 0
            available = min(wallet_available, max(0, budget_limit - committed))
        else:
            available = wallet_available
        amount = int(float(action.get("amount_points") or 0))
        if atype == "buy_percent":
            amount = int(available * max(0.0, min(float(action.get("percent") or 0), 100.0)) / 100)
        spend = max(0, min(amount, available))
        if spend <= 0:
            raise ValueError("workflow buy action has no available budget or funds")
        units = _units_for_point_budget(
            budget_points=spend,
            price_points=price_points or 0,
            fee_rate_percent=float(market["fee_rate_percent"] or 0),
            label="workflow buy action",
        )
        return {"side": "buy", "order_type": order_type, "quantity": units_to_quantity(units), "limit_price_points": limit_price}
    if atype in {"sell_percent", "close_all"}:
        sellable_units = max(0, int(position["quantity_units"] or 0))
        percent = 100.0 if atype == "close_all" else max(0.0, min(float(action.get("percent") or 0), 100.0))
        units = int(sellable_units * percent / 100)
        if units <= 0:
            raise ValueError("workflow sell action has no sellable position")
        return {"side": "sell", "order_type": order_type, "quantity": units_to_quantity(units), "limit_price_points": limit_price}
    return None


def bot_risk_target_order(bot, *, context):
    if not bool(context.get("has_position")):
        return None
    sellable_units = int(context.get("sellable_units") or 0)
    if sellable_units <= 0:
        return None
    stop_loss_raw = bot["stop_loss_percent"] if "stop_loss_percent" in bot.keys() else None
    take_profit_raw = bot["take_profit_percent"] if "take_profit_percent" in bot.keys() else None
    stop_loss_percent = _normalize_optional_risk_percent(stop_loss_raw, name="stop_loss_percent")
    take_profit_percent = _normalize_optional_risk_percent(take_profit_raw, name="take_profit_percent")
    pnl_low_percent = context.get("pnl_low_percent")
    pnl_high_percent = context.get("pnl_high_percent")
    if stop_loss_percent and pnl_low_percent is not None and pnl_low_percent <= -abs(stop_loss_percent):
        return {
            "side": "sell",
            "order_type": "market",
            "quantity": units_to_quantity(sellable_units),
            "limit_price_points": None,
            "reason": "stop_loss",
            "target_percent": stop_loss_percent,
        }
    if take_profit_percent and pnl_high_percent is not None and pnl_high_percent >= abs(take_profit_percent):
        return {
            "side": "sell",
            "order_type": "market",
            "quantity": units_to_quantity(sellable_units),
            "limit_price_points": None,
            "reason": "take_profit",
            "target_percent": take_profit_percent,
        }
    return None


def dca_price_limit_reason(bot, observed_price):
    """Return the user-facing DCA price-band skip reason, if any.

    The limits are deliberately checked *after* DCA stop-loss/take-profit
    handling in ``run_trading_bot_rows``.  A risk target must still be able
    to close an existing position even when the normal periodic buy is
    paused outside the configured band.
    """
    price = float(observed_price or 0)
    if price <= 0:
        return None
    upper = bot["price_upper_limit"] if "price_upper_limit" in bot.keys() else None
    lower = bot["price_lower_limit"] if "price_lower_limit" in bot.keys() else None
    if upper not in (None, "") and price > float(upper):
        return "price_above_limit"
    if lower not in (None, "") and price < float(lower):
        return "price_below_limit"
    return None


def claim_daily_trigger_slot(service, bot):
    """Atomically reserve one UTC-day Workflow trigger slot.

    The scheduler may be entered by an automatic job and a user-triggered
    scan at nearly the same time.  A read-then-write count would let both
    paths pass the cap, so the reservation is one conditional UPDATE inside
    an IMMEDIATE transaction.  ``max_daily_runs=0`` is the backwards-
    compatible unlimited value used by existing bots.
    """
    max_daily_runs = int(bot["max_daily_runs"] or 0) if "max_daily_runs" in bot.keys() else 0
    if max_daily_runs <= 0:
        return True
    today = _utc_now().date().isoformat()
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """
            UPDATE trading_bots
            SET daily_run_date=?,
                daily_run_count=CASE WHEN daily_run_date=? THEN daily_run_count+1 ELSE 1 END,
                updated_at=?
            WHERE id=?
              AND enabled=1
              AND (daily_run_date IS NULL OR daily_run_date<>? OR daily_run_count<?)
            """,
            (today, today, _now_text(), int(bot["id"]), today, max_daily_runs),
        )
        conn.commit()
        return updated.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_daily_trigger_slot(service, bot):
    """Release a reservation only when order placement itself failed."""
    max_daily_runs = int(bot["max_daily_runs"] or 0) if "max_daily_runs" in bot.keys() else 0
    if max_daily_runs <= 0:
        return
    today = _utc_now().date().isoformat()
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE trading_bots
            SET daily_run_count=CASE WHEN daily_run_count>0 THEN daily_run_count-1 ELSE 0 END,
                updated_at=?
            WHERE id=? AND daily_run_date=?
            """,
            (_now_text(), int(bot["id"]), today),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_trading_bots(service, *, actor):
    user_id = service._actor_id(actor)
    if not user_id:
        raise ValueError("login required")
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        market_prices = {
            row["symbol"]: float(_to_decimal(row["manual_price_points"] or 0, name="manual_price_points", minimum=0))
            for row in conn.execute("SELECT symbol, manual_price_points FROM trading_markets").fetchall()
        }
        bots = []
        for row in conn.execute("SELECT * FROM trading_bots WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,)).fetchall():
            bot = _bot_payload_with_budget_meta(service, conn, row)
            current_price = market_prices.get(str(bot.get("market_symbol") or ""), 0)
            try:
                bot["condition_checks"] = bot_condition_checks(service, bot, current_price)
            except Exception:
                bot["condition_checks"] = []
            bots.append(bot)
        runs = [
            service._bot_run_payload(row)
            for row in conn.execute("SELECT * FROM trading_bot_runs WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,)).fetchall()
        ]
        return {"ok": True, "bots": bots, "runs": runs}
    finally:
        conn.close()


def save_trading_bot(service, *, actor, payload, bot_uuid=None):
    user_id = service._actor_id(actor)
    if not user_id:
        raise ValueError("login required")
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        service._assert_writable(conn)
        data = validate_bot_payload(service, conn, payload)
        now = _now_text()
        if bot_uuid:
            existing = conn.execute("SELECT * FROM trading_bots WHERE bot_uuid=?", (str(bot_uuid),)).fetchone()
            if not existing:
                raise ValueError("trading bot not found")
            if int(existing["user_id"]) != int(user_id):
                raise ValueError("cannot update another user's trading bot")
            conn.execute(
                """
                UPDATE trading_bots
                SET bot_type=?, name=?, market_symbol=?, side=?, order_type=?, quantity_text=?,
                    limit_price_points=?, trigger_type=?, trigger_price_points=?,
                    enabled=?, max_runs=?, cooldown_seconds=?, interval_hours=?, budget_points=?,
                    price_upper_limit=?, price_lower_limit=?, max_daily_runs=?,
                    stop_loss_percent=?, take_profit_percent=?, share_parameters=?, workflow_json=?, execution_state_json='{}', last_error='',
                    enabled_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    data["bot_type"], data["name"], data["market_symbol"], data["side"], data["order_type"], data["quantity_text"],
                    data["limit_price_points"], data["trigger_type"], data["trigger_price_points"],
                    1 if data["enabled"] else 0, data["max_runs"], data["cooldown_seconds"],
                    data["interval_hours"], data["budget_points"], data["price_upper_limit"], data["price_lower_limit"], data["max_daily_runs"], data["stop_loss_percent"], data["take_profit_percent"], 1 if data["share_parameters"] else 0, _json_dumps(data["workflow"]) if data["workflow"] else None,
                    now if data["enabled"] and not bool(existing["enabled"]) else (existing["enabled_at"] if data["enabled"] else None),
                    now,
                    existing["id"],
                ),
            )
            bot_id = existing["id"]
            event_type = "TRADING_BOT_UPDATED"
        else:
            cur = conn.execute(
                """
                INSERT INTO trading_bots (
                    bot_uuid, user_id, bot_type, name, market_symbol, side, order_type, quantity_text,
                    limit_price_points, trigger_type, trigger_price_points, enabled,
                    max_runs, run_count, cooldown_seconds, interval_hours, budget_points, price_upper_limit, price_lower_limit, max_daily_runs, stop_loss_percent, take_profit_percent, share_parameters, workflow_json, execution_state_json, enabled_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), user_id, data["bot_type"], data["name"], data["market_symbol"], data["side"], data["order_type"],
                    data["quantity_text"], data["limit_price_points"], data["trigger_type"], data["trigger_price_points"],
                    1 if data["enabled"] else 0, data["max_runs"], data["cooldown_seconds"],
                    data["interval_hours"], data["budget_points"], data["price_upper_limit"], data["price_lower_limit"], data["max_daily_runs"], data["stop_loss_percent"], data["take_profit_percent"], 1 if data["share_parameters"] else 0, _json_dumps(data["workflow"]) if data["workflow"] else None, "{}",
                    now if data["enabled"] else None,
                    now,
                    now,
                ),
            )
            bot_id = cur.lastrowid
            event_type = "TRADING_BOT_CREATED"
        row = conn.execute("SELECT * FROM trading_bots WHERE id=?", (bot_id,)).fetchone()
        service._audit_event(conn, event_type, "trading bot workflow saved", actor=actor, target_user_id=user_id, market_symbol=row["market_symbol"], metadata={"bot_uuid": row["bot_uuid"], "bot_type": row["bot_type"], "trigger_type": row["trigger_type"], "side": row["side"], "order_type": row["order_type"]})
        conn.commit()
        return {"ok": True, "bot": _bot_payload_with_budget_meta(service, conn, row)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_trading_bot_share_parameters(service, *, actor, bot_uuid, share_parameters):
    user_id = service._actor_id(actor)
    if not user_id:
        raise ValueError("login required")
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM trading_bots WHERE bot_uuid=? AND user_id=?",
            (str(bot_uuid or ""), int(user_id)),
        ).fetchone()
        if not row:
            raise ValueError("trading bot not found")
        now = _now_text()
        conn.execute(
            "UPDATE trading_bots SET share_parameters=?, updated_at=? WHERE id=?",
            (1 if share_parameters else 0, now, row["id"]),
        )
        updated = conn.execute("SELECT * FROM trading_bots WHERE id=?", (row["id"],)).fetchone()
        service._audit_event(
            conn,
            "TRADING_BOT_PARAMETER_SHARE_UPDATED",
            "trading bot parameter sharing updated",
            actor=actor,
            target_user_id=user_id,
            market_symbol=updated["market_symbol"],
            metadata={"bot_uuid": updated["bot_uuid"], "share_parameters": bool(share_parameters)},
        )
        conn.commit()
        return {"ok": True, "bot": _bot_payload_with_budget_meta(service, conn, updated)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_trading_bot(service, *, actor, bot_uuid):
    user_id = service._actor_id(actor)
    if not user_id:
        raise ValueError("login required")
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM trading_bots WHERE bot_uuid=?", (str(bot_uuid or ""),)).fetchone()
        if not row:
            raise ValueError("trading bot not found")
        if int(row["user_id"]) != int(user_id):
            raise ValueError("cannot delete another user's trading bot")
        conn.execute("DELETE FROM trading_bots WHERE id=?", (row["id"],))
        service._audit_event(conn, "TRADING_BOT_DELETED", "trading bot workflow deleted", actor=actor, target_user_id=user_id, market_symbol=row["market_symbol"], metadata={"bot_uuid": row["bot_uuid"]})
        conn.commit()
        return {"ok": True, "bot_uuid": row["bot_uuid"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def increase_trading_bot_max_runs(service, *, actor, bot_uuid, delta):
    user_id = service._actor_id(actor)
    if not user_id:
        raise ValueError("login required")
    increment = _to_int(delta, name="delta", minimum=1, maximum=1000)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        service._assert_writable(conn)
        row = conn.execute("SELECT * FROM trading_bots WHERE bot_uuid=?", (str(bot_uuid or ""),)).fetchone()
        if not row:
            raise ValueError("trading bot not found")
        if int(row["user_id"]) != int(user_id):
            raise ValueError("cannot update another user's trading bot")
        if int(row["max_runs"] or 0) >= UNLIMITED_BOT_MAX_RUNS:
            conn.commit()
            return {"ok": True, "bot": _bot_payload_with_budget_meta(service, conn, row), "delta": 0, "unlimited": True}
        next_max_runs = _to_int(int(row["max_runs"] or 0) + increment, name="max_runs", minimum=1, maximum=10000)
        now = _now_text()
        conn.execute(
            "UPDATE trading_bots SET max_runs=?, updated_at=? WHERE id=?",
            (next_max_runs, now, row["id"]),
        )
        updated = conn.execute("SELECT * FROM trading_bots WHERE id=?", (row["id"],)).fetchone()
        service._audit_event(
            conn,
            "TRADING_BOT_MAX_RUNS_INCREASED",
            "trading bot max runs increased",
            actor=actor,
            target_user_id=user_id,
            market_symbol=row["market_symbol"],
            metadata={
                "bot_uuid": row["bot_uuid"],
                "delta": increment,
                "previous_max_runs": int(row["max_runs"] or 0),
                "max_runs": next_max_runs,
            },
        )
        conn.commit()
        return {"ok": True, "bot": _bot_payload_with_budget_meta(service, conn, updated), "delta": increment}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def adjust_trading_bot_budget(service, *, actor, bot_uuid, budget_points=None, delta_points=None):
    user_id = service._actor_id(actor)
    if not user_id:
        raise ValueError("login required")
    if budget_points is None and delta_points is None:
        raise ValueError("budget_points or delta_points is required")
    if budget_points is not None and delta_points is not None:
        raise ValueError("set budget_points or delta_points, not both")
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        service._assert_writable(conn)
        row = conn.execute("SELECT * FROM trading_bots WHERE bot_uuid=?", (str(bot_uuid or ""),)).fetchone()
        if not row:
            raise ValueError("trading bot not found")
        if int(row["user_id"]) != int(user_id):
            raise ValueError("cannot update another user's trading bot")
        current_budget = int(row["budget_points"] or 0)
        if budget_points is not None:
            next_budget = _to_int(budget_points, name="budget_points", minimum=0, maximum=10**12)
            delta = next_budget - current_budget
        else:
            delta = _to_int(delta_points, name="delta_points", minimum=-(10**12), maximum=10**12)
            next_budget = _to_int(current_budget + delta, name="budget_points", minimum=0, maximum=10**12)
        open_frozen = _bot_open_order_frozen_points(conn, row["id"])
        floor = _bot_budget_floor(row, open_frozen)
        bot_type = str(row["bot_type"] or "conditional")
        if bot_type == "dca" and next_budget < floor:
            raise ValueError(f"budget_points cannot be lower than {floor} points")
        if bot_type != "dca" and next_budget != 0 and next_budget < floor:
            raise ValueError(f"workflow budget cannot be lower than {floor} points while bot orders are open")
        now = _now_text()
        conn.execute(
            "UPDATE trading_bots SET budget_points=?, updated_at=? WHERE id=?",
            (next_budget, now, row["id"]),
        )
        updated = conn.execute("SELECT * FROM trading_bots WHERE id=?", (row["id"],)).fetchone()
        service._audit_event(
            conn,
            "TRADING_BOT_BUDGET_UPDATED",
            "trading bot budget updated",
            actor=actor,
            target_user_id=user_id,
            market_symbol=row["market_symbol"],
            metadata={
                "bot_uuid": row["bot_uuid"],
                "bot_type": bot_type,
                "previous_budget_points": current_budget,
                "budget_points": next_budget,
                "delta_points": delta,
                "open_order_frozen_points": open_frozen,
                "minimum_budget_points": floor,
            },
        )
        conn.commit()
        payload = _bot_payload_with_budget_meta(service, conn, updated)
        return {
            "ok": True,
            "bot": payload,
            "delta_points": delta,
            "previous_budget_points": current_budget,
            "budget_points": next_budget,
            "open_order_frozen_points": open_frozen,
            "minimum_budget_points": floor,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_trading_bots(service, *, actor, limit=50):
    user_id = service._actor_id(actor)
    if not user_id:
        raise ValueError("login required")
    limit = _to_int(limit or 50, name="limit", minimum=1, maximum=200)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT b.*, u.username, u.role
            FROM trading_bots b
            JOIN users u ON u.id=b.user_id
            WHERE b.user_id=? AND b.enabled=1 AND b.run_count < b.max_runs
            ORDER BY b.id ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return run_trading_bot_rows(service, rows)


def run_trading_bot_once(service, *, actor, bot_uuid):
    user_id = service._actor_id(actor)
    if not user_id:
        raise ValueError("login required")
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        row = conn.execute(
            """
            SELECT b.*, u.username, u.role
            FROM trading_bots b
            JOIN users u ON u.id=b.user_id
            WHERE b.bot_uuid=? AND b.user_id=?
            LIMIT 1
            """,
            (str(bot_uuid or ""), user_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError("trading bot not found")
    if not bool(row["enabled"]):
        return {"ok": True, "scanned": 1, "triggered": [], "skipped": [{"bot_uuid": row["bot_uuid"], "reason": "disabled"}], "failed": []}
    if not bot_max_runs_has_remaining(row["run_count"], row["max_runs"]):
        return {"ok": True, "scanned": 1, "triggered": [], "skipped": [{"bot_uuid": row["bot_uuid"], "reason": "max_runs_reached"}], "failed": []}
    return run_trading_bot_rows(service, [row])


def run_due_trading_bots(service, *, actor=None, limit=50):
    limit = _to_int(limit or 50, name="limit", minimum=1, maximum=200)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        settings = service._settings_payload(conn)
        if not settings.get("enabled", True):
            return {"ok": True, "enabled": False, "reason": "trading_disabled", "scanned": 0, "triggered": [], "skipped": [], "failed": []}
        if not settings.get("bot_auto_scan_enabled", True):
            return {"ok": True, "enabled": False, "reason": "bot_auto_scan_disabled", "scanned": 0, "triggered": [], "skipped": [], "failed": []}
        active_sql = _active_user_sql(conn)
        rows = conn.execute(
            f"""
            SELECT b.*, u.username, u.role
            FROM trading_bots b
            JOIN users u ON u.id=b.user_id
            WHERE b.enabled=1 AND b.run_count < b.max_runs AND {active_sql}
            ORDER BY b.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    result = run_trading_bot_rows(service, rows)
    result["enabled"] = True
    return result


def run_trading_bot_rows(service, rows):
    scanned = 0
    triggered = []
    skipped = []
    failed = []
    for row in rows:
        scanned += 1
        price_conn = None
        daily_slot_reserved = False
        now_dt = _utc_now()
        workflow_state = _workflow_state(row["execution_state_json"] if "execution_state_json" in row.keys() else None)
        decision = None
        if row["last_run_at"]:
            try:
                last_run = _coerce_utc_datetime(row["last_run_at"])
                if (now_dt - last_run).total_seconds() < int(row["cooldown_seconds"] or 0):
                    skipped.append({"bot_uuid": row["bot_uuid"], "reason": "cooldown"})
                    continue
            except Exception:
                pass
        observed_price = None
        try:
            price_conn = service.get_db()
            service.ensure_schema(price_conn)
            market = service._market(price_conn, row["market_symbol"])
            if not service._is_market_boot_ready(market, conn=price_conn):
                skipped.append({
                    "bot_uuid": row["bot_uuid"],
                    "reason": "market_boot_pending",
                    "detail": f"market {market['symbol']} 尚未收到任何即時價格更新",
                })
                continue
            settings = service._settings_payload(price_conn)
            observed_price, _price_source, price_meta = service._current_market_price_points(price_conn, market, with_meta=True, high_risk=True)
            service._assert_price_meta_allows_high_risk_use(
                price_conn,
                actor=_bot_actor(row),
                market_symbol=market["symbol"],
                usage="trading bot trigger",
                price_meta=price_meta,
            )
            price_window = service._recent_price_window(
                market["symbol"],
                lookback_seconds=max(60, int(settings.get("bot_auto_scan_interval_seconds") or 30) + 5),
                since_time_text=row["last_scan_at"] if "last_scan_at" in row.keys() else None,
                conn=price_conn,
            )
            observed_low = float((price_window or {}).get("low_points") or observed_price)
            observed_high = float((price_window or {}).get("high_points") or observed_price)
            workflow = _json_loads(row["workflow_json"], None) if "workflow_json" in row.keys() else None
            order_payload = None
            context = workflow_live_context(
                service,
                price_conn,
                market=market,
                user_id=int(row["user_id"]),
                observed_price=observed_price,
                observed_low=observed_low,
                observed_high=observed_high,
            )
            if workflow and str(row["bot_type"] or "conditional") == "conditional":
                decision = service._workflow_decision(
                    workflow,
                    context=context,
                    run_count=int(row["run_count"] or 0),
                    last_run_at=row["last_run_at"],
                    execution_state=workflow_state,
                )
                if not decision:
                    skipped_reason = "condition_not_met" if workflow.get("source") == "legacy_condition" else "workflow_not_matched"
                    price_conn.close()
                    price_conn = None
                    record_bot_run(service, row, status="skipped", observed_price=observed_price, error=skipped_reason)
                    skipped.append({"bot_uuid": row["bot_uuid"], "reason": skipped_reason, "observed_price_points": observed_price})
                    continue
                order_payload = workflow_order_from_decision(
                    service,
                    price_conn,
                    user_id=int(row["user_id"]),
                    actor=_bot_actor(row),
                    market=market,
                    decision=decision,
                    price_points=observed_price,
                    budget_points=int(row["budget_points"] or 0),
                    bot_id=int(row["id"]),
                )
                if not order_payload:
                    price_conn.close()
                    price_conn = None
                    record_bot_run(service, row, status="skipped", observed_price=observed_price, error="workflow_hold")
                    skipped.append({"bot_uuid": row["bot_uuid"], "reason": "workflow_hold", "observed_price_points": observed_price})
                    continue
            elif str(row["bot_type"] or "conditional") == "dca":
                order_payload = bot_risk_target_order(row, context=context)
                if order_payload is None:
                    limit_reason = dca_price_limit_reason(row, observed_price)
                    if limit_reason:
                        price_conn.close()
                        price_conn = None
                        record_bot_run(service, row, status="skipped", observed_price=observed_price, error=limit_reason)
                        skipped.append({"bot_uuid": row["bot_uuid"], "reason": limit_reason, "observed_price_points": observed_price})
                        continue
            price_conn.close()
            price_conn = None
            if not workflow and order_payload is None and not bot_trigger_hit(row, observed_price, observed_low=observed_low, observed_high=observed_high):
                record_bot_run(service, row, status="skipped", observed_price=observed_price, error="condition_not_met")
                skipped.append({"bot_uuid": row["bot_uuid"], "reason": "condition_not_met", "observed_price_points": observed_price})
                continue
            quantity_text = row["quantity_text"]
            if str(row["bot_type"] or "conditional") == "dca" and order_payload is None:
                quantity_text = quantity_text_from_budget(
                    budget_points=int(row["budget_points"] or 0),
                    price_points=observed_price,
                    fee_rate_percent=float(market["fee_rate_percent"] or 0),
                )
            if order_payload:
                quantity_text = order_payload["quantity"]
                order_type = order_payload["order_type"]
                side = order_payload["side"]
                limit_price_points = order_payload.get("limit_price_points")
            else:
                order_type = row["order_type"]
                side = row["side"]
                limit_price_points = row["limit_price_points"]
            if str(row["bot_type"] or "conditional") == "conditional":
                if not claim_daily_trigger_slot(service, row):
                    record_bot_run(service, row, status="skipped", observed_price=observed_price, error="daily_max_runs_reached")
                    skipped.append({"bot_uuid": row["bot_uuid"], "reason": "daily_max_runs_reached", "observed_price_points": observed_price})
                    continue
                # Only capped bots need a compensating release when order
                # placement fails.  A successful order consumes the slot.
                daily_slot_reserved = int(row["max_daily_runs"] or 0) > 0 if "max_daily_runs" in row.keys() else False
            result = service.place_order(
                actor=_bot_actor(row),
                market_symbol=row["market_symbol"],
                side=side,
                order_type=order_type,
                quantity=quantity_text,
                limit_price_points=limit_price_points,
            )
            # The trigger has now reached the exchange order path; retain the
            # UTC-day reservation even if later audit recording fails.
            daily_slot_reserved = False
            order_uuid = (result.get("order") or {}).get("order_uuid")
            if workflow and decision:
                action = decision.get("action") or {}
                action_id = decision.get("action_id") or (decision.get("branch") or {}).get("id")
                if action_id:
                    counts = workflow_state.setdefault("branch_step_counts", {})
                    counts[action_id] = int(counts.get(action_id, 0)) + 1
                    if action.get("type") != "close_all":
                        executed = workflow_state.setdefault("executed_action_ids", [])
                        if action_id not in executed:
                            executed.append(action_id)
            record_bot_run(service, row, status="triggered", observed_price=observed_price, order_uuid=order_uuid, execution_state=workflow_state)
            triggered.append({"bot_uuid": row["bot_uuid"], "order_uuid": order_uuid, "observed_price_points": observed_price, "executed": bool(result.get("executed"))})
        except Exception as exc:
            if price_conn is not None:
                try:
                    price_conn.close()
                except Exception:
                    pass
            if daily_slot_reserved:
                try:
                    release_daily_trigger_slot(service, row)
                except Exception:
                    pass
            record_bot_run(service, row, status="failed", observed_price=observed_price, error=str(exc))
            failed.append({"bot_uuid": row["bot_uuid"], "error": str(exc), "observed_price_points": observed_price})
    return {"ok": not failed, "scanned": scanned, "triggered": triggered, "skipped": skipped, "failed": failed}


def record_bot_run(service, bot, *, status, observed_price=None, order_uuid=None, error="", execution_state=None):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        now = _utc_now().isoformat()
        conn.execute(
            """
            INSERT INTO trading_bot_runs (
                run_uuid, bot_id, user_id, market_symbol, trigger_type, trigger_price_points,
                observed_price_points, status, order_uuid, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                int(bot["id"]),
                int(bot["user_id"]),
                bot["market_symbol"],
                bot["trigger_type"],
                bot["trigger_price_points"],
                observed_price,
                status,
                order_uuid,
                str(error or "")[:240],
                now,
            ),
        )
        if status == "triggered":
            if execution_state is not None:
                conn.execute(
                    "UPDATE trading_bots SET run_count=run_count+1, last_run_at=?, execution_state_json=?, last_error='', last_scan_at=?, updated_at=? WHERE id=?",
                    (now, _json_dumps(execution_state), now, now, int(bot["id"])),
                )
            else:
                conn.execute(
                    "UPDATE trading_bots SET run_count=run_count+1, last_run_at=?, last_error='', last_scan_at=?, updated_at=? WHERE id=?",
                    (now, now, now, int(bot["id"])),
                )
        elif status == "failed":
            conn.execute(
                "UPDATE trading_bots SET run_count=run_count+1, last_run_at=?, last_error=?, updated_at=? WHERE id=?",
                (now, str(error or "")[:240], now, int(bot["id"])),
            )
            create_notification_if_enabled(
                conn,
                user_id=int(bot["user_id"]),
                type="trading_bot_failed",
                title="交易機器人執行失敗",
                body=f"{bot['name']} 執行失敗：{str(error or '')[:120]}",
                link="/trading",
            )
        service._audit_event(
            conn,
            "TRADING_BOT_RUN",
            f"trading bot {status}",
            actor={"id": int(bot["user_id"]), "username": bot["username"], "role": bot["role"]},
            target_user_id=int(bot["user_id"]),
            market_symbol=bot["market_symbol"],
            severity="warning" if status == "failed" else "info",
            metadata={"bot_uuid": bot["bot_uuid"], "status": status, "observed_price_points": observed_price, "order_uuid": order_uuid, "error": str(error or "")[:240]},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bot_audit_latest_map_on_conn(conn):
    rows = conn.execute("SELECT * FROM trading_bot_audit_runs ORDER BY id DESC LIMIT 1000").fetchall()
    return bot_audit_latest_map(rows)


def bot_audit_enabled_at_on_row(row):
    return bot_audit_enabled_at(row, now_text=_now_text())


def bot_audit_is_eligible_on_row(row, *, bot_kind, min_enabled_seconds):
    return bot_audit_is_eligible(
        row,
        bot_kind=bot_kind,
        min_enabled_seconds=min_enabled_seconds or TRADING_BOT_AUDIT_MIN_ENABLED_SECONDS,
        now_text=_now_text(),
        enabled_at_func=bot_audit_enabled_at,
    )


def bot_audit_run_findings(service, conn, row, *, bot_kind, min_enabled_seconds):
    findings = []
    state = service._state(conn)
    if bool(state.get("safe_mode")):
        findings.append({
            "severity": "blocker",
            "code": "safe_mode_active",
            "message": "交易系統目前處於 safe mode，機器人結果不可信，需先排除全域交易異常。",
            "metadata": {"reason": state.get("reason") or ""},
        })
    eligible, eligible_reason = bot_audit_is_eligible_on_row(
        row,
        bot_kind=bot_kind,
        min_enabled_seconds=min_enabled_seconds,
    )
    if bot_kind == "trading_bot":
        recent_runs = [
            dict(item)
            for item in conn.execute(
                "SELECT status, error, created_at, order_uuid FROM trading_bot_runs WHERE bot_id=? ORDER BY id DESC LIMIT 10",
                (int(row["id"]),),
            ).fetchall()
        ]
        failed_runs = [item for item in recent_runs if item.get("status") == "failed"]
        if str(row.get("last_error") or "").strip():
            findings.append({
                "severity": "blocker" if failed_runs else "warning",
                "code": "bot_last_error_present",
                "message": f"最近一次 bot 執行留下錯誤：{str(row.get('last_error') or '')[:180]}",
                "metadata": {"last_error": str(row.get("last_error") or "")[:240]},
            })
        if eligible_reason == "aged_24h" and int(row.get("triggered_run_count") or 0) <= 0:
            findings.append({
                "severity": "warning",
                "code": "no_trade_after_24h",
                "message": "機器人啟用已滿 24 小時，但尚未產生任何成交，請檢查條件是否過嚴或市場是否不活躍。",
                "metadata": {"enabled_at": row.get("enabled_at") or row.get("created_at") or ""},
            })
        if len(failed_runs) >= 3 and int(row.get("triggered_run_count") or 0) <= 0:
            findings.append({
                "severity": "blocker",
                "code": "repeated_failed_runs",
                "message": "最近 bot 巡檢多次失敗且沒有成功成交，請先排除錯誤再繼續啟用。",
                "metadata": {"failed_runs": len(failed_runs)},
            })
        elif failed_runs:
            findings.append({
                "severity": "warning",
                "code": "recent_failed_runs",
                "message": f"最近 {len(failed_runs)} 次 bot 巡檢失敗，建議 root 追查執行錯誤。",
                "metadata": {"failed_runs": len(failed_runs)},
            })
    else:
        orders_table, _route_ctx = service._resolve_table("orders", action="bot_audit")
        open_orders = conn.execute(
            "SELECT COUNT(*) AS c FROM trading_grid_orders WHERE grid_bot_id=? AND status='open'",
            (int(row["id"]),),
        ).fetchone()["c"]
        orphan_open_orders = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM trading_grid_orders go
            LEFT JOIN {orders_table} o ON o.order_uuid=go.trading_order_uuid
            WHERE go.grid_bot_id=? AND go.status='open' AND (
                go.trading_order_uuid IS NULL OR o.id IS NULL OR COALESCE(o.status, '') NOT IN ('open', 'partially_filled')
            )
            """,
            (int(row["id"]),),
        ).fetchone()["c"]
        if str(row.get("last_error") or "").strip():
            findings.append({
                "severity": "blocker",
                "code": "grid_last_error_present",
                "message": f"網格機器人最近一次掃描失敗：{str(row.get('last_error') or '')[:180]}",
                "metadata": {"last_error": str(row.get("last_error") or "")[:240]},
            })
        if int(orphan_open_orders or 0) > 0:
            findings.append({
                "severity": "blocker",
                "code": "grid_orphan_open_orders",
                "message": f"仍有 {int(orphan_open_orders)} 筆網格開單找不到對應 trading_orders 或狀態不同步。",
                "metadata": {"orphan_open_orders": int(orphan_open_orders or 0)},
            })
        if bool(row.get("enabled")) and int(open_orders or 0) <= 0:
            findings.append({
                "severity": "warning",
                "code": "grid_has_no_open_orders",
                "message": "網格機器人目前啟用中，但沒有任何有效開單，可能已漏掛單或需要人工檢查。",
                "metadata": {"open_orders": int(open_orders or 0)},
            })
        if eligible_reason == "aged_24h" and int(row.get("total_trades") or 0) <= 0:
            findings.append({
                "severity": "warning",
                "code": "grid_no_trade_after_24h",
                "message": "網格機器人啟用已滿 24 小時，但尚未成交，請檢查網格範圍或市場波動是否不足。",
                "metadata": {"enabled_at": row.get("enabled_at") or row.get("created_at") or ""},
            })
    return bot_audit_result(
        findings=findings,
        eligible=eligible,
        eligible_reason=eligible_reason,
    )


def record_bot_audit_run(service, conn, row, *, bot_kind, audit_result):
    now = _now_text()
    findings = audit_result.get("findings") or []
    run_uuid = str(uuid.uuid4())
    cur = conn.execute(
        """
        INSERT INTO trading_bot_audit_runs (
            run_uuid, bot_kind, bot_uuid, bot_id, user_id, market_symbol,
            audit_status, eligible_reason, findings_json, finding_count,
            warning_count, blocker_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_uuid,
            bot_kind,
            str(row["bot_uuid"]),
            int(row["id"]),
            int(row["user_id"]),
            str(row["market_symbol"]),
            str(audit_result.get("audit_status") or "green"),
            str(audit_result.get("eligible_reason") or ""),
            _json_dumps(findings),
            len(findings),
            int(audit_result.get("warning_count") or 0),
            int(audit_result.get("blocker_count") or 0),
            now,
        ),
    )
    run_id = cur.lastrowid
    for finding in findings:
        conn.execute(
            """
            INSERT INTO trading_bot_audit_findings (
                run_id, severity, code, message, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(run_id),
                str(finding.get("severity") or "warning"),
                str(finding.get("code") or "unknown"),
                str(finding.get("message") or "")[:500],
                _json_dumps(finding.get("metadata") or {}),
                now,
            ),
        )
    if audit_result.get("audit_status") in {"yellow", "red"}:
        status = str(audit_result.get("audit_status") or "green")
        label = bot_audit_label(status)
        display_symbol = service._market_display_symbol_on_conn(conn, row.get("market_symbol"))
        notice = bot_audit_notification_payload(
            row=row,
            status=status,
            label=label,
            display_symbol=display_symbol,
        )
        create_trading_root_notification(
            conn,
            notification_type=notice["root_notification_type"],
            title=notice["root_title"],
            body=notice["body"],
            once=True,
            create_root_notification=create_root_notification_if_enabled,
        )
        create_trading_user_notification(
            conn,
            user_id=int(row["user_id"]),
            notification_type=notice["user_notification_type"],
            title=notice["user_title"],
            body=notice["body"],
            create_notification=create_notification_if_enabled,
        )
    service._audit_event(
        conn,
        "TRADING_BOT_AUDIT_RUN",
        "trading bot audit completed",
        actor={"id": None, "username": "system", "role": "system"},
        target_user_id=int(row["user_id"]),
        market_symbol=str(row["market_symbol"]),
        severity="warning" if audit_result.get("audit_status") in {"yellow", "red"} else "info",
        metadata={
            "bot_kind": bot_kind,
            "bot_uuid": str(row["bot_uuid"]),
            "audit_status": str(audit_result.get("audit_status") or "green"),
            "finding_count": len(findings),
            "warning_count": int(audit_result.get("warning_count") or 0),
            "blocker_count": int(audit_result.get("blocker_count") or 0),
        },
    )
    return run_uuid


def bot_audit_candidates(conn, *, limit):
    active_sql = _active_user_sql(conn)
    bot_rows = [
        {
            **dict(row),
            "bot_kind": "trading_bot",
            "triggered_run_count": row["triggered_run_count"],
        }
        for row in conn.execute(
            f"""
            SELECT b.*, u.username, u.role,
                   (SELECT COUNT(*) FROM trading_bot_runs r WHERE r.bot_id=b.id AND r.status='triggered') AS triggered_run_count
            FROM trading_bots b
            JOIN users u ON u.id=b.user_id
            WHERE {active_sql}
            ORDER BY b.enabled DESC, COALESCE(b.enabled_at, b.created_at, b.updated_at) ASC, b.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]
    grid_rows = [
        {**dict(row), "bot_kind": "grid_bot"}
        for row in conn.execute(
            f"""
            SELECT g.*, u.username, u.role
            FROM trading_grid_bots g
            JOIN users u ON u.id=g.user_id
            WHERE {active_sql}
            ORDER BY g.enabled DESC, COALESCE(g.enabled_at, g.created_at, g.updated_at) ASC, g.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]
    return [*bot_rows, *grid_rows]


def bot_audit_dashboard_on_conn(service, conn, *, limit, settings=None):
    settings = settings or service._settings_payload(conn)
    latest_map = bot_audit_latest_map_on_conn(conn)
    min_enabled_seconds = int(settings.get("bot_audit_min_enabled_seconds") or TRADING_BOT_AUDIT_MIN_ENABLED_SECONDS)
    items = []
    summary = {"unaudited": 0, "green": 0, "yellow": 0, "red": 0}
    for row in bot_audit_candidates(conn, limit=_to_int(limit, name="limit", minimum=1, maximum=300)):
        latest = latest_map.get((row["bot_kind"], str(row["bot_uuid"])))
        eligible, eligible_reason = bot_audit_is_eligible_on_row(
            row,
            bot_kind=row["bot_kind"],
            min_enabled_seconds=min_enabled_seconds,
        )
        audit_status = str((latest or {}).get("audit_status") or "unaudited")
        increment_audit_summary(summary, audit_status)
        open_order_count = 0
        if row["bot_kind"] != "trading_bot":
            open_order_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM trading_grid_orders WHERE grid_bot_id=? AND status='open'",
                    (int(row["id"]),),
                ).fetchone()["c"]
            )
        item = build_bot_audit_dashboard_item(
            row=row,
            latest=latest,
            eligible=eligible,
            eligible_reason=eligible_reason,
            eligible_reason_label=bot_audit_eligibility_reason_label(eligible_reason),
            audit_label=bot_audit_label(audit_status),
            open_order_count=open_order_count,
        )
        items.append(item)
    recent_runs = [
        dict(row)
        for row in conn.execute("SELECT * FROM trading_bot_audit_runs ORDER BY id DESC LIMIT 80").fetchall()
    ]
    return {
        "ok": True,
        "settings": {
            "bot_audit_enabled": settings.get("bot_audit_enabled", True),
            "bot_audit_interval_seconds": settings.get("bot_audit_interval_seconds", TRADING_BOT_AUDIT_INTERVAL_SECONDS),
            "bot_audit_limit": settings.get("bot_audit_limit", TRADING_BOT_AUDIT_LIMIT),
            "bot_audit_min_enabled_seconds": settings.get("bot_audit_min_enabled_seconds", TRADING_BOT_AUDIT_MIN_ENABLED_SECONDS),
        },
        "summary": summary,
        "items": items,
        "recent_runs": recent_runs,
    }


def run_due_bot_audits(service, *, actor=None, limit=0, force=False):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        settings = service._settings_payload(conn)
        if not settings.get("bot_audit_enabled", True) and not force:
            return {"ok": True, "enabled": False, "reason": "audit_disabled", "scanned": 0, "audited": [], "skipped": []}
        limit = _to_int(limit or settings.get("bot_audit_limit") or TRADING_BOT_AUDIT_LIMIT, name="bot_audit_limit", minimum=1, maximum=200)
        interval_seconds = int(settings.get("bot_audit_interval_seconds") or TRADING_BOT_AUDIT_INTERVAL_SECONDS)
        min_enabled_seconds = int(settings.get("bot_audit_min_enabled_seconds") or TRADING_BOT_AUDIT_MIN_ENABLED_SECONDS)
        latest_map = bot_audit_latest_map_on_conn(conn)
        audited = []
        skipped = []
        for row in bot_audit_candidates(conn, limit=limit):
            latest = latest_map.get((row["bot_kind"], str(row["bot_uuid"])))
            audit_result = bot_audit_run_findings(
                service,
                conn,
                row,
                bot_kind=row["bot_kind"],
                min_enabled_seconds=min_enabled_seconds,
            )
            if not audit_result["eligible"]:
                skipped.append({
                    "bot_kind": row["bot_kind"],
                    "bot_uuid": row["bot_uuid"],
                    "reason": audit_result["eligible_reason"],
                })
                continue
            if not force and latest:
                try:
                    last_dt = datetime.fromisoformat(str(latest["created_at"]))
                except Exception:
                    last_dt = None
                if last_dt and (datetime.fromisoformat(_now_text()) - last_dt).total_seconds() < interval_seconds:
                    skipped.append({
                        "bot_kind": row["bot_kind"],
                        "bot_uuid": row["bot_uuid"],
                        "reason": "interval_not_elapsed",
                    })
                    continue
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            run_uuid = record_bot_audit_run(
                service,
                conn,
                row,
                bot_kind=row["bot_kind"],
                audit_result=audit_result,
            )
            conn.commit()
            audited.append({
                "run_uuid": run_uuid,
                "bot_kind": row["bot_kind"],
                "bot_uuid": row["bot_uuid"],
                "audit_status": audit_result["audit_status"],
                "finding_count": len(audit_result["findings"]),
            })
        return {
            "ok": True,
            "enabled": True,
            "scanned": len(audited) + len(skipped),
            "audited": audited,
            "skipped": skipped,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_bot_audit_dashboard(service, *, limit=100):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        settings = service._settings_payload(conn)
        return bot_audit_dashboard_on_conn(service, conn, limit=limit, settings=settings)
    finally:
        conn.close()
