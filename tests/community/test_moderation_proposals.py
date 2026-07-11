import sqlite3

from flask import Flask, jsonify

from routes.moderation import register_moderation_routes
from services.governance.records import add_reputation_event, ensure_governance_records_schema


def _role_rank(role):
    return {"user": 0, "manager": 3, "super_admin": 4}.get(role or "user", 0)


def _build_app(db_path, actor_box, revoked, *, audit_enabled=False, audit_result=(True, None, "ok"), activation_log=None, violation_log=None):
    app = Flask(__name__)
    app.testing = True

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def passthrough(fn):
        return fn

    register_moderation_routes(app, {
        "AUDIT_LOG_PATH": "missing.log",
        "activate_emergency_lockdown": lambda reason: (activation_log.append(reason) if activation_log is not None else None),
        "add_violation": lambda *args, **kwargs: None,
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": lambda: actor_box["actor"],
        "get_db": get_db,
        "is_audit_chain_enabled": lambda: audit_enabled,
        "is_feature_enabled": lambda key: key == "feature_member_governance_enabled",
        "json_resp": lambda payload: jsonify(payload),
        "normalize_text": lambda value: value.strip() if isinstance(value, str) else "",
        "parse_positive_int": lambda value, default=None, min_value=None, max_value=None: int(value or default or 0),
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
        "revoke_user_sessions": lambda user_id: revoked.append(user_id),
        "role_rank": _role_rank,
        "secure_add_violation": lambda *args, **kwargs: (violation_log.append(args) if violation_log is not None else None),
        "verify_audit_integrity": lambda: audit_result,
        "verify_violation_integrity": lambda user_id: (True, None, "ok"),
    })
    return app


