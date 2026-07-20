import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone


def _now_text():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_utc_client_time(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith("Z") or "+" in text[10:] or "-" in text[10:]:
        return text
    return f"{text}Z"


def ensure_share_access_event_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS share_access_events (
            id TEXT PRIMARY KEY,
            share_type TEXT NOT NULL,
            share_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            ip TEXT,
            user_agent TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_share_access_events_share_created
        ON share_access_events(share_type, share_id, created_at DESC)
        """
    )


def log_share_access_event(conn, *, share_type, share_id, event_type="opened", ip="", user_agent=""):
    share_type = str(share_type or "").strip()
    share_id = str(share_id or "").strip()
    if not share_type or not share_id:
        return None
    ensure_share_access_event_schema(conn)
    event_id = secrets.token_urlsafe(18)
    created_at = _now_text()
    conn.execute(
        """
        INSERT INTO share_access_events (
            id, share_type, share_id, event_type, ip, user_agent, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            share_type,
            share_id,
            str(event_type or "opened").strip() or "opened",
            str(ip or "").strip(),
            str(user_agent or "").strip(),
            created_at,
        ),
    )
    return {
        "id": event_id,
        "share_type": share_type,
        "share_id": share_id,
        "event_type": str(event_type or "opened").strip() or "opened",
        "ip": str(ip or "").strip(),
        "user_agent": str(user_agent or "").strip(),
        "created_at": created_at,
    }


def log_share_access_event_once(conn, *, share_type, share_id, dedupe_key, event_type="opened", ip="", user_agent=""):
    share_type = str(share_type or "").strip()
    share_id = str(share_id or "").strip()
    dedupe_key = str(dedupe_key or "").strip()
    if not share_type or not share_id or not dedupe_key:
        return None, False
    ensure_share_access_event_schema(conn)
    event_type = str(event_type or "opened").strip() or "opened"
    event_id = hashlib.sha256(
        f"{share_type}\0{share_id}\0{event_type}\0{dedupe_key}".encode("utf-8")
    ).hexdigest()
    created_at = _now_text()
    try:
        conn.execute(
            """
            INSERT INTO share_access_events (
                id, share_type, share_id, event_type, ip, user_agent, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                share_type,
                share_id,
                event_type,
                str(ip or "").strip(),
                str(user_agent or "").strip(),
                created_at,
            ),
        )
    except sqlite3.IntegrityError:
        row = conn.execute(
            """
            SELECT id, share_type, share_id, event_type, ip, user_agent, created_at
            FROM share_access_events
            WHERE id=?
            """,
            (event_id,),
        ).fetchone()
        if not row:
            return None, False
        return {
            "id": row["id"],
            "share_type": row["share_type"],
            "share_id": row["share_id"],
            "event_type": row["event_type"],
            "ip": row["ip"] or "",
            "user_agent": row["user_agent"] or "",
            "created_at": _as_utc_client_time(row["created_at"]),
        }, False
    return {
        "id": event_id,
        "share_type": share_type,
        "share_id": share_id,
        "event_type": event_type,
        "ip": str(ip or "").strip(),
        "user_agent": str(user_agent or "").strip(),
        "created_at": created_at,
    }, True


def list_share_access_events(conn, *, share_type, share_id, limit=50):
    share_type = str(share_type or "").strip()
    share_id = str(share_id or "").strip()
    if not share_type or not share_id:
        return []
    ensure_share_access_event_schema(conn)
    try:
        limit = max(1, min(int(limit), 200))
    except Exception:
        limit = 50
    events = []
    for row in conn.execute(
        """
        SELECT event_type, ip, user_agent, created_at
        FROM share_access_events
        WHERE share_type=? AND share_id=?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (share_type, share_id, limit),
    ).fetchall():
        item = dict(row)
        item["created_at"] = _as_utc_client_time(item.get("created_at"))
        events.append(item)
    return events
