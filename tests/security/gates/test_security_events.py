import sqlite3

from services.security import events as security_events


def _get_db_factory(db_path):
    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return get_db


def test_record_security_event_normalizes_event_types(tmp_path):
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            target_user TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    original_state = dict(security_events._STATE)
    original_cleanup_at = security_events._LAST_EVENT_CLEANUP_AT
    try:
        security_events._LAST_EVENT_CLEANUP_AT = 10**12
        security_events.configure_security_events_service(
            get_db=_get_db_factory(str(db_path)),
            get_system_settings=lambda: {},
            audit=lambda *args, **kwargs: None,
            is_ip_blocking_enabled=lambda: False,
        )

        security_events.record_security_event(
            "feature_disabled",
            "127.0.0.1",
            target_user="alice",
            detail="feature_chat_enabled",
        )
        security_events.record_security_event("unknown_event", "127.0.0.1")
    finally:
        security_events._STATE.clear()
        security_events._STATE.update(original_state)
        security_events._LAST_EVENT_CLEANUP_AT = original_cleanup_at

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT event_type, ip_address, target_user, detail FROM security_events ORDER BY id"
    ).fetchall()
    conn.close()

    assert rows[0] == ("feature_disabled", "127.0.0.1", "alice", "feature_chat_enabled")
    assert rows[1][0] == "permission_denied"


def test_root_security_event_creates_root_notification(tmp_path):
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE
        );
        CREATE TABLE security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            target_user TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'root')")
    conn.execute("INSERT INTO users (id, username) VALUES (2, 'alice')")
    conn.commit()
    conn.close()

    original_state = dict(security_events._STATE)
    original_cleanup_at = security_events._LAST_EVENT_CLEANUP_AT
    try:
        security_events._LAST_EVENT_CLEANUP_AT = 10**12
        security_events.configure_security_events_service(
            get_db=_get_db_factory(str(db_path)),
            get_system_settings=lambda: {},
            audit=lambda *args, **kwargs: None,
            is_ip_blocking_enabled=lambda: False,
        )

        security_events.record_security_event(
            "csrf_fail",
            "10.0.0.5",
            target_user="alice",
            detail="POST /api/example",
        )
    finally:
        security_events._STATE.clear()
        security_events._STATE.update(original_state)
        security_events._LAST_EVENT_CLEANUP_AT = original_cleanup_at

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        note = conn.execute("SELECT * FROM notifications WHERE user_id=1 AND type='root_security_alert'").fetchone()
        assert note is not None
        assert note["title"] == "安全警訊：CSRF 安全驗證失敗"
        assert "CSRF 安全驗證失敗" in note["body"]
        assert "來源 IP：10.0.0.5" in note["body"]
        assert "相關帳號：alice" in note["body"]
        assert "請求：POST /api/example" in note["body"]
        assert "csrf_fail" not in note["body"]
    finally:
        conn.close()


def test_chain_mode_violation_is_recorded_without_root_alert(tmp_path):
    from services.system.notifications import ensure_notifications_schema

    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE
        );
        CREATE TABLE security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            target_user TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'root')")
    ensure_notifications_schema(conn)
    conn.commit()
    conn.close()

    original_state = dict(security_events._STATE)
    original_cleanup_at = security_events._LAST_EVENT_CLEANUP_AT
    try:
        security_events._LAST_EVENT_CLEANUP_AT = 10**12
        security_events.configure_security_events_service(
            get_db=_get_db_factory(str(db_path)),
            get_system_settings=lambda: {},
            audit=lambda *args, **kwargs: None,
            is_ip_blocking_enabled=lambda: False,
        )

        security_events.record_security_event(
            "chain_mode_violation",
            "127.0.0.1",
            target_user="-",
            detail="action=force_seal_block,mode='dev_ready'",
        )
    finally:
        security_events._STATE.clear()
        security_events._STATE.update(original_state)
        security_events._LAST_EVENT_CLEANUP_AT = original_cleanup_at

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        event = conn.execute("SELECT * FROM security_events ORDER BY id DESC LIMIT 1").fetchone()
        alert_count = conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE user_id=1 AND type='root_security_alert'"
        ).fetchone()["c"]
        assert event["event_type"] == "chain_mode_violation"
        assert event["detail"] == "action=force_seal_block,mode='dev_ready'"
        assert alert_count == 0
    finally:
        conn.close()