def _seed_users(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            member_level TEXT NOT NULL DEFAULT 'normal',
            reputation INTEGER NOT NULL DEFAULT 0,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            deleted_at TEXT,
            updated_at TEXT,
            violation_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            action TEXT,
            ip TEXT,
            user TEXT,
            success INTEGER,
            ua TEXT,
            detail TEXT,
            chain_hash TEXT
        );
        INSERT INTO users (id, username, role, status, member_level) VALUES
            (1, 'root', 'super_admin', 'active', 'normal'),
            (2, 'admin1', 'manager', 'active', 'normal'),
            (3, 'admin2', 'manager', 'active', 'normal'),
            (4, 'alice', 'user', 'active', 'normal'),
            (5, 'admin3', 'manager', 'active', 'normal'),
            (6, 'supervisor', 'super_admin', 'active', 'normal');
        """
    )
    conn.commit()
    conn.close()


def test_moderation_proposal_vote_and_execute(tmp_path):
    db_path = tmp_path / "moderation.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    create = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 4, "action_type": "warn", "reason": "輕微違規", "required_votes": 10},
    )
    assert create.status_code == 200
    proposal = create.get_json()["proposal"]
    assert proposal["risk_level"] == "normal"
    assert proposal["required_votes"] == 1
    proposal_id = proposal["id"]

    proposer_vote = client.post(f"/api/admin/moderation/proposals/{proposal_id}/vote", json={"vote": "approve"})
    assert proposer_vote.status_code == 403
    assert proposer_vote.get_json()["msg"] == "提案者不可投票"

    actor_box["actor"] = {"id": 3, "username": "admin2", "role": "manager"}
    first_vote = client.post(f"/api/admin/moderation/proposals/{proposal_id}/vote", json={"vote": "approve"})
    assert first_vote.status_code == 200
    assert first_vote.get_json()["proposal"]["status"] == "approved"

    duplicate_vote = client.post(f"/api/admin/moderation/proposals/{proposal_id}/vote", json={"vote": "approve"})
    assert duplicate_vote.status_code == 409

    execute = client.post(f"/api/admin/moderation/proposals/{proposal_id}/execute")
    assert execute.status_code == 200

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status, member_level FROM users WHERE id=4").fetchone()
    proposal_status = conn.execute("SELECT status FROM moderation_proposals WHERE id=?", (proposal_id,)).fetchone()[0]
    action = conn.execute("SELECT action_type, target_type, target_id FROM moderation_actions LIMIT 1").fetchone()
    conn.close()

    assert row == ("active", "normal")
    assert proposal_status == "executed"
    assert action == ("warn", "user", 4)
    assert revoked == []


def test_governance_cannot_target_self_or_be_voted_by_target(tmp_path):
    db_path = tmp_path / "moderation.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    self_create = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 2, "action_type": "warn", "reason": "self governance"},
    )
    assert self_create.status_code == 403
    assert self_create.get_json()["msg"] == "不可對自己建立治理提案"

    create = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 4, "action_type": "warn", "reason": "target should not vote"},
    )
    assert create.status_code == 200
    proposal_id = create.get_json()["proposal"]["id"]

    actor_box["actor"] = {"id": 4, "username": "alice", "role": "manager"}
    target_vote = client.post(f"/api/admin/moderation/proposals/{proposal_id}/vote", json={"vote": "approve"})
    assert target_vote.status_code == 403
    assert target_vote.get_json()["msg"] == "治理對象不可投票"


def test_manager_cannot_create_governance_proposal_against_super_admin(tmp_path):
    db_path = tmp_path / "moderation.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    create = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 6, "action_type": "warn", "reason": "privilege probe"},
    )

    assert create.status_code == 403
    assert "同級或更高權限" in create.get_json()["msg"]


def test_high_risk_governance_requires_root_and_two_managers(tmp_path):
    db_path = tmp_path / "moderation.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    create = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 4, "action_type": "suspend", "reason": "嚴重違規", "required_votes": 1},
    )
    proposal = create.get_json()["proposal"]
    assert proposal["risk_level"] == "high"
    assert proposal["required_root_approval"] is True
    assert proposal["required_manager_approvals"] == 2
    assert proposal["required_votes"] == 3
    proposal_id = proposal["id"]

    proposer_vote = client.post(f"/api/admin/moderation/proposals/{proposal_id}/vote", json={"vote": "approve"})
    assert proposer_vote.status_code == 403
    assert proposer_vote.get_json()["msg"] == "提案者不可投票"

    actor_box["actor"] = {"id": 3, "username": "admin2", "role": "manager"}
    first_vote = client.post(f"/api/admin/moderation/proposals/{proposal_id}/vote", json={"vote": "approve"})
    assert first_vote.status_code == 200
    assert first_vote.get_json()["proposal"]["status"] == "pending"

    actor_box["actor"] = {"id": 5, "username": "admin3", "role": "manager"}
    second_vote = client.post(f"/api/admin/moderation/proposals/{proposal_id}/vote", json={"vote": "approve"})
    assert second_vote.status_code == 200
    assert second_vote.get_json()["proposal"]["status"] == "pending"

    execute_too_early = client.post(f"/api/admin/moderation/proposals/{proposal_id}/execute")
    assert execute_too_early.status_code == 409

    actor_box["actor"] = {"id": 1, "username": "root", "role": "super_admin"}
    root_vote = client.post(f"/api/admin/moderation/proposals/{proposal_id}/vote", json={"vote": "approve"})
    assert root_vote.status_code == 200
    root_payload = root_vote.get_json()["proposal"]
    assert root_payload["status"] == "approved"
    assert root_payload["root_requirement_met"] is True
    assert root_payload["manager_requirement_met"] is True

    actor_box["actor"] = {"id": 3, "username": "admin2", "role": "manager"}
    manager_execute = client.post(f"/api/admin/moderation/proposals/{proposal_id}/execute")
    assert manager_execute.status_code == 403

    actor_box["actor"] = {"id": 1, "username": "root", "role": "super_admin"}
    execute = client.post(f"/api/admin/moderation/proposals/{proposal_id}/execute")
    assert execute.status_code == 200

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status, member_level FROM users WHERE id=4").fetchone()
    conn.close()
    assert row == ("active", "suspended")
    assert revoked == [4]


def test_restrict_governance_selects_features_and_executes_restrictions(tmp_path):
    db_path = tmp_path / "moderation-restrict.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    create = client.post(
        "/api/admin/moderation/proposals",
        json={
            "target_user_id": 4,
            "action_type": "restrict",
            "restriction_features": ["cloud_upload", "trading_order"],
            "duration_hours": 12,
            "reason": "大量上傳與交易濫用",
        },
    )
    assert create.status_code == 200, create.get_json()
    proposal = create.get_json()["proposal"]
    assert proposal["action_payload"]["restriction_features"] == ["cloud_upload", "trading_order"]

    actor_box["actor"] = {"id": 3, "username": "admin2", "role": "manager"}
    vote = client.post(f"/api/admin/moderation/proposals/{proposal['id']}/vote", json={"vote": "approve"})
    assert vote.status_code == 200
    execute = client.post(f"/api/admin/moderation/proposals/{proposal['id']}/execute")
    assert execute.status_code == 200

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT feature_key, source_type, source_ref, expires_at
        FROM user_feature_restrictions
        WHERE user_id=4
        ORDER BY feature_key
        """
    ).fetchall()
    conn.close()
    assert [row[0] for row in rows] == ["cloud_upload", "trading_order"]
    assert all(row[1] == "member_governance" for row in rows)
    assert all(row[2] == f"moderation_proposal:{proposal['id']}" for row in rows)
    assert all(row[3] for row in rows)
    assert revoked == [4]


