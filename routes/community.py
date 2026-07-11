import hashlib
import math
import re
import threading
from datetime import datetime, timedelta
from functools import wraps

from flask import Response, request

from services.governance.records import add_reputation_event, ensure_user_reputation_columns, record_moderation_action
from services.security.permissions import require_member_action


def register_community_routes(app, deps):
    COMMUNITY_POST_AUTO_HIDE_MIN_DISLIKES = 3
    COMMUNITY_POST_AUTO_HIDE_ACTIVE_USER_RATIO = 0.10
    BOARD_VISIBILITIES = {"public", "unlisted", "private"}
    THREAD_POST_TYPES = {"normal", "announcement", "question", "howto", "review", "nsfw"}
    DEFAULT_FORUM_BOARD_TITLES = (
        "遊戲專區",
        "二次元專區",
        "ComfyUI專區",
        "程式設計區",
        "AI新知區",
    )
    audit = deps["audit"]
    check_user_rate_limit = deps["check_user_rate_limit"]
    get_client_ip = deps["get_client_ip"]
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_db = deps["get_db"]
    get_ua = deps["get_ua"]
    json_resp = deps["json_resp"]
    normalize_text = deps["normalize_text"]
    parse_positive_int = deps["parse_positive_int"]
    require_csrf = deps["require_csrf"]
    require_csrf_safe = deps["require_csrf_safe"]
    role_rank = deps["role_rank"]
    add_violation = deps.get("add_violation")
    detect_chat_violation = deps.get("detect_chat_violation")
    points_service = deps.get("points_service")
    community_schema_lock = threading.RLock()
    community_schema_ready_keys = set()
    community_read_index_ready_keys = set()
    community_read_load_index_sql = (
        "CREATE INDEX IF NOT EXISTS idx_announcements_visible_order ON announcements(is_active, is_pinned, created_at, id)",
        "CREATE INDEX IF NOT EXISTS idx_forum_threads_board_visible_page ON forum_threads(board_id, is_deleted, status, is_sticky, created_at, id)",
        "CREATE INDEX IF NOT EXISTS idx_forum_posts_thread_page ON forum_posts(thread_id, is_deleted, is_hidden, is_pinned, created_at, id)",
    )

    def _schema_row_value(row, key, index=None, default=None):
        try:
            return row[key]
        except Exception:
            if index is not None:
                try:
                    return row[index]
                except Exception:
                    return default
            return default

    def _community_schema_db_key(conn):
        try:
            rows = conn.execute("PRAGMA database_list").fetchall()
            for row in rows:
                name = _schema_row_value(row, "name", 1, "")
                if name == "main":
                    filename = _schema_row_value(row, "file", 2, "")
                    return filename or f"memory:{id(conn)}"
        except Exception:
            pass
        return f"connection:{id(conn)}"

    def _community_table_columns(conn, table_name):
        try:
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        except Exception:
            return set()

    def _community_schema_is_current(conn):
        required = {
            "announcements": {
                "id",
                "title",
                "content",
                "author_user_id",
                "author_username",
                "is_pinned",
                "is_active",
                "created_at",
                "updated_at",
            },
            "forum_categories": {"id", "name", "description", "sort_order", "is_active", "created_at", "updated_at"},
            "forum_boards": {
                "id",
                "category_id",
                "slug",
                "title",
                "description",
                "rules",
                "visibility",
                "sort_order",
                "is_active",
                "last_activity_at",
                "owner_user_id",
                "owner_username",
                "status",
                "review_note",
                "reviewed_by",
                "reviewed_at",
                "created_at",
                "updated_at",
            },
            "forum_threads": {
                "id",
                "board_id",
                "title",
                "content",
                "status",
                "review_note",
                "reviewed_by",
                "reviewed_at",
                "post_type",
                "is_sticky",
                "is_locked",
                "is_curated",
                "view_count",
                "edited_at",
                "edited_by",
                "is_deleted",
                "deleted_at",
                "deleted_by",
                "delete_reason",
                "author_user_id",
                "author_username",
                "created_at",
                "updated_at",
            },
            "forum_posts": {
                "id",
                "thread_id",
                "content",
                "author_user_id",
                "author_username",
                "is_pinned",
                "is_hidden",
                "hidden_reason",
                "edited_at",
                "edited_by",
                "is_deleted",
                "deleted_at",
                "deleted_by",
                "delete_reason",
                "created_at",
                "updated_at",
            },
            "board_moderators": {
                "id",
                "board_id",
                "user_id",
                "username",
                "can_review_threads",
                "can_pin_posts",
                "can_pin_threads",
                "can_lock_threads",
                "can_edit_posts",
                "can_delete_posts",
                "can_reward_authors",
                "can_penalize_posts",
                "created_by",
                "created_at",
                "updated_at",
            },
            "forum_post_reactions": {"id", "post_id", "user_id", "value"},
            "forum_thread_reactions": {"id", "thread_id", "user_id", "value"},
            "forum_post_reports": {"id", "post_id", "thread_id", "reason", "status"},
            "forum_thread_views": {"id", "thread_id", "viewer_key", "viewed_at"},
        }
        for table_name, columns in required.items():
            if not columns.issubset(_community_table_columns(conn, table_name)):
                return False
        try:
            row = conn.execute(
                "SELECT id FROM forum_categories WHERE name=?",
                ("一般討論",),
            ).fetchone()
            if row is None:
                return False
            placeholders = ",".join("?" for _ in DEFAULT_FORUM_BOARD_TITLES)
            default_count = conn.execute(
                f"SELECT COUNT(*) AS c FROM forum_boards WHERE title IN ({placeholders})",
                DEFAULT_FORUM_BOARD_TITLES,
            ).fetchone()
            return int(_schema_row_value(default_count, "c", 0, 0) or 0) >= len(DEFAULT_FORUM_BOARD_TITLES)
        except Exception:
            return False

    def _ensure_community_read_load_indexes(conn):
        schema_key = _community_schema_db_key(conn)
        if schema_key in community_read_index_ready_keys:
            return True
        try:
            for sql in community_read_load_index_sql:
                conn.execute(sql)
            if not getattr(conn, "in_transaction", False):
                conn.commit()
            community_read_index_ready_keys.add(schema_key)
            return True
        except Exception as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                return False
            raise

    def require_csrf_by_method(fn):
        safe = require_csrf_safe(fn)
        strict = require_csrf(fn)

        @wraps(fn)
        def decorated(*args, **kwargs):
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                return safe(*args, **kwargs)
            return strict(*args, **kwargs)

        return decorated

    def actor_value(actor, key, default=None):
        if not actor:
            return default
        if hasattr(actor, "keys"):
            return actor[key] if key in actor.keys() else default
        return actor.get(key, default) if hasattr(actor, "get") else default

    def award_points(rule_key, *, user_id, reference_type, reference_id, actor=None, metadata=None):
        if not points_service or not user_id:
            return
        if actor and actor_value(actor, "username") == "root":
            return
        try:
            points_service.earn_points(
                user_id=user_id,
                rule_key=rule_key,
                reference_type=reference_type,
                reference_id=str(reference_id or ""),
                metadata=metadata or {},
                actor=actor,
            )
        except Exception as exc:
            audit(
                "POINTS_AWARD_FAILED",
                get_client_ip(),
                user=actor_value(actor, "username", "system"),
                success=False,
                ua=get_ua(),
                detail=f"rule={rule_key},user_id={user_id},reference={reference_type}:{reference_id},error={exc}",
            )

    def is_comfyui_board(board):
        if not board:
            return False
        title = row_value(board, "title", "") if hasattr(board, "keys") else board.get("title", "")
        if not title:
            title = row_value(board, "board_title", "") if hasattr(board, "keys") else board.get("board_title", "")
        slug = row_value(board, "slug", "") if hasattr(board, "keys") else board.get("slug", "")
        return "comfyui" in str(title or "").lower() or "comfyui" in str(slug or "").lower()

    def reject_sensitive_community_content(actor, text, *, content_type, reference=""):
        if not callable(detect_chat_violation):
            return None
        is_bad, reason = detect_chat_violation(text or "")
        if not is_bad:
            return None
        if actor_value(actor, "username") != "root" and callable(add_violation):
            try:
                role = "super_admin" if actor_value(actor, "username") == "root" else actor_value(actor, "role", "user")
                add_violation(
                    actor_value(actor, "id"),
                    actor_value(actor, "username"),
                    role,
                    points=1,
                    reason=f"討論區敏感詞：{reason}",
                    triggered_by=content_type,
                    actor_username=actor_value(actor, "username"),
                )
            except Exception as exc:
                audit(
                    "COMMUNITY_SENSITIVE_VIOLATION_FAILED",
                    get_client_ip(),
                    user=actor_value(actor, "username", "system"),
                    success=False,
                    ua=get_ua(),
                    detail=f"type={content_type},reference={reference},reason={reason},error={exc}",
                )
        audit(
            "COMMUNITY_SENSITIVE_BLOCKED",
            get_client_ip(),
            user=actor_value(actor, "username", "system"),
            success=False,
            ua=get_ua(),
            detail=f"type={content_type},reference={reference},reason={reason}",
        )
        return json_resp({
            "ok": False,
            "warned": True,
            "reason": reason,
            "msg": f"內容含敏感詞，請修改後再送出（{reason}）",
        }), 403

    def require_csrf_by_method(fn):
        safe = require_csrf_safe(fn)
        strict = require_csrf(fn)

        @wraps(fn)
        def decorated(*args, **kwargs):
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                return safe(*args, **kwargs)
            return strict(*args, **kwargs)

        return decorated

    def ensure_community_schema(conn):
        schema_key = _community_schema_db_key(conn)
        if schema_key in community_schema_ready_keys:
            _ensure_community_read_load_indexes(conn)
            return
        if _community_schema_is_current(conn):
            _ensure_community_read_load_indexes(conn)
            community_schema_ready_keys.add(schema_key)
            return
        with community_schema_lock:
            if schema_key in community_schema_ready_keys:
                _ensure_community_read_load_indexes(conn)
                return
            if _community_schema_is_current(conn):
                _ensure_community_read_load_indexes(conn)
                community_schema_ready_keys.add(schema_key)
                return

            # Guard executescript so AttributeError (missing method on wrapper) can't cause 500 (L-2)
            try:
                conn.executescript("""
        CREATE TABLE IF NOT EXISTS announcements (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            title            TEXT NOT NULL,
            content          TEXT NOT NULL,
            author_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            author_username  TEXT NOT NULL,
            is_pinned        INTEGER NOT NULL DEFAULT 0,
            is_active        INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS forum_categories (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL UNIQUE,
            description      TEXT,
            sort_order       INTEGER NOT NULL DEFAULT 100,
            is_active        INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS forum_boards (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id      INTEGER REFERENCES forum_categories(id) ON DELETE SET NULL,
            slug             TEXT UNIQUE,
            title            TEXT NOT NULL,
            description      TEXT NOT NULL,
            rules            TEXT,
            visibility       TEXT NOT NULL DEFAULT 'public',
            sort_order       INTEGER NOT NULL DEFAULT 100,
            is_active        INTEGER NOT NULL DEFAULT 1,
            last_activity_at TEXT,
            owner_user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            owner_username   TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'pending',
            review_note      TEXT,
            reviewed_by      TEXT,
            reviewed_at      TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS forum_threads (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id         INTEGER NOT NULL REFERENCES forum_boards(id) ON DELETE CASCADE,
            title            TEXT NOT NULL,
            content          TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'approved',
            review_note      TEXT,
            reviewed_by      TEXT,
            reviewed_at      TEXT,
            post_type        TEXT NOT NULL DEFAULT 'normal',
            is_sticky        INTEGER NOT NULL DEFAULT 0,
            is_locked        INTEGER NOT NULL DEFAULT 0,
            is_curated       INTEGER NOT NULL DEFAULT 0,
            view_count       INTEGER NOT NULL DEFAULT 0,
            edited_at        TEXT,
            edited_by        TEXT,
            is_deleted       INTEGER NOT NULL DEFAULT 0,
            deleted_at       TEXT,
            deleted_by       TEXT,
            delete_reason    TEXT,
            author_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            author_username  TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS forum_thread_views (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id        INTEGER NOT NULL REFERENCES forum_threads(id) ON DELETE CASCADE,
            viewer_key       TEXT NOT NULL,
            viewed_at        TEXT NOT NULL,
            UNIQUE(thread_id, viewer_key)
        );

        CREATE TABLE IF NOT EXISTS forum_posts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id        INTEGER NOT NULL REFERENCES forum_threads(id) ON DELETE CASCADE,
            content          TEXT NOT NULL,
            author_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            author_username  TEXT NOT NULL,
            is_pinned        INTEGER NOT NULL DEFAULT 0,
            is_hidden        INTEGER NOT NULL DEFAULT 0,
            hidden_reason    TEXT,
            edited_at        TEXT,
            edited_by        TEXT,
            is_deleted       INTEGER NOT NULL DEFAULT 0,
            deleted_at       TEXT,
            deleted_by       TEXT,
            delete_reason    TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS board_moderators (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id            INTEGER NOT NULL REFERENCES forum_boards(id) ON DELETE CASCADE,
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            username            TEXT NOT NULL,
            can_review_threads  INTEGER NOT NULL DEFAULT 1,
            can_pin_posts       INTEGER NOT NULL DEFAULT 1,
            can_pin_threads     INTEGER NOT NULL DEFAULT 1,
            can_lock_threads    INTEGER NOT NULL DEFAULT 1,
            can_edit_posts      INTEGER NOT NULL DEFAULT 1,
            can_delete_posts    INTEGER NOT NULL DEFAULT 1,
            can_reward_authors  INTEGER NOT NULL DEFAULT 1,
            can_penalize_posts  INTEGER NOT NULL DEFAULT 1,
            created_by          TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            UNIQUE(board_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS forum_post_reactions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id        INTEGER NOT NULL REFERENCES forum_posts(id) ON DELETE CASCADE,
            user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            value          INTEGER NOT NULL,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            UNIQUE(post_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS forum_thread_reactions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id      INTEGER NOT NULL REFERENCES forum_threads(id) ON DELETE CASCADE,
            user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            value          INTEGER NOT NULL,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            UNIQUE(thread_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS forum_post_reports (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id          INTEGER NOT NULL REFERENCES forum_posts(id) ON DELETE CASCADE,
            thread_id        INTEGER NOT NULL REFERENCES forum_threads(id) ON DELETE CASCADE,
            reporter_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reported_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason           TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'pending',
            reviewed_by      TEXT,
            reviewed_at      TEXT,
            review_note      TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(post_id, reason)
        );

        CREATE INDEX IF NOT EXISTS idx_announcements_active ON announcements(is_active, created_at);
        CREATE INDEX IF NOT EXISTS idx_announcements_visible_order ON announcements(is_active, is_pinned, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_forum_categories_active_sort ON forum_categories(is_active, sort_order, name);
        CREATE INDEX IF NOT EXISTS idx_forum_boards_category ON forum_boards(category_id, status, sort_order, last_activity_at);
        CREATE INDEX IF NOT EXISTS idx_forum_boards_visible ON forum_boards(is_active, visibility, status, sort_order);
        CREATE INDEX IF NOT EXISTS idx_forum_boards_status ON forum_boards(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_forum_threads_board ON forum_threads(board_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_forum_threads_visible ON forum_threads(board_id, is_deleted, status, is_sticky, created_at);
        CREATE INDEX IF NOT EXISTS idx_forum_threads_board_visible_page ON forum_threads(board_id, is_deleted, status, is_sticky, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_forum_posts_thread ON forum_posts(thread_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_forum_posts_visible ON forum_posts(thread_id, is_deleted, is_hidden, is_pinned, created_at);
        CREATE INDEX IF NOT EXISTS idx_forum_posts_thread_page ON forum_posts(thread_id, is_deleted, is_hidden, is_pinned, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_board_moderators_user ON board_moderators(user_id, board_id);
        CREATE INDEX IF NOT EXISTS idx_forum_post_reactions_post ON forum_post_reactions(post_id);
        CREATE INDEX IF NOT EXISTS idx_forum_thread_reactions_thread ON forum_thread_reactions(thread_id);
        CREATE INDEX IF NOT EXISTS idx_forum_post_reports_status ON forum_post_reports(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_forum_thread_views_thread ON forum_thread_views(thread_id, viewed_at);
        """)
            except Exception:
                return  # Guard: if executescript fails (wrapper doesn't support it), skip schema init
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(announcements)").fetchall()}
            if "is_pinned" not in cols:
                conn.execute("ALTER TABLE announcements ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0")
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(forum_boards)").fetchall()}
            if "category_id" not in cols:
                conn.execute("ALTER TABLE forum_boards ADD COLUMN category_id INTEGER REFERENCES forum_categories(id) ON DELETE SET NULL")
            for name, ddl in (
                ("slug", "TEXT"),
                ("visibility", "TEXT NOT NULL DEFAULT 'public'"),
                ("sort_order", "INTEGER NOT NULL DEFAULT 100"),
                ("is_active", "INTEGER NOT NULL DEFAULT 1"),
                ("last_activity_at", "TEXT"),
            ):
                if name not in cols:
                    conn.execute(f"ALTER TABLE forum_boards ADD COLUMN {name} {ddl}")
            if "rules" not in cols:
                conn.execute("ALTER TABLE forum_boards ADD COLUMN rules TEXT")
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(forum_threads)").fetchall()}
            if "status" not in cols:
                conn.execute("ALTER TABLE forum_threads ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
            if "review_note" not in cols:
                conn.execute("ALTER TABLE forum_threads ADD COLUMN review_note TEXT")
            if "reviewed_by" not in cols:
                conn.execute("ALTER TABLE forum_threads ADD COLUMN reviewed_by TEXT")
            if "reviewed_at" not in cols:
                conn.execute("ALTER TABLE forum_threads ADD COLUMN reviewed_at TEXT")
            for name, ddl in (
                ("post_type", "TEXT NOT NULL DEFAULT 'normal'"),
                ("is_sticky", "INTEGER NOT NULL DEFAULT 0"),
                ("is_locked", "INTEGER NOT NULL DEFAULT 0"),
                ("is_curated", "INTEGER NOT NULL DEFAULT 0"),
                ("view_count", "INTEGER NOT NULL DEFAULT 0"),
                ("edited_at", "TEXT"),
                ("edited_by", "TEXT"),
                ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
                ("deleted_at", "TEXT"),
                ("deleted_by", "TEXT"),
                ("delete_reason", "TEXT"),
            ):
                if name not in cols:
                    conn.execute(f"ALTER TABLE forum_threads ADD COLUMN {name} {ddl}")
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(forum_posts)").fetchall()}
            for name, ddl in (
                ("is_pinned", "INTEGER NOT NULL DEFAULT 0"),
                ("is_hidden", "INTEGER NOT NULL DEFAULT 0"),
                ("hidden_reason", "TEXT"),
                ("edited_at", "TEXT"),
                ("edited_by", "TEXT"),
                ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
                ("deleted_at", "TEXT"),
                ("deleted_by", "TEXT"),
                ("delete_reason", "TEXT"),
            ):
                if name not in cols:
                    conn.execute(f"ALTER TABLE forum_posts ADD COLUMN {name} {ddl}")
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(board_moderators)").fetchall()}
            for name, ddl in (
                ("can_review_threads", "INTEGER NOT NULL DEFAULT 1"),
                ("can_pin_posts", "INTEGER NOT NULL DEFAULT 1"),
                ("can_pin_threads", "INTEGER NOT NULL DEFAULT 1"),
                ("can_lock_threads", "INTEGER NOT NULL DEFAULT 1"),
                ("can_edit_posts", "INTEGER NOT NULL DEFAULT 1"),
                ("can_delete_posts", "INTEGER NOT NULL DEFAULT 1"),
                ("can_reward_authors", "INTEGER NOT NULL DEFAULT 1"),
                ("can_penalize_posts", "INTEGER NOT NULL DEFAULT 1"),
                ("created_by", "TEXT NOT NULL DEFAULT 'system'"),
                ("created_at", "TEXT"),
                ("updated_at", "TEXT"),
            ):
                if name not in cols:
                    conn.execute(f"ALTER TABLE board_moderators ADD COLUMN {name} {ddl}")
            default_category_id = ensure_default_forum_category(conn)
            conn.execute(
                "UPDATE forum_boards SET category_id=? WHERE category_id IS NULL",
                (default_category_id,)
            )
            conn.execute(
                "UPDATE forum_boards SET last_activity_at=COALESCE(last_activity_at, updated_at, created_at) "
                "WHERE last_activity_at IS NULL"
            )
            for row in conn.execute("SELECT id, title FROM forum_boards WHERE slug IS NULL OR slug=''").fetchall():
                conn.execute(
                    "UPDATE forum_boards SET slug=? WHERE id=?",
                    (make_board_slug(row["title"], row["id"]), row["id"])
                )
            ensure_default_forum_boards(conn, default_category_id)
            conn.commit()
            community_schema_ready_keys.add(schema_key)
            community_read_index_ready_keys.add(schema_key)

    def ensure_default_forum_category(conn):
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO forum_categories "
            "(name, description, sort_order, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            ("一般討論", "預設討論分類", 100, now, now)
        )
        return conn.execute(
            "SELECT id FROM forum_categories WHERE name=?",
            ("一般討論",)
        ).fetchone()["id"]

    def default_forum_moderator(conn):
        user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        level_expr = "''"
        if "effective_level" in user_cols:
            level_expr = "COALESCE(effective_level, base_level, member_level, '')"
        elif "base_level" in user_cols:
            level_expr = "COALESCE(base_level, member_level, '')"
        elif "member_level" in user_cols:
            level_expr = "COALESCE(member_level, '')"
        return conn.execute(
            f"SELECT id, username FROM users "
            f"WHERE username='root' OR role IN ('super_admin', 'manager') OR {level_expr}='vip' "
            f"ORDER BY CASE WHEN username='root' THEN 0 WHEN role='super_admin' THEN 1 WHEN role='manager' THEN 2 ELSE 3 END, id ASC "
            f"LIMIT 1"
        ).fetchone()

    def ensure_user_violation_columns(conn):
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "violation_count" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN violation_count INTEGER NOT NULL DEFAULT 0")
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")

    def ensure_board_moderator(conn, board_id, user_id, username, created_by="system"):
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO board_moderators (board_id, user_id, username, can_review_threads, can_pin_posts, can_pin_threads, "
            "can_lock_threads, can_edit_posts, can_delete_posts, can_reward_authors, can_penalize_posts, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 1, 1, 1, 1, 1, 1, 1, ?, ?, ?) "
            "ON CONFLICT(board_id, user_id) DO UPDATE SET username=excluded.username, updated_at=excluded.updated_at",
            (board_id, user_id, username, created_by, now, now)
        )

    def ensure_default_forum_boards(conn, category_id):
        moderator = default_forum_moderator(conn)
        if not moderator:
            return
        now = datetime.now().isoformat()
        default_details = (
            ("遊戲專區", "遊戲討論、攻略、組隊與實測心得。", "禁止外掛、詐騙、洗版與人身攻擊。", 10),
            ("二次元專區", "動漫、漫畫、角色創作與作品交流。", "尊重創作者與分級規範，禁止盜版與騷擾。", 20),
            ("ComfyUI專區", "ComfyUI 工作流、模型、節點與生成參數交流。", "分享工作流時請標註來源與使用限制。", 30),
            ("程式設計區", "程式設計、除錯、架構與工具鏈討論。", "提問請附環境、錯誤訊息與可重現步驟。", 40),
            ("AI新知區", "AI 研究、產品、模型與產業消息討論。", "請標註消息來源，避免未證實傳言。", 50),
        )
        for title, description, rules, sort_order in default_details:
            row = conn.execute("SELECT id FROM forum_boards WHERE title=?", (title,)).fetchone()
            if row:
                ensure_board_moderator(conn, row["id"], moderator["id"], moderator["username"])
                continue
            cur = conn.execute(
                "INSERT INTO forum_boards (category_id, title, description, rules, visibility, sort_order, is_active, "
                "last_activity_at, owner_user_id, owner_username, status, review_note, reviewed_by, reviewed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'public', ?, 1, ?, ?, ?, 'approved', ?, 'system', ?, ?, ?)",
                (
                    category_id,
                    title,
                    description,
                    rules,
                    sort_order,
                    now,
                    moderator["id"],
                    moderator["username"],
                    "系統預設討論版",
                    now,
                    now,
                    now,
                )
            )
            conn.execute("UPDATE forum_boards SET slug=? WHERE id=?", (make_board_slug(title, cur.lastrowid), cur.lastrowid))
            ensure_board_moderator(conn, cur.lastrowid, moderator["id"], moderator["username"])

    def row_value(row, key, default=None):
        return row[key] if key in row.keys() else default

    def make_board_slug(title, board_id):
        base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
        if not base:
            base = "board"
        return f"{base[:48]}-{board_id}"

    def category_payload(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"] or "",
            "sort_order": row["sort_order"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def actor_value(actor, key, default=None):
        if not actor:
            return default
        try:
            return actor[key]
        except Exception:
            return actor.get(key, default) if hasattr(actor, "get") else default

    def actor_role(actor):
        return "super_admin" if actor_value(actor, "username") == "root" else actor_value(actor, "role", "user")

    def can_manage_community(actor):
        if actor and actor_value(actor, "username") == "root":
            return True
        return role_rank(actor_role(actor)) >= role_rank("manager")

    def board_moderator_row(conn, board_id, actor):
        if not actor:
            return None
        return conn.execute(
            "SELECT * FROM board_moderators WHERE board_id=? AND user_id=?",
            (board_id, actor["id"])
        ).fetchone()

    def can_moderate_board(conn, board_id, actor, permission=None):
        if not actor:
            return False
        if actor_value(actor, "username") == "root":
            return True
        if can_manage_community(actor):
            return True
        row = board_moderator_row(conn, board_id, actor)
        if not row:
            return False
        if not permission:
            return True
        return bool(row[permission])

    def board_moderator_permissions(conn, board_id, actor):
        keys = (
            "can_review_threads",
            "can_pin_posts",
            "can_pin_threads",
            "can_lock_threads",
            "can_edit_posts",
            "can_delete_posts",
            "can_reward_authors",
            "can_penalize_posts",
        )
        if not actor:
            return {key: False for key in keys}
        if actor_value(actor, "username") == "root" or can_manage_community(actor):
            return {key: True for key in keys}
        row = board_moderator_row(conn, board_id, actor)
        if not row:
            return {key: False for key in keys}
        return {key: bool(row_value(row, key, 0)) for key in keys}

    def can_delete_community_content(actor, author_user_id=None, owner_user_id=None):
        if not actor:
            return False
        if actor_value(actor, "username") == "root":
            return True
        if can_manage_community(actor):
            return True
        return actor["id"] in (author_user_id, owner_user_id)

    def can_edit_community_content(actor, author_user_id=None):
        if not actor:
            return False
        if can_manage_community(actor):
            return True
        return actor["id"] == author_user_id

    def ensure_auto_hidden_post_report(conn, post_id, actor_id, dislike_count, auto_hide_threshold):
        post = conn.execute(
            "SELECT p.id, p.thread_id, p.author_user_id, p.author_username, p.content "
            "FROM forum_posts p WHERE p.id=?",
            (post_id,)
        ).fetchone()
        if not post:
            return
        reason = f"倒讚過多自動隱藏（{dislike_count}/{auto_hide_threshold}）"
        conn.execute(
            "INSERT OR IGNORE INTO forum_post_reports "
            "(post_id, thread_id, reporter_user_id, reported_user_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (post_id, post["thread_id"], actor_id, post["author_user_id"], reason, datetime.now().isoformat())
        )

    def community_post_auto_hide_threshold(conn):
        user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "status" in user_cols:
            active_users = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE status='active'"
            ).fetchone()["c"]
        else:
            active_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        ratio_threshold = math.ceil((active_users or 0) * COMMUNITY_POST_AUTO_HIDE_ACTIVE_USER_RATIO)
        return max(COMMUNITY_POST_AUTO_HIDE_MIN_DISLIKES, ratio_threshold)

    def board_payload(row):
        category_id = row_value(row, "category_id")
        category_name = row_value(row, "category_name")
        moderators_raw = row_value(row, "moderators", "") or ""
        moderators = [name for name in moderators_raw.split(",") if name]
        return {
            "id": row["id"],
            "category_id": category_id,
            "slug": row_value(row, "slug"),
            "category": {
                "id": category_id,
                "name": category_name,
                "description": row_value(row, "category_description", "") or "",
                "sort_order": row_value(row, "category_sort_order"),
                "is_active": bool(row_value(row, "category_is_active", 1)),
            } if category_id and category_name else None,
            "title": row["title"],
            "description": row["description"],
            "rules": row["rules"] or "",
            "visibility": row_value(row, "visibility", "public"),
            "sort_order": row_value(row, "sort_order", 100),
            "is_active": bool(row_value(row, "is_active", 1)),
            "last_activity_at": row_value(row, "last_activity_at"),
            "owner_user_id": row["owner_user_id"],
            "owner_username": row["owner_username"],
            "moderators": moderators,
            "moderator_count": int(row_value(row, "moderator_count", len(moderators)) or 0),
            "status": row["status"],
            "review_note": row["review_note"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def board_moderator_payload(row):
        return {
            "id": row["id"],
            "board_id": row["board_id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "can_review_threads": bool(row["can_review_threads"]),
            "can_pin_posts": bool(row["can_pin_posts"]),
            "can_pin_threads": bool(row_value(row, "can_pin_threads", row["can_pin_posts"])),
            "can_lock_threads": bool(row["can_lock_threads"]),
            "can_edit_posts": bool(row["can_edit_posts"]),
            "can_delete_posts": bool(row["can_delete_posts"]),
            "can_reward_authors": bool(row["can_reward_authors"]),
            "can_penalize_posts": bool(row["can_penalize_posts"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def validate_category_id(conn, category_id):
        if not category_id:
            return ensure_default_forum_category(conn), None
        try:
            parsed = int(category_id)
        except Exception:
            return None, "分類格式錯誤"
        row = conn.execute(
            "SELECT id FROM forum_categories WHERE id=? AND is_active=1",
            (parsed,)
        ).fetchone()
        if not row:
            return None, "找不到可用的討論分類"
        return row["id"], None

    def normalize_board_visibility(value):
        visibility = normalize_text(value) or "public"
        return visibility if visibility in BOARD_VISIBILITIES else None

    def normalize_thread_post_type(value):
        post_type = normalize_text(value) or "normal"
        return post_type if post_type in THREAD_POST_TYPES else None

    def normalize_payload_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = normalize_text(value).lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
        return bool(value)

    def actor_effective_level(actor):
        return actor_value(actor, "effective_level") or actor_value(actor, "base_level") or actor_value(actor, "member_level") or "normal"

    def thread_requires_review(actor, manageable):
        return (not manageable) and actor_effective_level(actor) == "newbie"

    def record_thread_view(conn, thread_id, actor):
        viewer_key = f"user:{actor['id']}"
        now = datetime.now()
        row = conn.execute(
            "SELECT viewed_at FROM forum_thread_views WHERE thread_id=? AND viewer_key=?",
            (thread_id, viewer_key),
        ).fetchone()
        should_count = True
        if row and row["viewed_at"]:
            try:
                should_count = datetime.fromisoformat(row["viewed_at"]) <= now - timedelta(minutes=15)
            except Exception:
                should_count = True
        if should_count:
            conn.execute(
                "UPDATE forum_threads SET view_count=COALESCE(view_count, 0)+1, updated_at=updated_at WHERE id=?",
                (thread_id,),
            )
            conn.execute(
                "INSERT INTO forum_thread_views (thread_id, viewer_key, viewed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(thread_id, viewer_key) DO UPDATE SET viewed_at=excluded.viewed_at",
                (thread_id, viewer_key, now.isoformat()),
            )
        return should_count

    def board_is_accessible(board, actor, manageable):
        if manageable:
            return True
        if not bool(row_value(board, "is_active", 1)):
            return False
        if board["owner_user_id"] == actor["id"]:
            return True
        return board["status"] == "approved" and row_value(board, "visibility", "public") == "public"

    def thread_payload(row):
        return {
            "id": row["id"],
            "board_id": row["board_id"],
            "title": row["title"],
            "content": row["content"],
            "status": row["status"],
            "review_note": row["review_note"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "post_type": row_value(row, "post_type", "normal"),
            "is_sticky": bool(row_value(row, "is_sticky", 0)),
            "is_locked": bool(row["is_locked"]),
            "is_curated": bool(row_value(row, "is_curated", 0)),
            "view_count": int(row_value(row, "view_count", 0) or 0),
            "like_count": int(row_value(row, "like_count", 0) or 0),
            "dislike_count": int(row_value(row, "dislike_count", 0) or 0),
            "user_reaction": int(row_value(row, "user_reaction", 0) or 0),
            "edited_at": row_value(row, "edited_at"),
            "edited_by": row_value(row, "edited_by"),
            "is_deleted": bool(row_value(row, "is_deleted", 0)),
            "deleted_at": row_value(row, "deleted_at"),
            "deleted_by": row_value(row, "deleted_by"),
            "delete_reason": row_value(row, "delete_reason"),
            "author_user_id": row["author_user_id"],
            "author_username": row["author_username"],
            "author_avatar_file_id": row_value(row, "author_avatar_file_id", ""),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @app.route("/api/community/announcements", methods=["GET", "POST"])
    @require_csrf_by_method
    def community_announcements():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401

        conn = get_db()
        try:
            ensure_community_schema(conn)
            if request.method == "GET":
                summary = conn.execute(
                    "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS updated_at, COALESCE(MAX(id), 0) AS max_id "
                    "FROM announcements WHERE is_active=1"
                ).fetchone()
                etag_seed = f"announcements:{summary['c']}:{summary['updated_at']}:{summary['max_id']}"
                etag = '"' + hashlib.sha256(etag_seed.encode("utf-8")).hexdigest()[:24] + '"'
                if str(request.headers.get("If-None-Match") or "").strip() == etag:
                    response = Response(status=304)
                    response.headers["ETag"] = etag
                    response.headers["Cache-Control"] = "private, max-age=15"
                    return response
                rows = conn.execute(
                    "SELECT id, title, content, author_username, is_pinned, created_at, updated_at "
                    "FROM announcements WHERE is_active=1 ORDER BY is_pinned DESC, created_at DESC LIMIT 30"
                ).fetchall()
                response = json_resp({
                    "ok": True,
                    "announcements": [{
                        "id": row["id"],
                        "title": row["title"],
                        "content": row["content"],
                        "author_username": row["author_username"],
                        "is_pinned": bool(row["is_pinned"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    } for row in rows],
                    "can_publish": can_manage_community(actor),
                })
                response.headers["ETag"] = etag
                response.headers["Cache-Control"] = "private, max-age=15"
                return response

            if not can_manage_community(actor):
                return json_resp({"ok": False, "msg": "只有管理員以上可發布公告"}), 403

            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            title = normalize_text(data.get("title"))[:80]
            content = normalize_text(data.get("content"))[:3000]
            is_pinned = 1 if bool(data.get("is_pinned")) else 0
            if not title or not content:
                return json_resp({"ok": False, "msg": "公告標題與內容不可為空"}), 400
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO announcements (title, content, author_user_id, author_username, is_pinned, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, content, actor["id"], actor["username"], is_pinned, now, now)
            )
            conn.commit()
            audit("COMMUNITY_ANNOUNCEMENT_CREATE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=title)
            return json_resp({"ok": True, "msg": "公告已發布"})
        finally:
            conn.close()

    @app.route("/api/community/announcements/<int:announcement_id>", methods=["PUT", "DELETE"])
    @require_csrf_by_method
    def community_update_or_delete_announcement(announcement_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if not can_manage_community(actor):
            action_label = "編輯" if request.method == "PUT" else "刪除"
            return json_resp({"ok": False, "msg": f"只有管理員以上可{action_label}公告"}), 403

        conn = get_db()
        try:
            ensure_community_schema(conn)
            row = conn.execute(
                "SELECT id, title, content, is_pinned FROM announcements WHERE id=? AND is_active=1",
                (announcement_id,),
            ).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到公告"}), 404
            if request.method == "PUT":
                try:
                    data = request.get_json(force=True)
                except Exception:
                    return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
                title = normalize_text(data.get("title"))[:80]
                content = normalize_text(data.get("content"))[:3000]
                is_pinned = 1 if bool(data.get("is_pinned")) else 0
                if not title or not content:
                    return json_resp({"ok": False, "msg": "公告標題與內容不可為空"}), 400
                now = datetime.now().isoformat()
                conn.execute(
                    "UPDATE announcements SET title=?, content=?, is_pinned=?, updated_at=? WHERE id=?",
                    (title, content, is_pinned, now, announcement_id),
                )
                conn.commit()
                audit("COMMUNITY_ANNOUNCEMENT_UPDATE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=title)
                return json_resp({
                    "ok": True,
                    "msg": "公告已更新",
                    "announcement": {
                        "id": announcement_id,
                        "title": title,
                        "content": content,
                        "is_pinned": bool(is_pinned),
                        "updated_at": now,
                    },
                })
            conn.execute(
                "UPDATE announcements SET is_active=0, updated_at=? WHERE id=?",
                (datetime.now().isoformat(), announcement_id)
            )
            conn.commit()
            audit("COMMUNITY_ANNOUNCEMENT_DELETE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=row["title"])
            return json_resp({"ok": True, "msg": "公告已刪除"})
        finally:
            conn.close()

    @app.route("/api/community/categories", methods=["GET", "POST"])
    @require_csrf_by_method
    def community_categories():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401

        conn = get_db()
        try:
            ensure_community_schema(conn)
            manageable = can_manage_community(actor)
            if request.method == "GET":
                where = "" if manageable else "WHERE is_active=1"
                rows = conn.execute(
                    f"SELECT * FROM forum_categories {where} ORDER BY sort_order ASC, name ASC"
                ).fetchall()
                if manageable:
                    count_rows = conn.execute(
                        "SELECT category_id, COUNT(*) AS c FROM forum_boards GROUP BY category_id"
                    ).fetchall()
                else:
                    count_rows = conn.execute(
                        "SELECT category_id, COUNT(*) AS c FROM forum_boards "
                        "WHERE is_active=1 AND ((status='approved' AND visibility='public') OR owner_user_id=?) "
                        "GROUP BY category_id",
                        (actor["id"],),
                    ).fetchall()
                board_counts = {row["category_id"]: int(row["c"] or 0) for row in count_rows}
                categories = []
                for row in rows:
                    payload = category_payload(row)
                    payload["board_count"] = board_counts.get(row["id"], 0)
                    categories.append(payload)
                return json_resp({"ok": True, "categories": categories, "can_manage": manageable})

            if not manageable:
                return json_resp({"ok": False, "msg": "只有管理員以上可建立討論分類"}), 403
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            name = normalize_text(data.get("name"))[:80]
            description = normalize_text(data.get("description"))[:500]
            sort_order = parse_positive_int(data.get("sort_order", 100), default=100, min_value=0, max_value=9999)
            is_active = 0 if data.get("is_active") is False else 1
            if not name:
                return json_resp({"ok": False, "msg": "分類名稱不可為空"}), 400
            now = datetime.now().isoformat()
            try:
                conn.execute(
                    "INSERT INTO forum_categories (name, description, sort_order, is_active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, description, sort_order, is_active, now, now)
                )
            except Exception:
                return json_resp({"ok": False, "msg": "分類名稱已存在或格式錯誤"}), 409
            conn.commit()
            audit("COMMUNITY_CATEGORY_CREATE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=name)
            return json_resp({"ok": True, "msg": "討論分類已建立"})
        finally:
            conn.close()

    @app.route("/api/community/categories/<int:category_id>", methods=["PUT"])
    @require_csrf
    def community_category_update(category_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if not can_manage_community(actor):
            return json_resp({"ok": False, "msg": "只有管理員以上可更新討論分類"}), 403

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400

        conn = get_db()
        try:
            ensure_community_schema(conn)
            row = conn.execute("SELECT * FROM forum_categories WHERE id=?", (category_id,)).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到討論分類"}), 404
            name = normalize_text(data.get("name"))[:80] if "name" in data else row["name"]
            description = normalize_text(data.get("description"))[:500] if "description" in data else row["description"]
            sort_order = parse_positive_int(
                data.get("sort_order", row["sort_order"]),
                default=row["sort_order"],
                min_value=0,
                max_value=9999,
            )
            is_active = 1 if data.get("is_active", bool(row["is_active"])) else 0
            if not name:
                return json_resp({"ok": False, "msg": "分類名稱不可為空"}), 400
            try:
                conn.execute(
                    "UPDATE forum_categories SET name=?, description=?, sort_order=?, is_active=?, updated_at=? WHERE id=?",
                    (name, description, sort_order, is_active, datetime.now().isoformat(), category_id)
                )
            except Exception:
                return json_resp({"ok": False, "msg": "分類名稱已存在或格式錯誤"}), 409
            conn.commit()
            audit("COMMUNITY_CATEGORY_UPDATE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"id={category_id}, name={name}")
            return json_resp({"ok": True, "msg": "討論分類已更新"})
        finally:
            conn.close()

    @app.route("/api/community/boards", methods=["GET", "POST"])
    @require_csrf_by_method
    def community_boards():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401

        conn = get_db()
        try:
            ensure_community_schema(conn)
            if request.method == "GET":
                # P4: thread/post counts are pre-aggregated as CTEs and
                # joined in one pass so the page no longer fires 1 + 2N
                # queries when the board list is long.
                base_select = (
                    "WITH thread_stats AS ( "
                    "    SELECT board_id, COUNT(*) AS thread_count "
                    "    FROM forum_threads "
                    "    WHERE is_deleted=0 "
                    "    GROUP BY board_id "
                    "), post_stats AS ( "
                    "    SELECT t.board_id, COUNT(*) AS post_count "
                    "    FROM forum_posts p "
                    "    JOIN forum_threads t ON t.id=p.thread_id AND t.is_deleted=0 "
                    "    WHERE p.is_deleted=0 "
                    "    GROUP BY t.board_id "
                    ") "
                    "SELECT b.*, c.name AS category_name, c.description AS category_description, "
                    "c.sort_order AS category_sort_order, c.is_active AS category_is_active, "
                    "(SELECT GROUP_CONCAT(username) FROM board_moderators WHERE board_id=b.id) AS moderators, "
                    "(SELECT COUNT(*) FROM board_moderators WHERE board_id=b.id) AS moderator_count, "
                    "COALESCE(ts.thread_count, 0) AS thread_count, "
                    "COALESCE(ps.post_count, 0) AS post_count "
                    "FROM forum_boards b "
                    "LEFT JOIN forum_categories c ON c.id=b.category_id "
                    "LEFT JOIN thread_stats ts ON ts.board_id=b.id "
                    "LEFT JOIN post_stats ps ON ps.board_id=b.id "
                )
                if can_manage_community(actor):
                    rows = conn.execute(
                        base_select
                        + "ORDER BY c.sort_order ASC, b.sort_order ASC, COALESCE(b.last_activity_at, b.created_at) DESC"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        base_select
                        + "WHERE b.is_active=1 AND ((b.status='approved' AND b.visibility='public') OR b.owner_user_id=?) "
                        "ORDER BY c.sort_order ASC, b.sort_order ASC, "
                        "CASE b.status WHEN 'approved' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
                        "COALESCE(b.last_activity_at, b.created_at) DESC",
                        (actor["id"],)
                    ).fetchall()
                boards = []
                for row in rows:
                    payload = board_payload(row)
                    payload["thread_count"] = row["thread_count"] or 0
                    payload["post_count"] = row["post_count"] or 0
                    boards.append(payload)
                return json_resp({"ok": True, "boards": boards, "can_review": can_manage_community(actor)})

            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            if not can_manage_community(actor):
                return json_resp({"ok": False, "msg": "目前只有管理員以上可建立討論版面"}), 403
            title = normalize_text(data.get("title"))[:80]
            description = normalize_text(data.get("description"))[:1200]
            rules = normalize_text(data.get("rules"))[:2000]
            category_id, category_error = validate_category_id(conn, data.get("category_id"))
            if category_error:
                return json_resp({"ok": False, "msg": category_error}), 400
            visibility = normalize_board_visibility(data.get("visibility"))
            if visibility is None:
                return json_resp({"ok": False, "msg": "版面可見性錯誤"}), 400
            sort_order = parse_positive_int(data.get("sort_order", 100), default=100, min_value=0, max_value=9999)
            if not title or not description:
                return json_resp({"ok": False, "msg": "討論區名稱與說明不可為空"}), 400
            existing = conn.execute(
                "SELECT 1 FROM forum_boards WHERE owner_user_id=? AND status='pending'",
                (actor["id"],)
            ).fetchone()
            if existing:
                return json_resp({"ok": False, "msg": "你已有待審核的討論區申請"}), 409
            now = datetime.now().isoformat()
            cursor = conn.execute(
                "INSERT INTO forum_boards (category_id, title, description, rules, visibility, sort_order, last_activity_at, "
                "owner_user_id, owner_username, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (category_id, title, description, rules, visibility, sort_order, now, actor["id"], actor["username"], now, now)
            )
            conn.execute(
                "UPDATE forum_boards SET slug=? WHERE id=?",
                (make_board_slug(title, cursor.lastrowid), cursor.lastrowid)
            )
            ensure_board_moderator(conn, cursor.lastrowid, actor["id"], actor["username"])
            conn.commit()
            audit("COMMUNITY_BOARD_REQUEST", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=title)
            return json_resp({"ok": True, "msg": "討論區申請已送出，待管理員審核"})
        finally:
            conn.close()

    @app.route("/api/community/boards/reviews", methods=["GET"])
    @require_csrf_safe
    def community_board_reviews():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if not can_manage_community(actor):
            return json_resp({"ok": False, "msg": "權限不足"}), 403

        conn = get_db()
        try:
            ensure_community_schema(conn)
            rows = conn.execute(
                "SELECT b.*, c.name AS category_name, c.description AS category_description, "
                "c.sort_order AS category_sort_order, c.is_active AS category_is_active, "
                "(SELECT GROUP_CONCAT(username) FROM board_moderators WHERE board_id=b.id) AS moderators, "
                "(SELECT COUNT(*) FROM board_moderators WHERE board_id=b.id) AS moderator_count "
                "FROM forum_boards b LEFT JOIN forum_categories c ON c.id=b.category_id "
                "WHERE b.status='pending' ORDER BY b.created_at ASC"
            ).fetchall()
            return json_resp({"ok": True, "items": [board_payload(row) for row in rows]})
        finally:
            conn.close()

    @app.route("/api/community/boards/<int:board_id>/review", methods=["POST"])
    @require_csrf
    def community_board_review(board_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if not can_manage_community(actor):
            return json_resp({"ok": False, "msg": "只有管理員以上可審核討論區"}), 403

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        action = normalize_text(data.get("action"))
        note = normalize_text(data.get("note"))[:200]
        if action not in ("approve", "reject"):
            return json_resp({"ok": False, "msg": "審核動作錯誤"}), 400

        conn = get_db()
        try:
            ensure_community_schema(conn)
            row = conn.execute("SELECT * FROM forum_boards WHERE id=?", (board_id,)).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到討論區申請"}), 404
            if row["status"] != "pending":
                return json_resp({"ok": False, "msg": "此討論區已審核"}), 409
            new_status = "approved" if action == "approve" else "rejected"
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE forum_boards SET status=?, review_note=?, reviewed_by=?, reviewed_at=?, updated_at=? WHERE id=?",
                (new_status, note or None, actor["username"], now, now, board_id)
            )
            if new_status == "approved":
                ensure_board_moderator(conn, board_id, row["owner_user_id"], row["owner_username"], actor["username"])
            conn.commit()
            audit("COMMUNITY_BOARD_REVIEW", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"board_id={board_id}, action={new_status}")
            return json_resp({"ok": True, "msg": "討論區審核完成"})
        finally:
            conn.close()

    @app.route("/api/community/boards/<int:board_id>", methods=["PUT"])
    @require_csrf
    def community_board_update(board_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if not can_manage_community(actor):
            return json_resp({"ok": False, "msg": "只有管理員以上可更新討論區"}), 403

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400

        conn = get_db()
        try:
            ensure_community_schema(conn)
            row = conn.execute("SELECT * FROM forum_boards WHERE id=?", (board_id,)).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到討論區"}), 404
            category_id = row["category_id"]
            if "category_id" in data:
                category_id, category_error = validate_category_id(conn, data.get("category_id"))
                if category_error:
                    return json_resp({"ok": False, "msg": category_error}), 400
            title = normalize_text(data.get("title"))[:80] if "title" in data else row["title"]
            description = normalize_text(data.get("description"))[:1200] if "description" in data else row["description"]
            rules = normalize_text(data.get("rules"))[:2000] if "rules" in data else (row["rules"] or "")
            visibility = normalize_board_visibility(data.get("visibility")) if "visibility" in data else row["visibility"]
            if visibility is None:
                return json_resp({"ok": False, "msg": "版面可見性錯誤"}), 400
            sort_order = parse_positive_int(
                data.get("sort_order", row["sort_order"]),
                default=row["sort_order"],
                min_value=0,
                max_value=9999,
            )
            is_active = 1 if data.get("is_active", bool(row["is_active"])) else 0
            if not title or not description:
                return json_resp({"ok": False, "msg": "討論區名稱與說明不可為空"}), 400
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE forum_boards SET category_id=?, title=?, description=?, rules=?, visibility=?, sort_order=?, "
                "is_active=?, updated_at=? WHERE id=?",
                (category_id, title, description, rules, visibility, sort_order, is_active, now, board_id)
            )
            if not row["slug"] or title != row["title"]:
                conn.execute("UPDATE forum_boards SET slug=? WHERE id=?", (make_board_slug(title, board_id), board_id))
            conn.commit()
            audit(
                "COMMUNITY_BOARD_UPDATE",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"board_id={board_id}, visibility={visibility}, is_active={is_active}"
            )
            return json_resp({"ok": True, "msg": "討論區已更新"})
        finally:
            conn.close()

    @app.route("/api/community/boards/<int:board_id>/moderators", methods=["GET", "POST"])
    @require_csrf_by_method
    def community_board_moderators(board_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if not can_manage_community(actor):
            return json_resp({"ok": False, "msg": "只有管理員以上可管理版主"}), 403

        conn = get_db()
        try:
            ensure_community_schema(conn)
            board = conn.execute("SELECT id, title FROM forum_boards WHERE id=?", (board_id,)).fetchone()
            if not board:
                return json_resp({"ok": False, "msg": "找不到討論區"}), 404
            if request.method == "GET":
                rows = conn.execute(
                    "SELECT * FROM board_moderators WHERE board_id=? ORDER BY username ASC",
                    (board_id,)
                ).fetchall()
                return json_resp({"ok": True, "moderators": [board_moderator_payload(row) for row in rows]})

            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            user_id = parse_positive_int(data.get("user_id"), default=None, min_value=1)
            if not user_id:
                return json_resp({"ok": False, "msg": "user_id 格式錯誤"}), 400
            user = conn.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                return json_resp({"ok": False, "msg": "找不到使用者"}), 404
            now = datetime.now().isoformat()
            values = {
                "can_review_threads": 1 if data.get("can_review_threads", True) else 0,
                "can_pin_posts": 1 if data.get("can_pin_posts", True) else 0,
                "can_pin_threads": 1 if data.get("can_pin_threads", data.get("can_pin_posts", True)) else 0,
                "can_lock_threads": 1 if data.get("can_lock_threads", True) else 0,
                "can_edit_posts": 1 if data.get("can_edit_posts", True) else 0,
                "can_delete_posts": 1 if data.get("can_delete_posts", True) else 0,
                "can_reward_authors": 1 if data.get("can_reward_authors", True) else 0,
                "can_penalize_posts": 1 if data.get("can_penalize_posts", True) else 0,
            }
            conn.execute(
                "INSERT INTO board_moderators (board_id, user_id, username, can_review_threads, can_pin_posts, can_pin_threads, "
                "can_lock_threads, can_edit_posts, can_delete_posts, can_reward_authors, can_penalize_posts, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(board_id, user_id) DO UPDATE SET "
                "username=excluded.username, can_review_threads=excluded.can_review_threads, "
                "can_pin_posts=excluded.can_pin_posts, can_pin_threads=excluded.can_pin_threads, can_lock_threads=excluded.can_lock_threads, "
                "can_edit_posts=excluded.can_edit_posts, can_delete_posts=excluded.can_delete_posts, "
                "can_reward_authors=excluded.can_reward_authors, can_penalize_posts=excluded.can_penalize_posts, "
                "updated_at=excluded.updated_at",
                (
                    board_id,
                    user["id"],
                    user["username"],
                    values["can_review_threads"],
                    values["can_pin_posts"],
                    values["can_pin_threads"],
                    values["can_lock_threads"],
                    values["can_edit_posts"],
                    values["can_delete_posts"],
                    values["can_reward_authors"],
                    values["can_penalize_posts"],
                    actor["username"],
                    now,
                    now,
                )
            )
            conn.commit()
            audit("COMMUNITY_BOARD_MODERATOR_UPSERT", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"board_id={board_id}, user_id={user_id}")
            return json_resp({"ok": True, "msg": "版主設定已儲存"})
        finally:
            conn.close()

    @app.route("/api/community/boards/<int:board_id>/moderators/<int:user_id>", methods=["DELETE"])
    @require_csrf
    def community_board_moderator_delete(board_id, user_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if not can_manage_community(actor):
            return json_resp({"ok": False, "msg": "只有管理員以上可移除版主"}), 403

        conn = get_db()
        try:
            ensure_community_schema(conn)
            row = conn.execute(
                "SELECT id FROM board_moderators WHERE board_id=? AND user_id=?",
                (board_id, user_id)
            ).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到版主設定"}), 404
            conn.execute("DELETE FROM board_moderators WHERE board_id=? AND user_id=?", (board_id, user_id))
            conn.commit()
            audit("COMMUNITY_BOARD_MODERATOR_DELETE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"board_id={board_id}, user_id={user_id}")
            return json_resp({"ok": True, "msg": "版主已移除"})
        finally:
            conn.close()

    @app.route("/api/community/boards/<int:board_id>/threads", methods=["GET", "POST"])
    @require_csrf_by_method
    def community_threads(board_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401

        conn = get_db()
        try:
            ensure_community_schema(conn)
            board = conn.execute(
                "SELECT b.*, c.name AS category_name, c.description AS category_description, "
                "c.sort_order AS category_sort_order, c.is_active AS category_is_active, "
                "(SELECT GROUP_CONCAT(username) FROM board_moderators WHERE board_id=b.id) AS moderators, "
                "(SELECT COUNT(*) FROM board_moderators WHERE board_id=b.id) AS moderator_count "
                "FROM forum_boards b LEFT JOIN forum_categories c ON c.id=b.category_id WHERE b.id=?",
                (board_id,)
            ).fetchone()
            if not board:
                return json_resp({"ok": False, "msg": "找不到討論區"}), 404
            manageable = can_moderate_board(conn, board_id, actor)
            accessible = board_is_accessible(board, actor, manageable)
            if not accessible:
                return json_resp({"ok": False, "msg": "權限不足"}), 403

            if request.method == "GET":
                q = (normalize_text(request.args.get("q")) or "")[:120]
                page = parse_positive_int(request.args.get("page", 0), default=0, min_value=0)
                if page is None:
                    return json_resp({"ok": False, "msg": "page 參數格式錯誤"}), 400
                limit = parse_positive_int(request.args.get("limit", 10), default=10, min_value=1, max_value=20)
                if limit is None:
                    return json_resp({"ok": False, "msg": "limit 參數格式錯誤"}), 400
                like = f"%{q}%"
                where = "t.board_id=? AND t.is_deleted=0"
                params = [board_id]
                if manageable:
                    status_filter = normalize_text(request.args.get("status")) or ""
                    if status_filter in ("pending", "approved", "rejected"):
                        where += " AND t.status=?"
                        params.append(status_filter)
                else:
                    where += " AND (t.status='approved' OR t.author_user_id=?)"
                    params.append(actor["id"])
                if q:
                    where += " AND (t.title LIKE ? OR t.content LIKE ? OR t.author_username LIKE ?)"
                    params.extend([like, like, like])
                total = conn.execute(
                    f"SELECT COUNT(*) AS c FROM forum_threads t WHERE {where}",
                    tuple(params)
                ).fetchone()["c"]
                rows = conn.execute(
                    "SELECT t.id, t.board_id, t.title, t.content, t.status, t.review_note, t.reviewed_by, t.reviewed_at, t.post_type, "
                    "t.is_sticky, t.is_locked, t.is_curated, t.view_count, t.author_user_id, t.author_username, "
                    "COALESCE(u.avatar_file_id, '') AS author_avatar_file_id, t.created_at, t.updated_at "
                    "FROM forum_threads t "
                    "LEFT JOIN users u ON u.id=t.author_user_id "
                    f"WHERE {where} ORDER BY t.is_sticky DESC, t.created_at DESC LIMIT ? OFFSET ?",
                    tuple(params + [limit, page * limit])
                ).fetchall()
                thread_ids = [int(row["id"]) for row in rows]
                reply_counts = {}
                reaction_map = {}
                if thread_ids:
                    placeholders = ",".join("?" for _ in thread_ids)
                    reply_counts = {
                        int(row["thread_id"]): int(row["c"] or 0)
                        for row in conn.execute(
                            f"SELECT thread_id, COUNT(*) AS c FROM forum_posts "
                            f"WHERE is_deleted=0 AND thread_id IN ({placeholders}) GROUP BY thread_id",
                            tuple(thread_ids),
                        ).fetchall()
                    }
                    reaction_map = {
                        int(row["thread_id"]): row
                        for row in conn.execute(
                            "SELECT thread_id, "
                            "COALESCE(SUM(CASE WHEN value=1 THEN 1 ELSE 0 END), 0) AS like_count, "
                            "COALESCE(SUM(CASE WHEN value=-1 THEN 1 ELSE 0 END), 0) AS dislike_count, "
                            "COALESCE(MAX(CASE WHEN user_id=? THEN value ELSE 0 END), 0) AS user_reaction "
                            f"FROM forum_thread_reactions WHERE thread_id IN ({placeholders}) GROUP BY thread_id",
                            tuple([actor["id"]] + thread_ids),
                        ).fetchall()
                    }
                threads = []
                for row in rows:
                    item = thread_payload(row)
                    reaction_counts = reaction_map.get(int(row["id"]))
                    item["like_count"] = (reaction_counts["like_count"] if reaction_counts else 0) or 0
                    item["dislike_count"] = (reaction_counts["dislike_count"] if reaction_counts else 0) or 0
                    item["user_reaction"] = (reaction_counts["user_reaction"] if reaction_counts else 0) or 0
                    item["reply_count"] = reply_counts.get(int(row["id"]), 0)
                    threads.append(item)
                can_post_without_review = not thread_requires_review(actor, manageable)
                return json_resp({
                    "ok": True,
                    "board": board_payload(board),
                    "threads": threads,
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "query": q,
                    "can_post_directly": can_post_without_review,
                    "can_post_without_review": can_post_without_review,
                    "requires_thread_review": not can_post_without_review,
                    "can_moderate": manageable,
                })

            if not board["is_active"] and not manageable:
                return json_resp({"ok": False, "msg": "討論區已停用"}), 403
            if board["visibility"] == "private" and not manageable and board["owner_user_id"] != actor["id"]:
                return json_resp({"ok": False, "msg": "此討論區不開放發文"}), 403
            if board["status"] != "approved":
                return json_resp({"ok": False, "msg": "討論區尚未開放"}), 403
            ok, msg, status_code = require_member_action(
                actor,
                "community_thread_create",
                conn=conn,
            )
            if not ok:
                return json_resp({"ok": False, "msg": msg}), status_code
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
            title = normalize_text(data.get("title"))[:120]
            content = normalize_text(data.get("content"))[:4000]
            post_type = normalize_thread_post_type(data.get("post_type"))
            if post_type is None:
                return json_resp({"ok": False, "msg": "文章類型錯誤"}), 400
            if post_type == "announcement" and not manageable:
                return json_resp({"ok": False, "msg": "只有管理員或版主可建立公告型主題"}), 403
            if not title or not content:
                return json_resp({"ok": False, "msg": "主題標題與內容不可為空"}), 400
            if not is_comfyui_board(board):
                sensitive_response = reject_sensitive_community_content(
                    actor,
                    f"{title}\n{content}",
                    content_type="community_thread",
                    reference=f"board_id={board_id}",
                )
                if sensitive_response:
                    return sensitive_response
            blocked, info = check_user_rate_limit(actor["id"], "community_thread_create", max_req=5, window_sec=300)
            if blocked:
                return json_resp({"ok": False, "msg": f"發文太頻繁（{info['limit']} 次 / 5 分鐘）"}), 429
            now = datetime.now().isoformat()
            status = "pending" if thread_requires_review(actor, manageable) else "approved"
            cur = conn.execute(
                "INSERT INTO forum_threads (board_id, title, content, status, post_type, author_user_id, author_username, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (board_id, title, content, status, post_type, actor["id"], actor["username"], now, now)
            )
            conn.execute(
                "UPDATE forum_boards SET last_activity_at=?, updated_at=? WHERE id=?",
                (now, now, board_id)
            )
            conn.commit()
            audit(
                "COMMUNITY_THREAD_CREATE",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"board_id={board_id}, title={title}, status={status}"
            )
            if status == "approved":
                award_points(
                    "create_post",
                    user_id=actor["id"],
                    reference_type="forum_thread",
                    reference_id=cur.lastrowid,
                    actor=actor,
                    metadata={"board_id": board_id, "post_type": post_type},
                )
            return json_resp({"ok": True, "msg": "主題已建立" if status == "approved" else "主題已送審，待管理員核准後公開", "status": status})
        finally:
            conn.close()

    @app.route("/api/community/threads/reviews", methods=["GET"])
    @require_csrf_safe
    def community_thread_reviews():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401

        conn = get_db()
        try:
            ensure_community_schema(conn)
            if can_manage_community(actor):
                rows = conn.execute(
                    "SELECT t.id, t.board_id, t.title, t.content, t.status, t.review_note, t.reviewed_by, t.reviewed_at, t.post_type, "
                    "t.is_sticky, t.is_locked, t.is_curated, t.view_count, "
                    "t.author_user_id, t.author_username, COALESCE(u.avatar_file_id, '') AS author_avatar_file_id, "
                    "t.created_at, t.updated_at "
                    "FROM forum_threads t "
                    "LEFT JOIN users u ON u.id=t.author_user_id "
                    "WHERE t.status='pending' AND t.is_deleted=0 ORDER BY t.created_at ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT t.id, t.board_id, t.title, t.content, t.status, t.review_note, t.reviewed_by, t.reviewed_at, "
                    "t.post_type, t.is_sticky, t.is_locked, t.is_curated, t.view_count, "
                    "t.author_user_id, t.author_username, COALESCE(u.avatar_file_id, '') AS author_avatar_file_id, "
                    "t.created_at, t.updated_at "
                    "FROM forum_threads t "
                    "JOIN board_moderators m ON m.board_id=t.board_id "
                    "LEFT JOIN users u ON u.id=t.author_user_id "
                    "WHERE t.status='pending' AND t.is_deleted=0 AND m.user_id=? AND m.can_review_threads=1 "
                    "ORDER BY t.created_at ASC",
                    (actor["id"],)
                ).fetchall()
                if not rows:
                    exists = conn.execute(
                        "SELECT 1 FROM board_moderators WHERE user_id=? AND can_review_threads=1 LIMIT 1",
                        (actor["id"],)
                    ).fetchone()
                    if not exists:
                        return json_resp({"ok": True, "items": [], "can_review": False})
            return json_resp({"ok": True, "items": [thread_payload(row) for row in rows], "can_review": True})
        finally:
            conn.close()

    @app.route("/api/community/threads/<int:thread_id>/review", methods=["POST"])
    @require_csrf
    def community_thread_review(thread_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        action = normalize_text(data.get("action"))
        note = normalize_text(data.get("note"))[:200]
        if action not in ("approve", "reject"):
            return json_resp({"ok": False, "msg": "審核動作錯誤"}), 400

        conn = get_db()
        try:
            ensure_community_schema(conn)
            row = conn.execute(
                "SELECT id, board_id, title, status, is_deleted FROM forum_threads WHERE id=?",
                (thread_id,)
            ).fetchone()
            if not row:
                return json_resp({"ok": False, "msg": "找不到主題"}), 404
            if not can_moderate_board(conn, row["board_id"], actor, "can_review_threads"):
                return json_resp({"ok": False, "msg": "只有管理員或版主可審核主題"}), 403
            if row["is_deleted"]:
                return json_resp({"ok": False, "msg": "主題已刪除"}), 404
            if row["status"] != "pending":
                return json_resp({"ok": False, "msg": "此主題已審核"}), 409
            new_status = "approved" if action == "approve" else "rejected"
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE forum_threads SET status=?, review_note=?, reviewed_by=?, reviewed_at=?, updated_at=? WHERE id=?",
                (new_status, note or None, actor["username"], now, now, thread_id)
            )
            conn.commit()
            audit(
                "COMMUNITY_THREAD_REVIEW",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"thread_id={thread_id}, action={new_status}"
            )
            return json_resp({"ok": True, "msg": "主題審核完成"})
        finally:
            conn.close()

    @app.route("/api/community/threads/<int:thread_id>", methods=["GET", "PUT", "DELETE"])
    @require_csrf_by_method
    def community_thread_detail(thread_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401

        conn = get_db()
        try:
            ensure_community_schema(conn)
            thread = conn.execute(
                "SELECT t.id, t.board_id, t.title, t.content, t.status, t.review_note, t.reviewed_by, t.reviewed_at, "
                "t.post_type, t.is_sticky, t.is_curated, t.view_count, "
                "t.author_user_id, t.author_username, COALESCE(tu.avatar_file_id, '') AS author_avatar_file_id, "
                "t.created_at, t.updated_at, t.is_locked, t.edited_at, t.edited_by, "
                "t.is_deleted, t.deleted_at, t.deleted_by, t.delete_reason, "
                "b.status AS board_status, b.owner_user_id, b.owner_username, b.title AS board_title, "
                "b.visibility AS board_visibility, b.is_active AS board_is_active "
                "FROM forum_threads t "
                "JOIN forum_boards b ON b.id=t.board_id "
                "LEFT JOIN users tu ON tu.id=t.author_user_id "
                "WHERE t.id=?",
                (thread_id,)
            ).fetchone()
            if not thread:
                return json_resp({"ok": False, "msg": "找不到主題"}), 404
            manageable = can_moderate_board(conn, thread["board_id"], actor)
            accessible = manageable or (
                bool(thread["board_is_active"]) and (
                    (thread["board_status"] == "approved" and thread["board_visibility"] == "public") or
                    thread["owner_user_id"] == actor["id"]
                )
            )
            if not accessible:
                return json_resp({"ok": False, "msg": "權限不足"}), 403
            if thread["status"] != "approved" and not manageable and thread["author_user_id"] != actor["id"]:
                return json_resp({"ok": False, "msg": "此主題尚未公開"}), 403
            if thread["is_deleted"] and not manageable:
                return json_resp({"ok": False, "msg": "找不到主題"}), 404

            if request.method == "PUT":
                if thread["is_deleted"]:
                    return json_resp({"ok": False, "msg": "已刪除主題不可編輯"}), 409
                if bool(thread["is_locked"]) and not manageable:
                    return json_resp({"ok": False, "msg": "此主題已鎖定，不可編輯"}), 403
                if not can_moderate_board(conn, thread["board_id"], actor, "can_edit_posts") and not can_edit_community_content(actor, thread["author_user_id"]):
                    return json_resp({"ok": False, "msg": "你沒有編輯此主題的權限"}), 403
                try:
                    data = request.get_json(force=True)
                except Exception:
                    return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
                title = normalize_text(data.get("title"))[:120]
                content = normalize_text(data.get("content"))[:4000]
                if not title or not content:
                    return json_resp({"ok": False, "msg": "主題標題與內容不可為空"}), 400
                if not is_comfyui_board(thread):
                    sensitive_response = reject_sensitive_community_content(
                        actor,
                        f"{title}\n{content}",
                        content_type="community_thread_edit",
                        reference=f"thread_id={thread_id}",
                    )
                    if sensitive_response:
                        return sensitive_response
                now = datetime.now().isoformat()
                conn.execute(
                    "UPDATE forum_threads SET title=?, content=?, edited_at=?, edited_by=?, updated_at=? WHERE id=?",
                    (title, content, now, actor["username"], now, thread_id)
                )
                conn.commit()
                audit(
                    "COMMUNITY_THREAD_UPDATE",
                    get_client_ip(),
                    user=actor["username"],
                    success=True,
                    ua=get_ua(),
                    detail=f"thread_id={thread_id}"
                )
                return json_resp({"ok": True, "msg": "主題已更新"})

            if request.method == "DELETE":
                if thread["is_deleted"]:
                    return json_resp({"ok": False, "msg": "主題已刪除"}), 409
                if not can_moderate_board(conn, thread["board_id"], actor, "can_delete_posts") and not can_delete_community_content(actor, thread["author_user_id"], thread["owner_user_id"]):
                    return json_resp({"ok": False, "msg": "你沒有刪除此主題的權限"}), 403
                now = datetime.now().isoformat()
                reason = ""
                if request.is_json:
                    try:
                        reason = normalize_text((request.get_json(silent=True) or {}).get("reason"))[:200]
                    except Exception:
                        reason = ""
                conn.execute(
                    "UPDATE forum_threads SET is_deleted=1, deleted_at=?, deleted_by=?, delete_reason=?, updated_at=? WHERE id=?",
                    (now, actor["username"], reason or None, now, thread_id)
                )
                conn.commit()
                audit(
                    "COMMUNITY_THREAD_DELETE",
                    get_client_ip(),
                    user=actor["username"],
                    success=True,
                    ua=get_ua(),
                    detail=f"thread_id={thread_id}, title={thread['title']}"
                )
                return json_resp({"ok": True, "msg": "主題已刪除"})

            post_where = "p.thread_id=? AND p.is_deleted=0"
            post_params = [thread_id]
            if not manageable:
                post_where += " AND p.is_hidden=0"
            posts_page = parse_positive_int(request.args.get("posts_page", request.args.get("post_page", 0)), default=0, min_value=0)
            if posts_page is None:
                return json_resp({"ok": False, "msg": "posts_page 參數格式錯誤"}), 400
            posts_limit = parse_positive_int(request.args.get("posts_limit", request.args.get("post_limit", 100)), default=100, min_value=1, max_value=200)
            if posts_limit is None:
                return json_resp({"ok": False, "msg": "posts_limit 參數格式錯誤"}), 400
            posts_total = conn.execute(
                f"SELECT COUNT(*) AS c FROM forum_posts p WHERE {post_where}",
                tuple(post_params),
            ).fetchone()["c"]
            posts = conn.execute(
                "SELECT p.id, p.content, p.author_user_id, p.author_username, COALESCE(u.avatar_file_id, '') AS author_avatar_file_id, p.is_pinned, p.is_hidden, p.hidden_reason, "
                "p.created_at, p.updated_at, "
                "COALESCE(SUM(CASE WHEN r.value=1 THEN 1 ELSE 0 END), 0) AS like_count, "
                "COALESCE(SUM(CASE WHEN r.value=-1 THEN 1 ELSE 0 END), 0) AS dislike_count, "
                "COALESCE(MAX(CASE WHEN r.user_id=? THEN r.value ELSE 0 END), 0) AS user_reaction "
                "FROM forum_posts p "
                "LEFT JOIN users u ON u.id=p.author_user_id "
                "LEFT JOIN forum_post_reactions r ON r.post_id=p.id "
                f"WHERE {post_where} "
                "GROUP BY p.id "
                "ORDER BY p.is_pinned DESC, p.created_at ASC "
                "LIMIT ? OFFSET ?",
                tuple([actor["id"]] + post_params + [posts_limit, posts_page * posts_limit])
            ).fetchall()
            thread_reactions = conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN value=1 THEN 1 ELSE 0 END), 0) AS like_count, "
                "COALESCE(SUM(CASE WHEN value=-1 THEN 1 ELSE 0 END), 0) AS dislike_count, "
                "COALESCE(MAX(CASE WHEN user_id=? THEN value ELSE 0 END), 0) AS user_reaction "
                "FROM forum_thread_reactions WHERE thread_id=?",
                (actor["id"], thread_id)
            ).fetchone()
            counted_view = record_thread_view(conn, thread_id, actor)
            conn.commit()
            refreshed_view = conn.execute("SELECT view_count FROM forum_threads WHERE id=?", (thread_id,)).fetchone()
            moderator_permissions = board_moderator_permissions(conn, thread["board_id"], actor)
            return json_resp({
                "ok": True,
                "thread": {
                    "id": thread["id"],
                    "board_id": thread["board_id"],
                    "board_title": thread["board_title"],
                    "title": thread["title"],
                    "content": thread["content"],
                    "status": thread["status"],
                    "review_note": thread["review_note"],
                    "reviewed_by": thread["reviewed_by"],
                    "reviewed_at": thread["reviewed_at"],
                    "post_type": thread["post_type"],
                    "is_sticky": bool(thread["is_sticky"]),
                    "is_locked": bool(thread["is_locked"]),
                    "is_curated": bool(thread["is_curated"]),
                    "view_count": int(refreshed_view["view_count"] or 0),
                    "view_counted": counted_view,
                    "like_count": thread_reactions["like_count"] or 0,
                    "dislike_count": thread_reactions["dislike_count"] or 0,
                    "user_reaction": thread_reactions["user_reaction"] or 0,
                    "edited_at": thread["edited_at"],
                    "edited_by": thread["edited_by"],
                    "is_deleted": bool(thread["is_deleted"]),
                    "deleted_at": thread["deleted_at"],
                    "deleted_by": thread["deleted_by"],
                    "delete_reason": thread["delete_reason"],
                    "author_user_id": thread["author_user_id"],
                    "author_username": thread["author_username"],
                    "author_avatar_file_id": thread["author_avatar_file_id"] or "",
                    "created_at": thread["created_at"],
                    "updated_at": thread["updated_at"],
                    "board_status": thread["board_status"],
                    "can_moderate": manageable,
                    "moderator_permissions": moderator_permissions,
                },
                "posts": [{
                    "id": row["id"],
                    "content": row["content"],
                    "author_user_id": row["author_user_id"],
                    "author_username": row["author_username"],
                    "author_avatar_file_id": row["author_avatar_file_id"] or "",
                    "is_pinned": bool(row["is_pinned"]),
                    "is_hidden": bool(row["is_hidden"]),
                    "hidden_reason": row["hidden_reason"],
                    "is_deleted": False,
                    "like_count": row["like_count"],
                    "dislike_count": row["dislike_count"],
                    "user_reaction": row["user_reaction"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                } for row in posts],
                "posts_total": int(posts_total or 0),
                "posts_page": posts_page,
                "posts_limit": posts_limit,
                "posts_has_more": (posts_page + 1) * posts_limit < int(posts_total or 0),
            })
        finally:
            conn.close()

    @app.route("/api/community/threads/<int:thread_id>/reaction", methods=["POST"])
    @require_csrf
    def community_thread_reaction(thread_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        value = data.get("value") if isinstance(data, dict) else None
        if value not in (-1, 0, 1):
            return json_resp({"ok": False, "msg": "反應值錯誤"}), 400

        conn = get_db()
        try:
            ensure_community_schema(conn)
            ok, msg, status_code = require_member_action(actor, "community_reaction", conn=conn)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), status_code
            thread = conn.execute(
                "SELECT t.id, t.board_id, t.status, t.is_deleted, b.status AS board_status, "
                "b.owner_user_id, b.visibility AS board_visibility, b.is_active AS board_is_active "
                "FROM forum_threads t JOIN forum_boards b ON b.id=t.board_id WHERE t.id=?",
                (thread_id,)
            ).fetchone()
            if not thread or thread["is_deleted"]:
                return json_resp({"ok": False, "msg": "找不到主題"}), 404
            manageable = can_moderate_board(conn, thread["board_id"], actor)
            accessible = manageable or (
                bool(thread["board_is_active"]) and (
                    (thread["board_status"] == "approved" and thread["board_visibility"] == "public") or
                    thread["owner_user_id"] == actor["id"]
                )
            )
            if not accessible or (thread["status"] != "approved" and not manageable):
                return json_resp({"ok": False, "msg": "權限不足"}), 403

            now = datetime.now().isoformat()
            if value == 0:
                conn.execute(
                    "DELETE FROM forum_thread_reactions WHERE thread_id=? AND user_id=?",
                    (thread_id, actor["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO forum_thread_reactions (thread_id, user_id, value, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(thread_id, user_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (thread_id, actor["id"], value, now, now)
                )
            counts = conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN value=1 THEN 1 ELSE 0 END), 0) AS like_count, "
                "COALESCE(SUM(CASE WHEN value=-1 THEN 1 ELSE 0 END), 0) AS dislike_count "
                "FROM forum_thread_reactions WHERE thread_id=?",
                (thread_id,)
            ).fetchone()
            conn.commit()
            return json_resp({
                "ok": True,
                "msg": "已更新主題反應",
                "like_count": counts["like_count"] or 0,
                "dislike_count": counts["dislike_count"] or 0,
                "user_reaction": value,
            })
        finally:
            conn.close()

    @app.route("/api/community/threads/<int:thread_id>/reward", methods=["POST"])
    @require_csrf
    def community_thread_reward(thread_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        points = parse_positive_int(data.get("points", 1), default=1, min_value=1, max_value=50)
        if points is None:
            return json_resp({"ok": False, "msg": "獎勵點數必須介於 1 到 50"}), 400
        reason = normalize_text(data.get("reason"))[:160] or "優質主題貢獻"

        conn = get_db()
        try:
            ensure_community_schema(conn)
            thread = conn.execute(
                "SELECT id, board_id, title, author_user_id, author_username, is_deleted FROM forum_threads WHERE id=?",
                (thread_id,)
            ).fetchone()
            if not thread or thread["is_deleted"]:
                return json_resp({"ok": False, "msg": "找不到主題"}), 404
            if not can_moderate_board(conn, thread["board_id"], actor, "can_reward_authors"):
                return json_resp({"ok": False, "msg": "只有管理員或版主可獎勵主題作者"}), 403
            ensure_user_reputation_columns(conn)
            add_reputation_event(
                conn,
                user_id=thread["author_user_id"],
                delta=points,
                reason=f"forum_thread_reward:{reason}",
                source_user_id=actor["id"],
                source_post_id=thread_id,
            )
            record_moderation_action(
                conn,
                moderator_id=actor["id"],
                action_type="reward_thread_author",
                target_type="forum_thread",
                target_id=thread_id,
                reason=reason,
            )
            conn.commit()
            audit("COMMUNITY_THREAD_REWARD", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"thread_id={thread_id}, author={thread['author_username']}, points={points}")
            return json_resp({"ok": True, "msg": "已獎勵主題作者", "points": points})
        finally:
            conn.close()

    @app.route("/api/community/threads/<int:thread_id>/posts", methods=["POST"])
    @require_csrf
    def community_thread_reply(thread_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        content = normalize_text(data.get("content"))[:3000]
        if not content:
            return json_resp({"ok": False, "msg": "留言內容不可為空"}), 400
        blocked, info = check_user_rate_limit(actor["id"], "community_thread_reply", max_req=10, window_sec=300)
        if blocked:
            return json_resp({"ok": False, "msg": f"留言太頻繁（{info['limit']} 次 / 5 分鐘）"}), 429

        conn = get_db()
        try:
            ensure_community_schema(conn)
            ok, msg, status_code = require_member_action(actor, "community_reply", conn=conn)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), status_code
            thread = conn.execute(
                "SELECT t.id, t.board_id, t.status, t.is_locked, t.is_deleted, b.status AS board_status, b.visibility AS board_visibility, "
                "b.is_active AS board_is_active, b.owner_user_id FROM forum_threads t "
                "JOIN forum_boards b ON b.id=t.board_id WHERE t.id=?",
                (thread_id,)
            ).fetchone()
            if not thread:
                return json_resp({"ok": False, "msg": "找不到主題"}), 404
            if thread["is_deleted"]:
                return json_resp({"ok": False, "msg": "主題已刪除，不能留言"}), 404
            manageable = can_moderate_board(conn, thread["board_id"], actor)
            board_open = (
                bool(thread["board_is_active"])
                and thread["board_status"] == "approved"
                and thread["board_visibility"] == "public"
            )
            accessible = manageable or board_open or thread["owner_user_id"] == actor["id"]
            if not accessible:
                return json_resp({"ok": False, "msg": "此討論區不開放留言"}), 403
            if thread["status"] != "approved":
                return json_resp({"ok": False, "msg": "此主題尚未公開留言"}), 403
            if bool(thread["is_locked"]):
                return json_resp({"ok": False, "msg": "此主題已鎖定，暫停留言"}), 403
            sensitive_response = reject_sensitive_community_content(
                actor,
                content,
                content_type="community_reply",
                reference=f"thread_id={thread_id}",
            )
            if sensitive_response:
                return sensitive_response
            now = datetime.now().isoformat()
            cur = conn.execute(
                "INSERT INTO forum_posts (thread_id, content, author_user_id, author_username, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, content, actor["id"], actor["username"], now, now)
            )
            conn.execute(
                "UPDATE forum_boards SET last_activity_at=?, updated_at=? WHERE id=?",
                (now, now, thread["board_id"])
            )
            conn.commit()
            audit("COMMUNITY_THREAD_REPLY", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"thread_id={thread_id}")
            award_points(
                "create_comment",
                user_id=actor["id"],
                reference_type="forum_post",
                reference_id=cur.lastrowid,
                actor=actor,
                metadata={"thread_id": thread_id, "board_id": thread["board_id"]},
            )
            return json_resp({"ok": True, "msg": "留言已送出"})
        finally:
            conn.close()

    @app.route("/api/community/posts/<int:post_id>/reaction", methods=["POST"])
    @require_csrf
    def community_post_reaction(post_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        value = data.get("value") if isinstance(data, dict) else None
        if value not in (-1, 0, 1):
            return json_resp({"ok": False, "msg": "反應值錯誤"}), 400

        conn = get_db()
        try:
            ensure_community_schema(conn)
            ok, msg, status_code = require_member_action(actor, "community_reaction", conn=conn)
            if not ok:
                return json_resp({"ok": False, "msg": msg}), status_code
            post = conn.execute(
                "SELECT p.id, p.thread_id, p.author_user_id, p.is_hidden, p.is_deleted, t.board_id, t.status AS thread_status, t.is_deleted AS thread_is_deleted, b.status AS board_status, "
                "b.owner_user_id, b.visibility AS board_visibility, b.is_active AS board_is_active "
                "FROM forum_posts p "
                "JOIN forum_threads t ON t.id=p.thread_id "
                "JOIN forum_boards b ON b.id=t.board_id "
                "WHERE p.id=?",
                (post_id,)
            ).fetchone()
            if not post:
                return json_resp({"ok": False, "msg": "找不到留言"}), 404
            if post["is_deleted"] or post["thread_is_deleted"]:
                return json_resp({"ok": False, "msg": "找不到留言"}), 404
            manageable = can_moderate_board(conn, post["board_id"], actor)
            accessible = manageable or (
                bool(post["board_is_active"]) and (
                    (post["board_status"] == "approved" and post["board_visibility"] == "public") or
                    post["owner_user_id"] == actor["id"]
                )
            )
            if not accessible or (post["thread_status"] != "approved" and not manageable):
                return json_resp({"ok": False, "msg": "權限不足"}), 403
            if post["is_hidden"] and not manageable:
                return json_resp({"ok": False, "msg": "留言已隱藏"}), 403

            now = datetime.now().isoformat()
            previous_reaction = conn.execute(
                "SELECT value FROM forum_post_reactions WHERE post_id=? AND user_id=?",
                (post_id, actor["id"]),
            ).fetchone()
            previous_value = int(previous_reaction["value"]) if previous_reaction else 0
            if value == 0:
                conn.execute(
                    "DELETE FROM forum_post_reactions WHERE post_id=? AND user_id=?",
                    (post_id, actor["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO forum_post_reactions (post_id, user_id, value, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(post_id, user_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (post_id, actor["id"], value, now, now)
                )

            counts = conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN value=1 THEN 1 ELSE 0 END), 0) AS like_count, "
                "COALESCE(SUM(CASE WHEN value=-1 THEN 1 ELSE 0 END), 0) AS dislike_count "
                "FROM forum_post_reactions WHERE post_id=?",
                (post_id,)
            ).fetchone()
            like_count = counts["like_count"] or 0
            dislike_count = counts["dislike_count"] or 0
            auto_hidden = False
            auto_hide_threshold = community_post_auto_hide_threshold(conn)
            if dislike_count >= auto_hide_threshold and not post["is_hidden"]:
                auto_hidden = True
                reason = f"倒讚過多自動隱藏（{dislike_count}/{auto_hide_threshold}）"
                conn.execute(
                    "UPDATE forum_posts SET is_hidden=1, hidden_reason=?, updated_at=? WHERE id=?",
                    (reason, now, post_id)
                )
                ensure_auto_hidden_post_report(conn, post_id, actor["id"], dislike_count, auto_hide_threshold)
            conn.commit()
            if auto_hidden:
                audit("COMMUNITY_POST_AUTO_HIDDEN", get_client_ip(), user="system", success=True, ua=get_ua(),
                      detail=f"post_id={post_id}, dislikes={dislike_count}")
            if value == 1 and previous_value != 1 and int(post["author_user_id"]) != int(actor["id"]):
                award_points(
                    "receive_like",
                    user_id=post["author_user_id"],
                    reference_type="forum_post_reaction",
                    reference_id=f"{post_id}:{actor['id']}",
                    actor=actor,
                    metadata={"post_id": post_id, "reactor_user_id": actor["id"]},
                )
            return json_resp({
                "ok": True,
                "msg": "已更新反應",
                "like_count": like_count,
                "dislike_count": dislike_count,
                "user_reaction": value,
                "auto_hidden": auto_hidden,
            })
        finally:
            conn.close()

    @app.route("/api/community/posts/<int:post_id>/pin", methods=["POST"])
    @require_csrf
    def community_pin_post(post_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        pinned = 1 if isinstance(data, dict) and bool(data.get("pinned")) else 0

        conn = get_db()
        try:
            ensure_community_schema(conn)
            post = conn.execute(
                "SELECT p.id, p.thread_id, p.is_deleted, t.board_id FROM forum_posts p "
                "JOIN forum_threads t ON t.id=p.thread_id WHERE p.id=?",
                (post_id,)
            ).fetchone()
            if not post:
                return json_resp({"ok": False, "msg": "找不到留言"}), 404
            if not can_moderate_board(conn, post["board_id"], actor, "can_pin_posts"):
                return json_resp({"ok": False, "msg": "只有管理員或版主可置頂留言"}), 403
            if post["is_deleted"]:
                return json_resp({"ok": False, "msg": "已刪除留言不可置頂"}), 409
            conn.execute(
                "UPDATE forum_posts SET is_pinned=?, updated_at=? WHERE id=?",
                (pinned, datetime.now().isoformat(), post_id)
            )
            conn.commit()
            audit("COMMUNITY_POST_PIN", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"post_id={post_id}, pinned={pinned}")
            return json_resp({"ok": True, "msg": "留言已置頂" if pinned else "留言已取消置頂"})
        finally:
            conn.close()

    @app.route("/api/community/posts/<int:post_id>/penalty", methods=["POST"])
    @require_csrf
    def community_post_penalty(post_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        points = parse_positive_int(data.get("points", 1), default=1, min_value=1, max_value=10)
        if points is None:
            return json_resp({"ok": False, "msg": "違規點數必須介於 1 到 10"}), 400
        reason = normalize_text(data.get("reason"))[:200] or "討論區違規留言"

        conn = get_db()
        try:
            ensure_community_schema(conn)
            post = conn.execute(
                "SELECT p.id, p.thread_id, p.content, p.author_user_id, p.author_username, p.is_deleted, "
                "t.board_id, t.is_deleted AS thread_is_deleted, u.role AS author_role "
                "FROM forum_posts p "
                "JOIN forum_threads t ON t.id=p.thread_id "
                "JOIN users u ON u.id=p.author_user_id "
                "WHERE p.id=?",
                (post_id,)
            ).fetchone()
            if not post or post["is_deleted"] or post["thread_is_deleted"]:
                return json_resp({"ok": False, "msg": "找不到留言"}), 404
            if not can_moderate_board(conn, post["board_id"], actor, "can_penalize_posts"):
                return json_resp({"ok": False, "msg": "只有管理員或版主可懲處違規留言"}), 403
            if post["author_username"] == "root":
                return json_resp({"ok": False, "msg": "無法對 root 計點"}), 403
            if actor_role(actor) == "user" and post["author_role"] != "user":
                return json_resp({"ok": False, "msg": "版主只能懲處一般帳戶"}), 403

            if callable(add_violation):
                conn.commit()
                add_violation(
                    post["author_user_id"],
                    post["author_username"],
                    post["author_role"],
                    points=points,
                    reason=reason,
                    triggered_by="forum_moderator",
                    actor_username=actor["username"],
                )
            else:
                ensure_user_violation_columns(conn)
                conn.execute(
                    "UPDATE users SET violation_count=COALESCE(violation_count, 0)+?, updated_at=? WHERE id=?",
                    (points, datetime.now().isoformat(), post["author_user_id"])
                )
            record_moderation_action(
                conn,
                moderator_id=actor["id"],
                action_type="penalize_post_author",
                target_type="forum_post",
                target_id=post_id,
                reason=reason,
            )
            conn.commit()
            audit("COMMUNITY_POST_PENALTY", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"post_id={post_id}, author={post['author_username']}, points={points}")
            return json_resp({"ok": True, "msg": "已對違規留言作者計點", "points": points})
        finally:
            conn.close()

    @app.route("/api/community/posts/<int:post_id>", methods=["PUT", "DELETE"])
    @require_csrf
    def community_delete_post(post_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401

        conn = get_db()
        try:
            ensure_community_schema(conn)
            post = conn.execute(
                "SELECT p.id, p.thread_id, p.content, p.author_user_id, p.author_username, p.is_deleted, "
                "t.board_id, t.author_user_id AS thread_author_user_id, t.is_locked, t.is_deleted AS thread_is_deleted, "
                "b.owner_user_id, b.status AS board_status "
                "FROM forum_posts p "
                "JOIN forum_threads t ON t.id=p.thread_id "
                "JOIN forum_boards b ON b.id=t.board_id "
                "WHERE p.id=?",
                (post_id,)
            ).fetchone()
            if not post:
                return json_resp({"ok": False, "msg": "找不到留言"}), 404
            if post["is_deleted"] or post["thread_is_deleted"]:
                return json_resp({"ok": False, "msg": "找不到留言"}), 404
            board_moderator = can_moderate_board(conn, post["board_id"], actor)
            accessible = post["board_status"] == "approved" or board_moderator or post["owner_user_id"] == actor["id"]
            if not accessible:
                return json_resp({"ok": False, "msg": "權限不足"}), 403

            if request.method == "PUT":
                manageable = can_moderate_board(conn, post["board_id"], actor, "can_edit_posts")
                if bool(post["is_locked"]) and not manageable:
                    return json_resp({"ok": False, "msg": "此主題已鎖定，不可編輯留言"}), 403
                if not manageable and not can_edit_community_content(actor, post["author_user_id"]):
                    return json_resp({"ok": False, "msg": "你沒有編輯此留言的權限"}), 403
                try:
                    data = request.get_json(force=True)
                except Exception:
                    return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
                content = normalize_text(data.get("content"))[:3000]
                if not content:
                    return json_resp({"ok": False, "msg": "留言內容不可為空"}), 400
                sensitive_response = reject_sensitive_community_content(
                    actor,
                    content,
                    content_type="community_reply_edit",
                    reference=f"post_id={post_id}",
                )
                if sensitive_response:
                    return sensitive_response
                now = datetime.now().isoformat()
                conn.execute(
                    "UPDATE forum_posts SET content=?, edited_at=?, edited_by=?, updated_at=? WHERE id=?",
                    (content, now, actor["username"], now, post_id)
                )
                conn.commit()
                audit(
                    "COMMUNITY_POST_UPDATE",
                    get_client_ip(),
                    user=actor["username"],
                    success=True,
                    ua=get_ua(),
                    detail=f"post_id={post_id}, thread_id={post['thread_id']}"
                )
                return json_resp({"ok": True, "msg": "留言已更新"})

            if not can_moderate_board(conn, post["board_id"], actor, "can_delete_posts") and not can_delete_community_content(actor, post["author_user_id"], post["owner_user_id"]):
                return json_resp({"ok": False, "msg": "你沒有刪除此留言的權限"}), 403

            now = datetime.now().isoformat()
            reason = ""
            if request.is_json:
                try:
                    reason = normalize_text((request.get_json(silent=True) or {}).get("reason"))[:200]
                except Exception:
                    reason = ""
            conn.execute(
                "UPDATE forum_posts SET is_deleted=1, deleted_at=?, deleted_by=?, delete_reason=?, updated_at=? WHERE id=?",
                (now, actor["username"], reason or None, now, post_id)
            )
            conn.commit()
            audit(
                "COMMUNITY_POST_DELETE",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"post_id={post_id}, thread_id={post['thread_id']}, author={post['author_username']}"
            )
            return json_resp({"ok": True, "msg": "留言已刪除"})
        finally:
            conn.close()

    @app.route("/api/community/threads/<int:thread_id>/lock", methods=["POST"])
    @require_csrf
    def community_thread_lock(thread_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if "locked" in data:
            locked_value = data.get("locked")
        elif "is_locked" in data:
            locked_value = data.get("is_locked")
        else:
            return json_resp({"ok": False, "msg": "請指定 locked 狀態"}), 400
        locked = 1 if normalize_payload_bool(locked_value) else 0
        conn = get_db()
        try:
            ensure_community_schema(conn)
            thread = conn.execute("SELECT id, board_id, title, is_deleted FROM forum_threads WHERE id=?", (thread_id,)).fetchone()
            if not thread:
                return json_resp({"ok": False, "msg": "找不到主題"}), 404
            if not can_moderate_board(conn, thread["board_id"], actor, "can_lock_threads"):
                return json_resp({"ok": False, "msg": "只有管理員或版主可鎖定主題"}), 403
            if thread["is_deleted"]:
                return json_resp({"ok": False, "msg": "已刪除主題不可鎖定"}), 409
            conn.execute(
                "UPDATE forum_threads SET is_locked=?, updated_at=? WHERE id=?",
                (locked, datetime.now().isoformat(), thread_id)
            )
            conn.commit()
            audit("COMMUNITY_THREAD_LOCK", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"thread_id={thread_id}, locked={locked}")
            return json_resp({"ok": True, "msg": "主題狀態已更新", "locked": bool(locked), "is_locked": bool(locked)})
        finally:
            conn.close()

    @app.route("/api/community/threads/<int:thread_id>/sticky", methods=["POST"])
    @require_csrf
    def community_thread_sticky(thread_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        sticky = 1 if bool(data.get("sticky")) else 0
        conn = get_db()
        try:
            ensure_community_schema(conn)
            thread = conn.execute("SELECT id, board_id, title, is_deleted FROM forum_threads WHERE id=?", (thread_id,)).fetchone()
            if not thread:
                return json_resp({"ok": False, "msg": "找不到主題"}), 404
            if not can_moderate_board(conn, thread["board_id"], actor, "can_pin_threads"):
                return json_resp({"ok": False, "msg": "只有管理員或版主可置頂主題"}), 403
            if thread["is_deleted"]:
                return json_resp({"ok": False, "msg": "已刪除主題不可置頂"}), 409
            conn.execute(
                "UPDATE forum_threads SET is_sticky=?, updated_at=? WHERE id=?",
                (sticky, datetime.now().isoformat(), thread_id)
            )
            conn.commit()
            audit("COMMUNITY_THREAD_STICKY", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"thread_id={thread_id}, sticky={sticky}")
            return json_resp({"ok": True, "msg": "主題已置頂" if sticky else "主題已取消置頂"})
        finally:
            conn.close()

    @app.route("/api/community/threads/<int:thread_id>/curate", methods=["POST"])
    @require_csrf
    def community_thread_curate(thread_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        curated = 1 if bool(data.get("curated")) else 0
        conn = get_db()
        try:
            ensure_community_schema(conn)
            thread = conn.execute("SELECT id, board_id, title, is_deleted FROM forum_threads WHERE id=?", (thread_id,)).fetchone()
            if not thread:
                return json_resp({"ok": False, "msg": "找不到主題"}), 404
            if not can_moderate_board(conn, thread["board_id"], actor, "can_pin_posts"):
                return json_resp({"ok": False, "msg": "只有管理員或版主可設定精華主題"}), 403
            if thread["is_deleted"]:
                return json_resp({"ok": False, "msg": "已刪除主題不可設定精華"}), 409
            conn.execute(
                "UPDATE forum_threads SET is_curated=?, updated_at=? WHERE id=?",
                (curated, datetime.now().isoformat(), thread_id)
            )
            conn.commit()
            audit("COMMUNITY_THREAD_CURATE", get_client_ip(), user=actor["username"], success=True, ua=get_ua(),
                  detail=f"thread_id={thread_id}, curated={curated}")
            return json_resp({"ok": True, "msg": "主題已加入精華" if curated else "主題已移出精華"})
        finally:
            conn.close()
