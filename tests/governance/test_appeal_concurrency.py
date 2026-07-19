from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request

from routes.appeals import register_appeal_routes
from services.governance.sanction_notices import ensure_admin_sanction_appeal_schema
from services.server.database import ensure_appeal_columns


_USER = {
    "id": 3,
    "username": "alice",
    "role": "user",
    "status": "active",
}
_ROOT = {
    "id": 1,
    "username": "root",
    "role": "super_admin",
    "status": "active",
}


class _BarrierCursor:
    """Align stale-read races without deadlocking a fixed BEGIN IMMEDIATE path."""

    def __init__(self, cursor, connection, barrier):
        self._cursor = cursor
        self._connection = connection
        self._barrier = barrier

    def fetchone(self):
        row = self._cursor.fetchone()
        # A correct implementation may claim the row inside BEGIN IMMEDIATE (or
        # via an UPDATE) before reading it.  In that case the connection already
        # owns a write transaction and deliberately must not wait for a peer that
        # SQLite is keeping behind the same lock.  The barrier only makes the
        # legacy check-then-write race deterministic.
        if not self._connection.in_transaction:
            self._barrier.wait(timeout=10)
        return row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _RouteConnection:
    def __init__(self, connection, *, stale_read_barrier=None, barrier_query=""):
        self._connection = connection
        self._stale_read_barrier = stale_read_barrier
        self._barrier_query = " ".join(str(barrier_query).lower().split())
        self._barrier_used = False

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        normalized = " ".join(str(sql).lower().split())
        if (
            self._stale_read_barrier is not None
            and not self._barrier_used
            and self._barrier_query in normalized
        ):
            self._barrier_used = True
            return _BarrierCursor(cursor, self._connection, self._stale_read_barrier)
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _connect(path):
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_db(path):
    conn = _connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            role TEXT NOT NULL DEFAULT 'user',
            violation_count INTEGER NOT NULL DEFAULT 0,
            blocked_until TEXT,
            base_level TEXT NOT NULL DEFAULT 'normal',
            member_level TEXT NOT NULL DEFAULT 'normal',
            effective_level TEXT NOT NULL DEFAULT 'normal',
            sanction_status TEXT NOT NULL DEFAULT 'none',
            sanction_until TEXT,
            level_update_reason TEXT,
            updated_at TEXT
        );

        CREATE TABLE secure_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            points INTEGER NOT NULL,
            reason TEXT NOT NULL,
            triggered_by TEXT NOT NULL,
            actor_username TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        -- Deliberately mirrors an old runtime.  The production migration must
        -- add the unique (user_id, latest_violation_id) constraint/index.
        CREATE TABLE violation_appeals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            latest_violation_id INTEGER,
            violation_count_snapshot INTEGER NOT NULL DEFAULT 0,
            penalty_points INTEGER NOT NULL DEFAULT 0,
            pre_status TEXT NOT NULL DEFAULT 'active',
            pre_role TEXT NOT NULL DEFAULT 'user',
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_by TEXT,
            reviewed_at TEXT,
            review_note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    conn.executemany(
        "INSERT INTO users (id, username, role, status, violation_count) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "root", "super_admin", "active", 0),
            (3, "alice", "user", "active", 9),
            (4, "bob", "user", "active", 0),
        ],
    )
    conn.execute(
        """
        INSERT INTO secure_violations (
            id, user_id, username, points, reason, triggered_by,
            actor_username, created_at
        ) VALUES (41, 3, 'alice', 2, 'concurrency fixture', 'manager', 'admin', ?)
        """,
        (datetime.now().isoformat(),),
    )
    ensure_appeal_columns(conn)
    ensure_admin_sanction_appeal_schema(conn)
    conn.commit()
    conn.close()