def test_csrf_root_notifications_are_burst_throttled_but_events_are_kept(tmp_path):
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE
        );
        CREATE TABLE security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            target_user TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'root')")
    conn.commit()
    conn.close()

    original_state = dict(security_events._STATE)
    original_cleanup_at = security_events._LAST_EVENT_CLEANUP_AT
    try:
        security_events._LAST_EVENT_CLEANUP_AT = 10**12
        security_events.configure_security_events_service(
            get_db=_get_db_factory(str(db_path)),
            get_system_settings=lambda: {},
            audit=lambda *args, **kwargs: None,
            is_ip_blocking_enabled=lambda: False,
        )

        security_events.record_security_event(
            "csrf_fail",
            "10.0.0.5",
            target_user="root",
            detail="path=/api/admin/system-reset,reason=invalid_authenticated",
            created_at="2026-05-11T10:32:00",
        )
        security_events.record_security_event(
            "csrf_fail",
            "10.0.0.5",
            target_user="root",
            detail="path=/api/admin/server-mode,reason=invalid_safe",
            created_at="2026-05-11T10:32:30",
        )
        security_events.record_security_event(
            "csrf_fail",
            "10.0.0.6",
            target_user="root",
            detail="path=/api/admin/system-reset,reason=invalid_authenticated",
            created_at="2026-05-11T10:33:00",
        )
        security_events.record_security_event(
            "csrf_fail",
            "10.0.0.5",
            target_user="root",
            detail="path=/api/admin/snapshots/not-a-snapshot/restore,reason=invalid_authenticated",
            created_at="2026-05-11T10:38:01",
        )
    finally:
        security_events._STATE.clear()
        security_events._STATE.update(original_state)
        security_events._LAST_EVENT_CLEANUP_AT = original_cleanup_at

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        event_count = conn.execute(
            "SELECT COUNT(*) AS c FROM security_events WHERE event_type='csrf_fail'"
        ).fetchone()["c"]
        notes = conn.execute(
            "SELECT body FROM notifications WHERE user_id=1 AND type='root_security_alert' ORDER BY id"
        ).fetchall()
        assert event_count == 4
        assert len(notes) == 3
        assert "通知彙總" in notes[0]["body"]
        assert "/api/admin/system-reset" in notes[0]["body"]
        assert "/api/admin/server-mode" not in notes[0]["body"]
    finally:
        conn.close()


