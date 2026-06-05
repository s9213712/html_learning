import sqlite3

from flask import Flask, jsonify

from routes.users import register_user_routes
from services.users.friends import ensure_social_schema


def _json_resp(payload, status=None):
    response = jsonify(payload)
    return (response, status) if status else response


def _passthrough(fn):
    return fn


def _role_rank(role):
    return {"user": 1, "manager": 2, "super_admin": 3}.get(role or "user", 1)


def _build_app(db_path, actor_box, *, connection_factory=None):
    app = Flask(__name__)
    app.testing = True

    def get_db():
        kwargs = {"factory": connection_factory} if connection_factory is not None else {}
        conn = sqlite3.connect(db_path, **kwargs)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    register_user_routes(app, {
        "ACCOUNT_STATUSES": {"active", "inactive", "suspended", "restricted"},
        "MAX_MANAGERS": 5,
        "MAX_EXTRA_SUPER_ADMINS": 1,
        "MEMBER_LEVELS": {"normal": {"label": "一般"}},
        "PASSWORD_HISTORY_LIMIT": 5,
        "ROLE_LABEL": {"user": "使用者", "manager": "管理員", "super_admin": "Root"},
        "ROLE_RANK": {"user": 1, "manager": 2, "super_admin": 3},
        "add_violation": lambda *args, **kwargs: ("noop", "noop", 0),
        "audit": lambda *args, **kwargs: None,
        "check_user_rate_limit": lambda *args, **kwargs: (False, {}),
        "count_role": lambda *args, **kwargs: 0,
        "decrypt_field": lambda value: value,
        "encrypt_field": lambda value: value,
        "ensure_user_official_room_membership": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_auth_db": get_db,
        "get_db": get_db,
        "get_ua": lambda: "pytest",
        "hash_password": lambda value: f"hash:{value}",
        "hash_token": lambda value: f"token:{value}",
        "is_feature_enabled": lambda key: True,
        "json_resp": _json_resp,
        "normalize_text": lambda value: value.strip() if isinstance(value, str) else "",
        "parse_birthdate": lambda value: value,
        "parse_positive_int": lambda value, default=1: int(value or default),
        "revoke_user_sessions": lambda *args, **kwargs: None,
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": False,
        "enforce_password_strength": lambda value, **kwargs: (True, ""),
        "role_rank": _role_rank,
        "score_password_strength": lambda value: {"score": 100},
        "user_public_payload": lambda row: dict(row),
        "validate_id_number": lambda value: True,
        "validate_password": lambda value: True,
        "validate_phone": lambda value: True,
        "verify_password": lambda *args, **kwargs: False,
    })
    return app


