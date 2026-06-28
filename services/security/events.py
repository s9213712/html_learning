import sqlite3
import os
import threading
import time
from datetime import datetime, timedelta

from services.system.notifications import create_root_notification_if_enabled

_STATE = {
    "get_db": None,
    "get_system_settings": None,
    "audit": None,
    "is_ip_blocking_enabled": None,
}

_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_BUCKETS = {}
_USER_RATE_LIMIT_BUCKETS = {}
_CLEANUP_LOCK = threading.Lock()
_LAST_EVENT_CLEANUP_AT = 0.0
_EVENT_RETENTION_DAYS = 7
_EVENT_CLEANUP_INTERVAL_SECONDS = 300
_ROOT_NOTIFICATION_BURST_WINDOW_SECONDS = 300
SECURITY_EVENT_TYPES = {
    "login_fail",
    "ip_block",
    "rate_limit",
    "403_access",
    "feature_disabled",
    "csrf_fail",
    "permission_denied",
    "chain_mode_violation",
    "session_revoked",
    "login_location_suspicious",
}


def _ensure_ip_blocks_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS security_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT,
            ip_address  TEXT,
            target_user TEXT,
            detail      TEXT,
            created_at  TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ip_blocks (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address     TEXT NOT NULL UNIQUE,
            blocked_until  TEXT NOT NULL,
            reason         TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ip_blocks_ip ON ip_blocks(ip_address)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ip_blocks_until ON ip_blocks(blocked_until)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_event_type_ip_time ON security_events(event_type, ip_address, created_at)")


def _capacity_probe_unlimited():
    return str(os.environ.get("HACKME_CAPACITY_PROBE_UNLIMITED") or "").strip().lower() in {"1", "true", "yes", "on"}
ROOT_NOTIFICATION_EVENT_TYPES = {
    "ip_block",
    "rate_limit",
    "403_access",
    "feature_disabled",
    "csrf_fail",
    "permission_denied",
    "session_revoked",
    "login_location_suspicious",
}
ROOT_NOTIFICATION_BURST_EVENT_TYPES = {
    "ip_block",
    "rate_limit",
    "403_access",
    "feature_disabled",
    "csrf_fail",
    "permission_denied",
}

SECURITY_EVENT_LABELS = {
    "login_fail": "登入失敗",
    "ip_block": "IP 已被封鎖",
    "rate_limit": "請求過於頻繁",
    "403_access": "被拒絕的越權存取",
    "feature_disabled": "存取已關閉的功能",
    "csrf_fail": "CSRF 安全驗證失敗",
    "permission_denied": "權限不足的操作",
    "chain_mode_violation": "PointsChain 模式保護",
    "session_revoked": "登入 session 已被撤銷",
    "login_location_suspicious": "疑似異常登入位置",
}

SECURITY_EVENT_ADVICE = {
    "login_fail": "若同一來源持續嘗試登入，請檢查是否需要封鎖 IP 或要求帳號改密碼。",
    "ip_block": "系統已依規則封鎖該來源；請確認是否為誤判或攻擊流量。",
    "rate_limit": "請查看安全中心的近期事件，確認是否為爬蟲、腳本或異常使用。",
    "403_access": "請確認該帳號是否嘗試存取不應開放的管理或 root 功能。",
    "feature_disabled": "使用者嘗試使用目前已關閉的功能；若是正常需求，請到安全中心開啟。",
    "csrf_fail": "若大量出現，可能是舊頁面、多分頁、腳本請求或跨站請求造成，請檢查來源。",
    "permission_denied": "請確認帳號權限、伺服器模式與相關功能開關是否符合預期。",
    "chain_mode_violation": "非 production 模式下拒絕寫入 PointsChain 是預期保護；若 production 出現此事件，請檢查 Server Mode 設定。",
    "session_revoked": "這通常代表登出、逾時、單一登入限制或帳號安全策略生效。",
    "login_location_suspicious": "請確認是否為帳號本人登入；必要時要求重設密碼。",
}


def _humanize_security_detail(event_type, detail):
    text = str(detail or "").strip()
    if not text:
        return "無額外細節"
    if text == "single_session_logout":
        return "因單一登入限制，舊 session 已被登出"
    if text == "idle_timeout":
        return "因閒置逾時，session 已被登出"
    if text.startswith("blocked_until="):
        return f"封鎖期限：{text.split('=', 1)[1].strip()}"
    if text.startswith("path="):
        return f"被拒絕路徑：{text.split('=', 1)[1].strip()}"
    if text.startswith("limit=") and ",window=" in text:
        limit = text.split("limit=", 1)[1].split(",", 1)[0]
        window = text.split("window=", 1)[1]
        return f"觸發速率限制：{limit} 次 / {window} 秒"
    if event_type == "feature_disabled" and text.startswith("feature_"):
        return f"被存取的功能開關：{text}"
    if event_type == "chain_mode_violation" and text.startswith("action=") and ",mode=" in text:
        action = text.split("action=", 1)[1].split(",", 1)[0].strip()
        mode = text.split(",mode=", 1)[1].strip().strip("'\"")
        return f"PointsChain 寫入被 Server Mode 保護阻擋：{action}，目前模式：{mode or '-'}"
    if " " in text and text.split(" ", 1)[0].upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        method, path = text.split(" ", 1)
        return f"請求：{method.upper()} {path}"
    return text


def format_root_security_notification(event_type, ip, target_user=None, detail=""):
    normalized_type = normalize_security_event_type(event_type)
    label = SECURITY_EVENT_LABELS.get(normalized_type, "安全事件")
    lines = [
        f"{label}。",
        f"來源 IP：{ip or '-'}",
    ]
    if target_user:
        lines.append(f"相關帳號：{target_user}")
    lines.extend([
        f"事件細節：{_humanize_security_detail(normalized_type, detail)}",
        "處理狀態：系統已記錄此事件，並依目前安全設定放行或阻擋。",
    ])
    if normalized_type in ROOT_NOTIFICATION_BURST_EVENT_TYPES:
        lines.append("通知彙總：同來源短時間重複事件只顯示一則通知，完整紀錄請到安全中心查看。")
    lines.append(f"建議處理：{SECURITY_EVENT_ADVICE.get(normalized_type, '請到安全中心查看近期事件與伺服器日誌。')}")
    return f"安全警訊：{label}", "\n".join(lines)


def _should_create_root_notification(event_type, detail=""):
    normalized_type = normalize_security_event_type(event_type)
    if normalized_type != "session_revoked":
        return normalized_type in ROOT_NOTIFICATION_EVENT_TYPES
    detail_text = str(detail or "").strip()
    return detail_text not in {"idle_timeout", "single_session_logout", "user_sessions_revoked"}


def _root_notification_recently_sent(conn, event_type, ip, created_at):
    normalized_type = normalize_security_event_type(event_type)
    if normalized_type not in ROOT_NOTIFICATION_BURST_EVENT_TYPES:
        return False
    try:
        event_dt = datetime.fromisoformat(str(created_at))
    except Exception:
        event_dt = datetime.now()
    cutoff = (event_dt - timedelta(seconds=_ROOT_NOTIFICATION_BURST_WINDOW_SECONDS)).isoformat()
    try:
        row = conn.execute(
            """
            SELECT id FROM security_events
            WHERE event_type=? AND ip_address=? AND created_at>=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (normalized_type, ip or "-", cutoff),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def configure_security_events_service(*, get_db, get_system_settings, audit, is_ip_blocking_enabled):
    _STATE.update({
        "get_db": get_db,
        "get_system_settings": get_system_settings,
        "audit": audit,
        "is_ip_blocking_enabled": is_ip_blocking_enabled,
    })


def _parse_blocked_until(detail):
    if not detail:
        return None
    prefix = "blocked_until="
    if prefix not in detail:
        return None
    raw = detail.split(prefix, 1)[1].strip()
    if " " in raw:
        raw = raw.split(" ", 1)[0]
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _maybe_cleanup_old_events():
    global _LAST_EVENT_CLEANUP_AT
    now_monotonic = time.monotonic()
    if now_monotonic - _LAST_EVENT_CLEANUP_AT < _EVENT_CLEANUP_INTERVAL_SECONDS:
        return
    with _CLEANUP_LOCK:
        if now_monotonic - _LAST_EVENT_CLEANUP_AT < _EVENT_CLEANUP_INTERVAL_SECONDS:
            return
        conn = _STATE["get_db"]()
        try:
            _ensure_ip_blocks_schema(conn)
            cutoff = (datetime.now() - timedelta(days=_EVENT_RETENTION_DAYS)).isoformat()
            now_iso = datetime.now().isoformat()
            try:
                conn.execute("DELETE FROM security_events WHERE created_at<?", (cutoff,))
            except sqlite3.OperationalError:
                return
            try:
                conn.execute("DELETE FROM ip_blocks WHERE blocked_until<?", (now_iso,))
            except Exception:
                pass
            conn.commit()
        finally:
            conn.close()
        _LAST_EVENT_CLEANUP_AT = now_monotonic


def _check_window_limit(bucket, key, *, max_req, window_sec):
    now = time.time()
    cutoff = now - window_sec
    with _RATE_LIMIT_LOCK:
        recent = [ts for ts in bucket.get(key, []) if ts >= cutoff]
        blocked = len(recent) >= max_req
        if not blocked:
            recent.append(now)
        if recent:
            bucket[key] = recent
        else:
            bucket.pop(key, None)
    return blocked, {"count": len(recent), "limit": max_req}


def normalize_security_event_type(event_type):
    event_type = str(event_type or "").strip().lower()
    return event_type if event_type in SECURITY_EVENT_TYPES else "permission_denied"


def record_security_event(event_type, ip, target_user=None, detail="", created_at=None):
    if not callable(_STATE.get("get_db")):
        return
    _maybe_cleanup_old_events()
    normalized_type = normalize_security_event_type(event_type)
    event_created_at = created_at or datetime.now().isoformat()
    conn = _STATE["get_db"]()
    try:
        _ensure_ip_blocks_schema(conn)
        suppress_root_notification = _root_notification_recently_sent(
            conn,
            normalized_type,
            ip,
            event_created_at,
        )
        conn.execute(
            "INSERT INTO security_events (event_type, ip_address, target_user, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                normalized_type,
                ip or "-",
                target_user,
                detail or "",
                event_created_at,
            ),
        )
        if (not suppress_root_notification) and _should_create_root_notification(normalized_type, detail):
            title, body = format_root_security_notification(normalized_type, ip, target_user=target_user, detail=detail)
            create_root_notification_if_enabled(
                conn,
                type="root_security_alert",
                title=title,
                body=body,
                link="/security",
                once=True,
            )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


def is_ip_blocked(ip):
    if not _STATE["is_ip_blocking_enabled"]():
        return False
    _maybe_cleanup_old_events()
    conn = _STATE["get_db"]()
    try:
        _ensure_ip_blocks_schema(conn)
        row = conn.execute(
            "SELECT blocked_until FROM ip_blocks WHERE ip_address=? LIMIT 1",
            (ip,)
        ).fetchone()
        if row:
            try:
                until = datetime.fromisoformat(row["blocked_until"])
                if datetime.now() < until:
                    return True
                conn.execute("DELETE FROM ip_blocks WHERE ip_address=?", (ip,))
                conn.commit()
                return False
            except Exception:
                conn.execute("DELETE FROM ip_blocks WHERE ip_address=?", (ip,))
                conn.commit()
                return False
        legacy_row = conn.execute(
            "SELECT detail FROM security_events WHERE event_type='ip_block' AND ip_address=? ORDER BY created_at DESC LIMIT 1",
            (ip,)
        ).fetchone()
        if not legacy_row:
            return False
        until = _parse_blocked_until(legacy_row["detail"])
        return bool(until and datetime.now() < until)
    finally:
        conn.close()


def block_ip(ip, minutes=10, reason="multiple failures"):
    if ip == "127.0.0.1" or ip == "::1":
        return
    blocked_until = (datetime.now() + timedelta(minutes=minutes)).isoformat()
    conn = _STATE["get_db"]()
    try:
        _ensure_ip_blocks_schema(conn)
        conn.execute(
            "INSERT INTO ip_blocks (ip_address, blocked_until, reason, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(ip_address) DO UPDATE SET blocked_until=excluded.blocked_until, reason=excluded.reason, created_at=excluded.created_at",
            (ip, blocked_until, reason, datetime.now().isoformat())
        )
        conn.commit()
    finally:
        conn.close()
    record_security_event("ip_block", ip, detail=f"blocked_until={blocked_until}")
    _maybe_cleanup_old_events()


def record_login_failure(ip, username="", ua="-", detail="-", lock_on=3):
    settings = _STATE["get_system_settings"]()
    lock_limit = max(1, int(settings.get("max_login_failures", lock_on or 3)))
    block_minutes = max(1, int(settings.get("block_duration_minutes", 10)))
    _maybe_cleanup_old_events()
    conn = _STATE["get_db"]()
    try:
        conn.execute(
            "INSERT INTO security_events (event_type, ip_address, target_user, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (normalize_security_event_type("login_fail"), ip, username, detail, datetime.now().isoformat())
        )
        since = (datetime.now() - timedelta(minutes=10)).isoformat()
        count = conn.execute(
            "SELECT COUNT(*) as c FROM security_events WHERE event_type='login_fail' AND ip_address=? AND created_at>=?",
            (ip, since)
        ).fetchone()["c"]
        conn.commit()
    finally:
        conn.close()

    _STATE["audit"]("LOGIN_FAIL", ip, username, ua=ua, detail=detail)

    if count >= lock_limit and _STATE["is_ip_blocking_enabled"]():
        block_ip(ip, block_minutes, f"{count} failures → {block_minutes} min block")
        _STATE["audit"]("LOGIN_IP_BLOCKED", ip, username, ua=ua, detail=f"{count} failures → {block_minutes} min block")
    return count


def clear_failed_logins(ip):
    conn = _STATE["get_db"]()
    try:
        conn.execute("DELETE FROM security_events WHERE event_type='login_fail' AND ip_address=?", (ip,))
        conn.commit()
    finally:
        conn.close()


def is_rate_limited(ip, max_req=30, window_sec=60):
    if _capacity_probe_unlimited():
        return False, {"count": 0, "limit": max_req, "disabled": "capacity_probe_unlimited"}
    blocked, info = _check_window_limit(_RATE_LIMIT_BUCKETS, str(ip), max_req=max_req, window_sec=window_sec)
    if blocked:
        record_security_event("rate_limit", ip, detail=f"limit={max_req},window={window_sec}")
    return blocked, info


def check_user_rate_limit(user_id, action, max_req=10, window_sec=3600):
    if _capacity_probe_unlimited():
        return False, {"count": 0, "limit": max_req, "disabled": "capacity_probe_unlimited"}
    key = (int(user_id), str(action))
    blocked, info = _check_window_limit(_USER_RATE_LIMIT_BUCKETS, key, max_req=max_req, window_sec=window_sec)
    if blocked:
        record_security_event(
            "rate_limit",
            "-",
            target_user=str(user_id),
            detail=f"action={action},limit={max_req},window={window_sec}",
        )
    return blocked, info


def record_403_access(ip, path, username="-"):
    record_security_event("403_access", ip, target_user=username, detail=f"path={path}")