def test_ip_block_root_notifications_are_burst_throttled_but_events_are_kept(tmp_path):
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE
        );
        CREATE TABLE security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            target_user TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'root')")
    conn.commit()
    conn.close()

    original_state = dict(security_events._STATE)
    original_cleanup_at = security_events._LAST_EVENT_CLEANUP_AT
    try:
        security_events._LAST_EVENT_CLEANUP_AT = 10**12
        security_events.configure_security_events_service(
            get_db=_get_db_factory(str(db_path)),
            get_system_settings=lambda: {},
            audit=lambda *args, **kwargs: None,
            is_ip_blocking_enabled=lambda: False,
        )

        security_events.record_security_event(
            "ip_block",
            "10.0.0.7",
            target_user="alice",
            detail="blocked_until=2026-05-16T10:00:01",
            created_at="2026-05-16T09:50:00",
        )
        security_events.record_security_event(
            "ip_block",
            "10.0.0.7",
            target_user="alice",
            detail="blocked_until=2026-05-16T10:00:02",
            created_at="2026-05-16T09:50:03",
        )
        security_events.record_security_event(
            "ip_block",
            "10.0.0.8",
            target_user="bob",
            detail="blocked_until=2026-05-16T10:00:04",
            created_at="2026-05-16T09:50:04",
        )
    finally:
        security_events._STATE.clear()
        security_events._STATE.update(original_state)
        security_events._LAST_EVENT_CLEANUP_AT = original_cleanup_at

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        event_count = conn.execute(
            "SELECT COUNT(*) AS c FROM security_events WHERE event_type='ip_block'"
        ).fetchone()["c"]
        notes = conn.execute(
            "SELECT body FROM notifications WHERE user_id=1 AND type='root_security_alert' ORDER BY id"
        ).fetchall()
        assert event_count == 3
        assert len(notes) == 2
        assert "通知彙總" in notes[0]["body"]
        assert "封鎖期限：2026-05-16T10:00:01" in notes[0]["body"]
        assert "2026-05-16T10:00:02" not in notes[0]["body"]
    finally:
        conn.close()


def test_rate_limit_block_records_security_event(tmp_path):
    db_path = tmp_path / "rate-limit.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            target_user TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    original_state = dict(security_events._STATE)
    original_cleanup_at = security_events._LAST_EVENT_CLEANUP_AT
    original_buckets = dict(security_events._RATE_LIMIT_BUCKETS)
    try:
        security_events._LAST_EVENT_CLEANUP_AT = 10**12
        security_events._RATE_LIMIT_BUCKETS.clear()
        security_events.configure_security_events_service(
            get_db=_get_db_factory(str(db_path)),
            get_system_settings=lambda: {},
            audit=lambda *args, **kwargs: None,
            is_ip_blocking_enabled=lambda: False,
        )

        scoped_key = ("10.0.0.9", "login_ip")
        assert security_events.is_rate_limited(scoped_key, max_req=1, window_sec=60)[0] is False
        blocked, info = security_events.is_rate_limited(scoped_key, max_req=1, window_sec=60)
        assert blocked is True
        assert 1 <= info["retry_after_seconds"] <= 60
    finally:
        security_events._STATE.clear()
        security_events._STATE.update(original_state)
        security_events._LAST_EVENT_CLEANUP_AT = original_cleanup_at
        security_events._RATE_LIMIT_BUCKETS.clear()
        security_events._RATE_LIMIT_BUCKETS.update(original_buckets)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT event_type, ip_address, detail FROM security_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row[0] == "rate_limit"
    assert row[1] == "10.0.0.9"
    assert "limit=1" in row[2]


def test_capacity_probe_unlimited_disables_security_rate_limits(monkeypatch):
    original_buckets = dict(security_events._RATE_LIMIT_BUCKETS)
    original_user_buckets = dict(security_events._USER_RATE_LIMIT_BUCKETS)
    try:
        monkeypatch.setenv("HACKME_CAPACITY_PROBE_UNLIMITED", "1")
        security_events._RATE_LIMIT_BUCKETS.clear()
        security_events._USER_RATE_LIMIT_BUCKETS.clear()

        assert security_events.is_rate_limited("10.0.0.10", max_req=1, window_sec=60)[0] is False
        assert security_events.is_rate_limited("10.0.0.10", max_req=1, window_sec=60)[0] is False
        assert security_events.check_user_rate_limit(123, "chat_send", max_req=1, window_sec=60)[0] is False
        assert security_events.check_user_rate_limit(123, "chat_send", max_req=1, window_sec=60)[0] is False
    finally:
        security_events._RATE_LIMIT_BUCKETS.clear()
        security_events._RATE_LIMIT_BUCKETS.update(original_buckets)
        security_events._USER_RATE_LIMIT_BUCKETS.clear()
        security_events._USER_RATE_LIMIT_BUCKETS.update(original_user_buckets)