def _seed_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            member_level TEXT NOT NULL DEFAULT 'normal',
            effective_level TEXT NOT NULL DEFAULT 'normal',
            avatar_file_id TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00'
        )
        """
    )
    conn.executemany(
        "INSERT INTO users (id, username, role, status) VALUES (?, ?, ?, 'active')",
        [
            (1, "root", "super_admin"),
            (2, "manager", "manager"),
            (3, "alice", "user"),
            (4, "bob", "user"),
        ],
    )
    conn.commit()
    conn.close()


def _seed_admin_creation_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            nickname TEXT,
            real_name TEXT,
            birthdate TEXT,
            id_number TEXT,
            phone TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            member_level TEXT NOT NULL DEFAULT 'normal',
            base_level TEXT NOT NULL DEFAULT 'normal',
            effective_level TEXT NOT NULL DEFAULT 'normal',
            avatar_file_id TEXT,
            password_strength_score INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO users (username, role, status) VALUES ('root', 'super_admin', 'active')")
    conn.commit()
    conn.close()


def _accept_friendship(path, user_id, friend_user_id, requested_by=None):
    a, b = sorted([int(user_id), int(friend_user_id)])
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_social_schema(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO user_friends (
                user_id, friend_user_id, status, requested_by, created_at, updated_at
            ) VALUES (?, ?, 'accepted', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
            """,
            (a, b, int(requested_by or user_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _assert_public_friend_target(target):
    assert {
        "id",
        "username",
        "role",
        "role_label",
        "is_official",
        "member_level",
        "avatar_file_id",
        "display_name",
    } >= set(target.keys())
    for private_key in (
        "status",
        "created_at",
        "updated_at",
        "nickname",
        "real_name",
        "id_number",
        "phone",
        "password_strength_score",
        "failed_login_count",
        "locked_until",
    ):
        assert private_key not in target


def test_profile_api_returns_random_friend_code_only_to_owner(tmp_path):
    db_path = tmp_path / "profile.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 3, "username": "alice", "role": "user"}}
    client = _build_app(str(db_path), actor_box).test_client()

    own = client.get("/api/users/me/profile")
    code = own.get_json()["profile"]["friend_code"]
    updated = client.put("/api/users/me/profile", json={
        "display_name": "Alice A.",
        "bio": "hello",
        "display_timezone": "Asia/Taipei",
        "profile_template": "creator",
        "profile_accent": "ocean",
        "profile_density": "compact",
        "profile_style": {"avatar_size": "500", "avatar_frame": "neon", "avatar_shape": "squircle"},
        "profile_public_info": [
            {"label": "作品集", "value": "https://example.test/alice"},
            {"label": "社群", "value": "@alice"},
        ],
    })

    actor_box["actor"] = {"id": 4, "username": "bob", "role": "user"}
    public = client.get("/api/users/3/profile")

    assert own.status_code == 200
    assert code.startswith("F")
    assert len(code) >= 8
    assert updated.status_code == 200
    assert updated.get_json()["profile"]["display_name"] == "Alice A."
    assert updated.get_json()["profile"]["display_timezone"] == "Asia/Taipei"
    assert updated.get_json()["profile"]["profile_template"] == "creator"
    assert updated.get_json()["profile"]["profile_accent"] == "ocean"
    assert updated.get_json()["profile"]["profile_density"] == "compact"
    assert updated.get_json()["profile"]["profile_style"]["avatar_size"] == "500"
    assert updated.get_json()["profile"]["profile_style"]["avatar_frame"] == "neon"
    assert updated.get_json()["profile"]["profile_style"]["avatar_shape"] == "squircle"
    assert updated.get_json()["profile"]["profile_public_info"] == [
        {"label": "作品集", "value": "https://example.test/alice"},
        {"label": "社群", "value": "@alice"},
    ]
    assert public.get_json()["profile"]["profile_template"] == "creator"
    assert public.get_json()["profile"]["profile_style"]["avatar_size"] == "500"
    assert public.get_json()["profile"]["profile_style"]["avatar_shape"] == "squircle"
    assert public.get_json()["profile"]["profile_public_info"][0]["label"] == "作品集"
    assert "friend_code" not in public.get_json()["profile"]
    assert "display_timezone" not in public.get_json()["profile"]


def test_profile_timezone_rejects_invalid_iana_name(tmp_path):
    db_path = tmp_path / "profile-timezone.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 3, "username": "alice", "role": "user"}}
    client = _build_app(str(db_path), actor_box).test_client()

    bad = client.put("/api/users/me/profile", json={"display_timezone": "Mars/Base"})
    good = client.put("/api/users/me/profile", json={"display_timezone": "auto"})

    assert bad.status_code == 400
    assert "顯示時區" in bad.get_json()["msg"]
    assert good.status_code == 200
    assert good.get_json()["profile"]["display_timezone"] == "auto"


def test_profile_and_target_options_accept_sqlite_row_actor(tmp_path):
    db_path = tmp_path / "profile-row-actor.db"
    _seed_db(db_path)
    _accept_friendship(db_path, 3, 4)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        actor = conn.execute("SELECT * FROM users WHERE username='alice'").fetchone()
    finally:
        conn.close()
    actor_box = {"actor": actor}
    client = _build_app(str(db_path), actor_box).test_client()

    own = client.get("/api/users/me/profile")
    targets = client.get("/api/users/target-options?context=pm")

    assert own.status_code == 200
    assert own.get_json()["profile"]["friend_code"].startswith("F")
    assert targets.status_code == 200
    assert [row["username"] for row in targets.get_json()["users"]] == ["bob"]


def test_friend_request_accept_and_friend_code_direct_add(tmp_path):
    db_path = tmp_path / "friends.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 3, "username": "alice", "role": "user"}}
    client = _build_app(str(db_path), actor_box).test_client()
    alice_code = client.get("/api/users/me/profile").get_json()["profile"]["friend_code"]

    requested = client.post("/api/friends/request", json={"username": "bob"})
    actor_box["actor"] = {"id": 4, "username": "bob", "role": "user"}
    pending = client.get("/api/friends/requests")
    request_id = pending.get_json()["incoming"][0]["id"]
    accepted = client.post(f"/api/friends/requests/{request_id}/accept")
    friends = client.get("/api/friends")
    removed = client.delete("/api/friends/3")
    added_by_code = client.post("/api/friends/add-by-code", json={"friend_code": alice_code})

    assert requested.status_code == 200
    _assert_public_friend_target(requested.get_json()["request"]["target"])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        request_note = conn.execute(
            "SELECT user_id, type, title, body, is_read FROM notifications WHERE user_id=4 AND type='friend_request'"
        ).fetchone()
    finally:
        conn.close()
    assert request_note is not None
    assert request_note["is_read"] == 0
    assert "alice" in request_note["body"]
    assert accepted.status_code == 200
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        accepted_note = conn.execute(
            "SELECT user_id, type, title, body, is_read FROM notifications WHERE user_id=3 AND type='friend_request_accepted'"
        ).fetchone()
    finally:
        conn.close()
    assert accepted_note is not None
    assert accepted_note["is_read"] == 0
    assert "bob" in accepted_note["body"]
    assert friends.status_code == 200
    assert friends.get_json()["friends"][0]["other_username"] == "alice"
    assert removed.status_code == 200
    assert added_by_code.status_code == 200
    assert added_by_code.get_json()["request"]["status"] == "accepted"
    _assert_public_friend_target(added_by_code.get_json()["request"]["target"])


def test_profile_follow_counts_and_follow_unfollow_api(tmp_path):
    db_path = tmp_path / "profile-follow.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 3, "username": "alice", "role": "user"}}
    client = _build_app(str(db_path), actor_box).test_client()

    follow = client.post("/api/users/4/follow")
    bob_profile = client.get("/api/users/4/profile")
    actor_box["actor"] = {"id": 4, "username": "bob", "role": "user"}
    own_profile = client.get("/api/users/me/profile")
    unfollow_actor = {"id": 3, "username": "alice", "role": "user"}
    actor_box["actor"] = unfollow_actor
    unfollow = client.delete("/api/users/4/follow")
    bob_after = client.get("/api/users/4/profile")

    assert follow.status_code == 200
    _assert_public_friend_target(follow.get_json()["follow"]["target"])
    assert bob_profile.get_json()["profile"]["follow_status"] == "following"
    assert bob_profile.get_json()["profile"]["follower_count"] == 1
    assert own_profile.get_json()["profile"]["follower_count"] == 1
    assert unfollow.status_code == 200
    assert bob_after.get_json()["profile"]["follow_status"] == "not_following"
    assert bob_after.get_json()["profile"]["follower_count"] == 0


def test_block_user_response_uses_public_target_payload(tmp_path):
    db_path = tmp_path / "profile-block.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 3, "username": "alice", "role": "user"}}
    client = _build_app(str(db_path), actor_box).test_client()

    blocked = client.post("/api/friends/4/block")

    assert blocked.status_code == 200
    payload = blocked.get_json()
    assert payload["block"]["status"] == "blocked"
    _assert_public_friend_target(payload["block"]["target"])


def test_target_options_are_friends_only_for_personal_context_and_all_users_for_official_context(tmp_path):
    db_path = tmp_path / "target-options.db"
    _seed_db(db_path)
    _accept_friendship(db_path, 3, 4)
    _accept_friendship(db_path, 1, 4)
    actor_box = {"actor": {"id": 3, "username": "alice", "role": "user"}}
    client = _build_app(str(db_path), actor_box).test_client()

    personal = client.get("/api/users/target-options?context=pm")
    actor_box["actor"] = {"id": 1, "username": "root", "role": "super_admin"}
    root_pm = client.get("/api/users/target-options?context=pm")
    official = client.get("/api/users/target-options?context=admin_notice")

    assert personal.status_code == 200
    assert [row["username"] for row in personal.get_json()["users"]] == ["bob"]
    assert personal.get_json()["users"][0]["is_friend"] is True

    assert root_pm.status_code == 200
    root_pm_users = root_pm.get_json()["users"]
    assert {row["username"] for row in root_pm_users} >= {"alice", "bob", "manager"}
    assert root_pm_users[0]["username"] == "bob"
    assert root_pm_users[0]["is_friend"] is True

    assert official.status_code == 200
    official_users = official.get_json()["users"]
    assert {row["username"] for row in official_users} >= {"alice", "bob", "manager"}
    assert official_users[0]["username"] in {"alice", "bob"}
    assert official_users[0]["is_friend"] is True


def test_admin_create_user_duplicate_race_returns_conflict(tmp_path):
    db_path = tmp_path / "admin-create-race.db"
    _seed_admin_creation_db(db_path)
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}

    class RaceConnection(sqlite3.Connection):
        injected = False

        def execute(self, sql, parameters=(), /):
            if (
                not RaceConnection.injected
                and str(sql).lstrip().upper().startswith("INSERT INTO USERS")
                and parameters
                and parameters[0] == "raceuser"
            ):
                RaceConnection.injected = True
                other = sqlite3.connect(db_path)
                try:
                    other.execute(
                        "INSERT INTO users (username, role, status, member_level, base_level, effective_level) "
                        "VALUES ('raceuser', 'user', 'active', 'normal', 'normal', 'normal')"
                    )
                    other.commit()
                finally:
                    other.close()
            return super().execute(sql, parameters)

    client = _build_app(str(db_path), actor_box, connection_factory=RaceConnection).test_client()
    response = client.post(
        "/api/admin/users",
        json={
            "username": "raceuser",
            "password": "pw",
            "password_confirm": "pw",
            "nickname": "Race",
            "role": "user",
            "status": "active",
            "member_level": "normal",
        },
    )

    assert response.status_code == 409
    assert response.get_json()["msg"] == "帳號已存在"
