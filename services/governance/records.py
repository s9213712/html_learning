from datetime import datetime


def ensure_user_reputation_columns(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "reputation" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN reputation INTEGER NOT NULL DEFAULT 0")
    if "updated_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")


def ensure_governance_records_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS moderation_actions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            moderator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action_type  TEXT NOT NULL,
            target_type  TEXT NOT NULL,
            target_id    INTEGER NOT NULL,
            reason       TEXT,
            is_auto      INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_mod_notes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            moderator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            note         TEXT NOT NULL,
            created_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reputation_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            delta          INTEGER NOT NULL,
            reason         TEXT NOT NULL,
            source_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            source_post_id INTEGER,
            created_at     TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moderation_actions_target ON moderation_actions(target_type, target_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moderation_actions_moderator ON moderation_actions(moderator_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_mod_notes_user ON user_mod_notes(user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reputation_events_user ON reputation_events(user_id, created_at)")


def record_moderation_action(conn, *, moderator_id, action_type, target_type, target_id, reason="", is_auto=False):
    ensure_governance_records_schema(conn)
    conn.execute(
        "INSERT INTO moderation_actions (moderator_id, action_type, target_type, target_id, reason, is_auto, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            int(moderator_id),
            (action_type or "").strip(),
            (target_type or "").strip(),
            int(target_id),
            (reason or "").strip()[:1000],
            1 if is_auto else 0,
            datetime.now().isoformat(),
        ),
    )


def add_reputation_event(conn, *, user_id, delta, reason, source_user_id=None, source_post_id=None):
    ensure_user_reputation_columns(conn)
    ensure_governance_records_schema(conn)
    delta = int(delta)
    conn.execute(
        "INSERT INTO reputation_events (user_id, delta, reason, source_user_id, source_post_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            int(user_id),
            delta,
            (reason or "manual_adjustment").strip()[:80],
            source_user_id,
            source_post_id,
            datetime.now().isoformat(),
        ),
    )
    conn.execute("UPDATE users SET reputation=COALESCE(reputation, 0)+?, updated_at=? WHERE id=?", (delta, datetime.now().isoformat(), int(user_id)))