def test_mute_governance_requires_duration_and_limits_speech_features(tmp_path):
    db_path = tmp_path / "moderation-mute.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    missing_duration = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 4, "action_type": "mute", "reason": "洗頻"},
    )
    assert missing_duration.status_code == 400
    assert "期限" in missing_duration.get_json()["msg"]

    create = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 4, "action_type": "mute", "duration_hours": 6, "reason": "洗頻"},
    )
    assert create.status_code == 200, create.get_json()
    proposal = create.get_json()["proposal"]
    assert proposal["action_payload"]["duration_hours"] == 6

    actor_box["actor"] = {"id": 3, "username": "admin2", "role": "manager"}
    vote = client.post(f"/api/admin/moderation/proposals/{proposal['id']}/vote", json={"vote": "approve"})
    assert vote.status_code == 200
    execute = client.post(f"/api/admin/moderation/proposals/{proposal['id']}/execute")
    assert execute.status_code == 200

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT feature_key, expires_at FROM user_feature_restrictions WHERE user_id=4 ORDER BY feature_key"
    ).fetchall()
    user_status = conn.execute("SELECT status FROM users WHERE id=4").fetchone()[0]
    conn.close()
    assert [row[0] for row in rows] == ["chat_dm", "chat_send", "community_comment", "community_post"]
    assert all(row[1] for row in rows)
    assert user_status == "active"
    assert revoked == [4]


def test_governance_preserves_bounded_evidence_and_rejects_bad_ttl(tmp_path):
    db_path = tmp_path / "moderation-evidence.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    malformed = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 4, "action_type": "warn", "reason": "probe", "ttl_hours": "forever"},
    )
    oversized = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 4, "action_type": "warn", "reason": "probe", "ttl_hours": 999999},
    )
    created = client.post(
        "/api/admin/moderation/proposals",
        json={
            "target_user_id": 4,
            "action_type": "warn",
            "reason": "documented abuse",
            "evidence": {"report_ids": [11, 12], "summary": "duplicate spam"},
        },
    )

    assert malformed.status_code == 400
    assert oversized.status_code == 400
    assert created.status_code == 200, created.get_json()
    assert created.get_json()["proposal"]["action_payload"]["evidence"] == {
        "report_ids": [11, 12],
        "summary": "duplicate spam",
    }


def test_emergency_governance_applies_then_reverts_if_not_approved(tmp_path):
    db_path = tmp_path / "moderation-emergency.db"
    _seed_users(db_path)
    revoked = []
    violation_log = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked, violation_log=violation_log).test_client()

    create = client.post(
        "/api/admin/moderation/proposals",
        json={
            "target_user_id": 4,
            "action_type": "restrict",
            "restriction_features": ["wallet_transfer"],
            "duration_hours": 24,
            "emergency_execute": True,
            "reason": "疑似盜號出金",
            "ttl_hours": 72,
        },
    )
    assert create.status_code == 200, create.get_json()
    proposal = create.get_json()["proposal"]
    assert proposal["is_emergency"] is True
    assert proposal["status"] == "pending"
    assert proposal["emergency_applied_at"]
    assert proposal["expires_at"]

    conn = sqlite3.connect(db_path)
    active = conn.execute(
        "SELECT status, source_type FROM user_feature_restrictions WHERE user_id=4 AND feature_key='wallet_transfer'"
    ).fetchone()
    conn.execute("UPDATE moderation_proposals SET expires_at='2000-01-01T00:00:00' WHERE id=?", (proposal["id"],))
    conn.commit()
    conn.close()
    assert active == ("active", "member_governance_emergency")
    assert revoked == [4]

    list_res = client.get("/api/admin/moderation/proposals?status=expired")
    assert list_res.status_code == 200

    conn = sqlite3.connect(db_path)
    released = conn.execute(
        "SELECT status, released_at FROM user_feature_restrictions WHERE user_id=4 AND feature_key='wallet_transfer'"
    ).fetchone()
    final = conn.execute(
        "SELECT status, emergency_reverted_at, emergency_revert_reason FROM moderation_proposals WHERE id=?",
        (proposal["id"],),
    ).fetchone()
    conn.close()
    assert released[0] == "released"
    assert released[1]
    assert final[0] == "expired"
    assert final[1]
    assert final[2] == "emergency_not_approved_in_time"
    assert violation_log and violation_log[0][0] == 2