def _identity_decorator(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapped


def _parse_positive_int(value, max_value=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or (max_value is not None and parsed > max_value):
        return None
    return parsed


def _actor_from_request():
    actor = request.headers.get("X-Test-Actor", "")
    if actor == "root":
        return dict(_ROOT)
    if actor == "alice":
        return dict(_USER)
    if actor == "bob":
        return {"id": 4, "username": "bob", "role": "user", "status": "active"}
    return None


def _build_app(path, audits, *, stale_read_barrier=None, barrier_query=""):
    app = Flask(__name__)
    app.config.update(TESTING=True)

    def get_db():
        conn = _connect(path)
        if stale_read_barrier is None:
            return conn
        return _RouteConnection(
            conn,
            stale_read_barrier=stale_read_barrier,
            barrier_query=barrier_query,
        )

    def latest_violation(conn, user_id):
        return conn.execute(
            """
            SELECT id, user_id, username, points, reason, triggered_by,
                   actor_username, created_at
            FROM secure_violations WHERE user_id=? ORDER BY id DESC LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()

    register_appeal_routes(
        app,
        {
            "VIOLATION_APPEAL_WINDOW_HOURS": 24,
            "audit": lambda *args, **kwargs: audits.append((args, kwargs)),
            "check_user_rate_limit": lambda *_args, **_kwargs: (False, {"limit": 5}),
            "get_client_ip": lambda: "127.0.0.1",
            "get_current_user_ctx": _actor_from_request,
            "get_db": get_db,
            "get_latest_violation": latest_violation,
            "json_resp": jsonify,
            "normalize_text": lambda value: str(value or "").strip(),
            "parse_iso_to_datetime": lambda value: datetime.fromisoformat(value) if value else None,
            "parse_positive_int": _parse_positive_int,
            "points_service": None,
            "require_csrf": _identity_decorator,
            "require_csrf_safe": _identity_decorator,
            "role_rank": lambda role: {"user": 1, "manager": 2, "super_admin": 3}.get(role, 0),
        },
    )
    return app


def _insert_pending_appeal(path, *, snapshot=3, penalty=2):
    conn = _connect(path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO violation_appeals (
                user_id, username, latest_violation_id,
                violation_count_snapshot, penalty_points, pre_status, pre_role,
                reason, status, created_at
            ) VALUES (3, 'alice', 41, ?, ?, 'active', 'user',
                      'please review', 'pending', ?)
            """,
            (snapshot, penalty, datetime.now().isoformat()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _post_at_once(app, *, count, path, actor, payload):
    ready = threading.Barrier(count + 1)

    def invoke(_index):
        ready.wait(timeout=10)
        with app.test_client() as client:
            response = client.post(path, json=payload, headers={"X-Test-Actor": actor})
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(invoke, index) for index in range(count)]
        ready.wait(timeout=10)
        return [future.result(timeout=30) for future in futures]


def test_parallel_duplicate_submission_has_one_durable_appeal(tmp_path):
    db_path = str(tmp_path / "appeals-submit.db")
    _seed_db(db_path)
    audits = []
    stale_read_barrier = threading.Barrier(8)
    app = _build_app(
        db_path,
        audits,
        stale_read_barrier=stale_read_barrier,
        barrier_query="select 1 from violation_appeals where user_id=? and latest_violation_id=?",
    )

    results = _post_at_once(
        app,
        count=8,
        path="/api/appeals",
        actor="alice",
        payload={"violation_id": 41, "reason": "same violation submitted in parallel"},
    )

    assert [status for status, _payload in results].count(200) == 1
    assert [status for status, _payload in results].count(409) == 7
    assert all(payload["ok"] is (status == 200) for status, payload in results)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT user_id, username, latest_violation_id, status FROM violation_appeals"
        ).fetchall()
        assert [dict(row) for row in rows] == [
            {
                "user_id": 3,
                "username": "alice",
                "latest_violation_id": 41,
                "status": "pending",
            }
        ]
    finally:
        conn.close()
    submitted = [entry for entry in audits if entry[0][0] == "VIOLATION_APPEAL_SUBMITTED"]
    assert len(submitted) == 1


def test_approving_old_appeal_subtracts_penalty_from_current_count(tmp_path):
    db_path = str(tmp_path / "appeals-current-count.db")
    _seed_db(db_path)
    appeal_id = _insert_pending_appeal(db_path, snapshot=3, penalty=2)
    audits = []
    app = _build_app(db_path, audits)

    response = app.test_client().post(
        f"/api/admin/appeals/{appeal_id}/review",
        json={"action": "approve", "note": "evidence accepted"},
        headers={"X-Test-Actor": "root"},
    )

    assert response.status_code == 200, response.get_json()
    conn = _connect(db_path)
    try:
        user = conn.execute(
            "SELECT status, role, violation_count FROM users WHERE id=3"
        ).fetchone()
        appeal = conn.execute(
            "SELECT status, reviewed_by FROM violation_appeals WHERE id=?",
            (appeal_id,),
        ).fetchone()
        # The appeal snapshot was 3, but two later violations raised the live
        # count to 9.  Only this appeal's 2-point penalty may be removed.
        assert dict(user) == {"status": "active", "role": "user", "violation_count": 7}
        assert dict(appeal) == {"status": "approved", "reviewed_by": "root"}
    finally:
        conn.close()


def test_parallel_approve_reject_claims_review_exactly_once(tmp_path):
    db_path = str(tmp_path / "appeals-review.db")
    _seed_db(db_path)
    appeal_id = _insert_pending_appeal(db_path, snapshot=9, penalty=2)
    audits = []
    stale_read_barrier = threading.Barrier(2)
    app = _build_app(
        db_path,
        audits,
        stale_read_barrier=stale_read_barrier,
        barrier_query="select * from violation_appeals where id=?",
    )
    ready = threading.Barrier(3)

    def review(action):
        ready.wait(timeout=10)
        with app.test_client() as client:
            response = client.post(
                f"/api/admin/appeals/{appeal_id}/review",
                json={"action": action, "note": f"parallel {action}"},
                headers={"X-Test-Actor": "root"},
            )
            return action, response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(review, action) for action in ("approve", "reject")]
        ready.wait(timeout=10)
        results = [future.result(timeout=30) for future in futures]

    assert sorted(status for _action, status, _payload in results) == [200, 409]
    winning_action = next(action for action, status, _payload in results if status == 200)
    conn = _connect(db_path)
    try:
        appeal = conn.execute(
            "SELECT status, reviewed_by, review_note FROM violation_appeals WHERE id=?",
            (appeal_id,),
        ).fetchone()
        user = conn.execute("SELECT violation_count FROM users WHERE id=3").fetchone()
        assert appeal["status"] == ("approved" if winning_action == "approve" else "rejected")
        assert appeal["reviewed_by"] == "root"
        assert appeal["review_note"] == f"parallel {winning_action}"
        assert user["violation_count"] == (7 if winning_action == "approve" else 9)
    finally:
        conn.close()
    reviewed = [entry for entry in audits if entry[0][0] == "VIOLATION_APPEAL_REVIEWED"]
    assert len(reviewed) == 1
    assert f"action={winning_action}" in reviewed[0][1]["detail"]


def test_user_and_admin_appeal_views_project_stable_identity(tmp_path):
    db_path = str(tmp_path / "appeals-identity.db")
    _seed_db(db_path)
    appeal_id = _insert_pending_appeal(db_path, snapshot=9, penalty=2)
    app = _build_app(db_path, [])

    user_response = app.test_client().get(
        "/api/appeals", headers={"X-Test-Actor": "alice"}
    )
    other_response = app.test_client().get(
        "/api/appeals", headers={"X-Test-Actor": "bob"}
    )
    admin_response = app.test_client().get(
        "/api/admin/appeals?status=pending&page=1&limit=20",
        headers={"X-Test-Actor": "root"},
    )

    assert user_response.status_code == 200, user_response.get_json()
    assert other_response.status_code == 200, other_response.get_json()
    assert admin_response.status_code == 200, admin_response.get_json()
    user_payload = user_response.get_json()
    admin_payload = admin_response.get_json()
    assert user_payload["appeals"][0]["id"] == appeal_id
    assert {
        "user_id": user_payload["appeals"][0]["user_id"],
        "username": user_payload["appeals"][0]["username"],
    } == {"user_id": 3, "username": "alice"}
    nested = user_payload["violations"][0]["appeal"]
    assert {"id": nested["id"], "user_id": nested["user_id"], "username": nested["username"]} == {
        "id": appeal_id,
        "user_id": 3,
        "username": "alice",
    }
    assert other_response.get_json()["appeals"] == []
    assert admin_payload["total"] == 1
    assert {
        "id": admin_payload["items"][0]["id"],
        "user_id": admin_payload["items"][0]["user_id"],
        "username": admin_payload["items"][0]["username"],
    } == {"id": appeal_id, "user_id": 3, "username": "alice"}
