import sqlite3

from flask import Flask, jsonify

from routes.users import register_user_routes


def _json_resp(payload, status=None):
    response = jsonify(payload)
    return (response, status) if status else response


def _passthrough(fn):
    return fn


def _role_rank(role):
    return {"user": 1, "manager": 2, "super_admin": 3}.get(role or "user", 1)


def _build_app(db_path, actor_box):
    app = Flask(__name__)
    app.testing = True

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    register_user_routes(
        app,
        {
            "ACCOUNT_STATUSES": {"active", "inactive", "pending", "rejected"},
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
            "parse_positive_int": lambda value, default=1, min_value=1, max_value=None: (
                int(value or default)
                if int(value or default) >= min_value and (max_value is None or int(value or default) <= max_value)
                else None
            ),
            "revoke_user_sessions": lambda *args, **kwargs: None,
            "require_csrf": _passthrough,
            "require_csrf_safe": _passthrough,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": False,
            "enforce_password_strength": lambda value, **kwargs: (True, "", {}),
            "role_rank": _role_rank,
            "score_password_strength": lambda value: {"score": 100},
            "user_public_payload": lambda row: dict(row),
            "validate_id_number": lambda value: True,
            "validate_password": lambda value: True,
            "validate_phone": lambda value: True,
            "verify_password": lambda *args, **kwargs: False,
        },
    )
    return app


def _seed_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
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
            trust_score INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            reputation INTEGER NOT NULL DEFAULT 0,
            violation_score INTEGER NOT NULL DEFAULT 0,
            sanction_status TEXT NOT NULL DEFAULT 'none',
            sanction_until TEXT,
            level_updated_at TEXT,
            level_updated_by TEXT,
            level_update_reason TEXT,
            password_strength_score INTEGER NOT NULL DEFAULT 0,
            avatar_file_id TEXT,
            avatar_crop_json TEXT,
            blocked_until TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00'
        );
        INSERT INTO users (id, username, role) VALUES
          (1, 'root', 'super_admin'),
          (2, 'admin', 'manager'),
          (3, 'alice', 'user'),
          (4, 'manager2', 'manager');
        """
    )
    conn.commit()
    conn.close()


def test_manager_member_governance_disposition_can_restrict_features_and_create_fine(tmp_path):
    db_path = tmp_path / "member-governance.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 2, "username": "admin", "role": "manager"}}
    client = _build_app(db_path, actor_box).test_client()

    res = client.put(
        "/api/admin/users/3",
        json={
            "restriction_features": ["cloud_upload", "trading_order"],
            "fine_amount_points": 250,
            "fine_due_hours": 48,
            "governance_disposition_reason": "重複濫用交易與上傳",
        },
    )

    assert res.status_code == 200, res.get_json()
    payload = res.get_json()
    action_types = [item["type"] for item in payload["governance_actions"]]
    assert action_types.count("feature_restriction") == 2
    assert "violation_fine" in action_types

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        restrictions = conn.execute(
            "SELECT feature_key, source_type, reason FROM user_feature_restrictions WHERE user_id=3 ORDER BY feature_key"
        ).fetchall()
        fine = conn.execute("SELECT amount_points, policy_key, reason FROM violation_fines WHERE user_id=3").fetchone()
    finally:
        conn.close()

    assert [row["feature_key"] for row in restrictions] == ["cloud_upload", "trading_order"]
    assert all(row["source_type"] == "member_governance" for row in restrictions)
    assert fine["amount_points"] == 250
    assert fine["policy_key"] == "member_governance"
    assert "重複濫用" in fine["reason"]


def test_manager_member_governance_disposition_cannot_target_manager(tmp_path):
    db_path = tmp_path / "member-governance-deny.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 2, "username": "admin", "role": "manager"}}
    client = _build_app(db_path, actor_box).test_client()

    res = client.put(
        "/api/admin/users/4",
        json={"restriction_features": ["wallet_transfer"], "fine_amount_points": 100},
    )

    assert res.status_code == 403
    assert "一般用戶" in res.get_json()["msg"]


def test_manager_reputation_reward_is_auditable_and_role_scoped(tmp_path):
    db_path = tmp_path / "member-reputation-reward.db"
    _seed_db(db_path)
    actor_box = {"actor": {"id": 2, "username": "admin", "role": "manager"}}
    client = _build_app(db_path, actor_box).test_client()

    rewarded = client.post(
        "/api/admin/users/3/reputation-reward",
        json={"points": 9, "reason": "high quality security report"},
    )
    denied = client.post(
        "/api/admin/users/4/reputation-reward",
        json={"points": 2, "reason": "peer reward must be root-reviewed"},
    )

    assert rewarded.status_code == 200, rewarded.get_json()
    assert rewarded.get_json()["reward_type"] == "reputation"
    assert rewarded.get_json()["reputation"] == 9
    assert denied.status_code == 403

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        event = conn.execute(
            "SELECT delta, reason, source_user_id FROM reputation_events WHERE user_id=3"
        ).fetchone()
        action = conn.execute(
            "SELECT action_type, target_type, target_id, reason FROM moderation_actions WHERE target_id=3"
        ).fetchone()
    finally:
        conn.close()

    assert dict(event) == {
        "delta": 9,
        "reason": "admin_member_reward:high quality security report",
        "source_user_id": 2,
    }
    assert dict(action) == {
        "action_type": "reward_member_reputation",
        "target_type": "user",
        "target_id": 3,
        "reason": "high quality security report",
    }
