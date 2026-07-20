"""Pure trading bot-audit helpers."""

from datetime import datetime, timezone


def bot_audit_latest_map(rows):
    latest = {}
    for row in rows:
        key = (str(row["bot_kind"]), str(row["bot_uuid"]))
        if key not in latest:
            latest[key] = dict(row)
    return latest


def bot_audit_enabled_at(row, *, now_text):
    raw = row.get("enabled_at") or row.get("updated_at") or row.get("created_at") or ""
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return datetime.fromisoformat(now_text)


def bot_audit_is_eligible(row, *, bot_kind, min_enabled_seconds, now_text, enabled_at_func):
    if not bool(row.get("enabled")):
        return False, "disabled"
    enabled_at = enabled_at_func(row, now_text=now_text)
    now_value = datetime.fromisoformat(now_text)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=timezone.utc)
    if enabled_at.tzinfo is None:
        enabled_at = enabled_at.replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((now_value - enabled_at).total_seconds()))
    has_trade = int(row.get("triggered_run_count") or 0) > 0 if bot_kind == "trading_bot" else int(row.get("total_trades") or 0) > 0
    if has_trade:
        return True, "has_trade"
    if age_seconds >= int(min_enabled_seconds or 0):
        return True, "aged_24h"
    return False, "awaiting_first_trade"


def bot_audit_result(*, findings, eligible, eligible_reason):
    blocker_count = sum(1 for item in findings if item["severity"] == "blocker")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    audit_status = "red" if blocker_count else ("yellow" if warning_count else "green")
    return {
        "eligible": eligible,
        "eligible_reason": eligible_reason,
        "audit_status": audit_status,
        "findings": findings,
        "blocker_count": blocker_count,
        "warning_count": warning_count,
    }


def build_bot_audit_dashboard_item(
    *,
    row,
    latest,
    eligible,
    eligible_reason,
    eligible_reason_label,
    audit_label,
    open_order_count=0,
):
    audit_status = str((latest or {}).get("audit_status") or "unaudited")
    item = {
        "bot_kind": row["bot_kind"],
        "bot_uuid": str(row["bot_uuid"]),
        "name": str(row.get("name") or row.get("market_symbol") or ""),
        "market_symbol": str(row.get("market_symbol") or ""),
        "display_symbol": str(row.get("market_symbol") or "").replace("/POINTS", "/USDT"),
        "user_id": int(row["user_id"]),
        "username": str(row.get("username") or ""),
        "enabled": bool(row.get("enabled")),
        "enabled_at": row.get("enabled_at") or row.get("created_at") or "",
        "eligible": bool(eligible),
        "eligible_reason": eligible_reason,
        "eligible_reason_label": eligible_reason_label,
        "audit_status": audit_status,
        "audit_label": audit_label,
        "last_audited_at": (latest or {}).get("created_at") or "",
        "warning_count": int((latest or {}).get("warning_count") or 0),
        "blocker_count": int((latest or {}).get("blocker_count") or 0),
        "finding_count": int((latest or {}).get("finding_count") or 0),
        "last_error": str(row.get("last_error") or "")[:240],
    }
    if row["bot_kind"] == "trading_bot":
        item["triggered_run_count"] = int(row.get("triggered_run_count") or 0)
        item["run_count"] = int(row.get("run_count") or 0)
    else:
        item["total_trades"] = int(row.get("total_trades") or 0)
        item["open_order_count"] = int(open_order_count or 0)
    return item


def increment_audit_summary(summary, audit_status):
    summary[audit_status] = summary.get(audit_status, 0) + 1
    return summary