def test_admin_audit_reports_broken_chain_without_auto_lockdown(tmp_path):
    db_path = tmp_path / "moderation.db"
    _seed_users(db_path)
    revoked = []
    activation_log = []
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    client = _build_app(
        str(db_path),
        actor_box,
        revoked,
        audit_enabled=True,
        audit_result=(False, 11, "hash mismatch"),
        activation_log=activation_log,
    ).test_client()

    res = client.get("/api/admin/audit")
    data = res.get_json()

    assert res.status_code == 200
    assert data["integrity"]["ok"] is False
    assert data["integrity"]["broken_at"] == 11
    assert data["integrity"]["operator_action_required"] is True
    assert data["integrity"]["auto_lockdown_applied"] is False
    assert activation_log == []


def test_root_proposer_does_not_auto_vote_on_high_risk_proposal(tmp_path):
    db_path = tmp_path / "moderation.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 1, "username": "root", "role": "super_admin"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    create = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 4, "action_type": "suspend", "reason": "嚴重違規"},
    )
    assert create.status_code == 200
    proposal = create.get_json()["proposal"]
    assert proposal["required_root_approval"] is True
    assert proposal["approve_count"] == 0
    assert proposal["status"] == "pending"

    root_vote = client.post(f"/api/admin/moderation/proposals/{proposal['id']}/vote", json={"vote": "approve"})
    assert root_vote.status_code == 403
    assert root_vote.get_json()["msg"] == "提案者不可投票"


def test_root_override_is_blocked(tmp_path):
    db_path = tmp_path / "moderation.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    create = client.post(
        "/api/admin/moderation/proposals",
        json={"target_user_id": 4, "action_type": "restrict", "restriction_features": ["community_post"], "reason": "洗版"},
    )
    proposal_id = create.get_json()["proposal"]["id"]

    actor_box["actor"] = {"id": 1, "username": "root", "role": "super_admin"}
    override = client.post(f"/api/root/moderation/proposals/{proposal_id}/override")
    assert override.status_code == 403


def test_mod_notes_and_reputation_account_api(tmp_path):
    db_path = tmp_path / "moderation.db"
    _seed_users(db_path)
    revoked = []
    actor_box = {"actor": {"id": 2, "username": "admin1", "role": "manager"}}
    client = _build_app(str(db_path), actor_box, revoked).test_client()

    note = client.post("/api/admin/mod-notes/4", json={"note": "需要觀察留言品質"})
    assert note.status_code == 200

    notes = client.get("/api/admin/mod-notes/4")
    assert notes.status_code == 200
    assert notes.get_json()["notes"][0]["note"] == "需要觀察留言品質"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_governance_records_schema(conn)
    add_reputation_event(conn, user_id=4, delta=10, reason="post_upvoted", source_user_id=2)
    conn.commit()
    conn.close()

    actor_box["actor"] = {"id": 4, "username": "alice", "role": "user"}
    summary = client.get("/api/account/reputation/summary")
    history = client.get("/api/account/reputation/history")

    assert summary.status_code == 200
    assert summary.get_json()["summary"]["current_reputation"] == 10
    assert summary.get_json()["summary"]["total_delta"] == 10
    assert history.status_code == 200
    assert history.get_json()["events"][0]["delta"] == 10
