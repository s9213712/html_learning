import json
from datetime import datetime, timedelta

ACTION_TYPES = {
    "warn",
    "mute",
    "restrict",
    "suspend",
    "delete",
    "downgrade_level",
    "force_password_reset",
}
PROPOSAL_STATUSES = {"pending", "approved", "rejected", "expired", "cancelled", "executed"}
VOTES = {"approve", "reject"}
HIGH_RISK_ACTION_TYPES = {"suspend", "delete", "downgrade_level"}
MAX_PROPOSAL_TTL_HOURS = 24 * 365


def governance_policy_for_action(action_type, target_role=None):
    target_role = str(target_role or "user")
    high_risk = action_type in HIGH_RISK_ACTION_TYPES or target_role in {"manager", "super_admin"}
    if high_risk:
        return {
            "risk_level": "high",
            "required_votes": 3,
            "required_root_approval": True,
            "required_manager_approvals": 2,
            "summary": "高風險：需要 root 同意，且另外需要 2 位 admin/manager 同意。",
        }
    return {
        "risk_level": "normal",
        "required_votes": 1,
        "required_root_approval": False,
        "required_manager_approvals": 1,
        "summary": "一般：需要 1 位 admin/manager 或 root 同意。",
    }


def proposal_policy_from_row(row):
    data = dict(row) if row else {}
    fallback = governance_policy_for_action(data.get("action_type"), data.get("target_role"))
    risk_level = data.get("risk_level") or fallback["risk_level"]
    required_root_approval = bool(data.get("required_root_approval")) if "required_root_approval" in data else fallback["required_root_approval"]
    required_manager_approvals = int(data.get("required_manager_approvals") or fallback["required_manager_approvals"])
    required_votes = int(data.get("required_votes") or fallback["required_votes"])
    return {
        "risk_level": risk_level,
        "required_votes": required_votes,
        "required_root_approval": required_root_approval,
        "required_manager_approvals": required_manager_approvals,
        "summary": (
            "高風險：需要 root 同意，且另外需要 2 位 admin/manager 同意。"
            if required_root_approval
            else "一般：需要 1 位 admin/manager 或 root 同意。"
        ),
    }


def ensure_moderation_proposals_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS moderation_proposals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            target_user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action_type         TEXT NOT NULL,
            action_value        TEXT,
            proposed_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason              TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending',
            required_votes      INTEGER NOT NULL DEFAULT 2,
            approve_count       INTEGER NOT NULL DEFAULT 0,
            reject_count        INTEGER NOT NULL DEFAULT 0,
            expires_at          TEXT NOT NULL,
            executed_at         TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS moderation_votes (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id    INTEGER NOT NULL REFERENCES moderation_proposals(id) ON DELETE CASCADE,
            voter_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vote           TEXT NOT NULL,
            comment        TEXT,
            created_at     TEXT NOT NULL,
            UNIQUE(proposal_id, voter_user_id)
        )
        """
    )
    proposal_cols = {row["name"] for row in conn.execute("PRAGMA table_info(moderation_proposals)").fetchall()}
    if "action_value" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN action_value TEXT")
    if "risk_level" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN risk_level TEXT NOT NULL DEFAULT 'normal'")
    if "required_root_approval" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN required_root_approval INTEGER NOT NULL DEFAULT 0")
    if "required_manager_approvals" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN required_manager_approvals INTEGER NOT NULL DEFAULT 1")
    if "action_payload_json" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN action_payload_json TEXT NOT NULL DEFAULT '{}'")
    if "is_emergency" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN is_emergency INTEGER NOT NULL DEFAULT 0")
    if "emergency_applied_at" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN emergency_applied_at TEXT")
    if "emergency_reverted_at" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN emergency_reverted_at TEXT")
    if "emergency_revert_reason" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN emergency_revert_reason TEXT")
    if "emergency_previous_state_json" not in proposal_cols:
        conn.execute("ALTER TABLE moderation_proposals ADD COLUMN emergency_previous_state_json TEXT NOT NULL DEFAULT '{}'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moderation_proposals_status ON moderation_proposals(status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moderation_proposals_target ON moderation_proposals(target_user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moderation_proposals_emergency ON moderation_proposals(is_emergency, status, emergency_applied_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moderation_votes_proposal ON moderation_votes(proposal_id)")


def _json_loads(value, fallback):
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except Exception:
        return fallback


def _json_dumps(value):
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def proposal_row_to_dict(row):
    if not row:
        return None
    data = dict(row)
    for key in ("required_votes", "approve_count", "reject_count"):
        data[key] = int(data.get(key) or 0)
    try:
        data["is_emergency"] = bool(int(data.get("is_emergency") or 0))
    except Exception:
        data["is_emergency"] = str(data.get("is_emergency") or "").strip().lower() in {"1", "true", "yes"}
    data["action_payload"] = _json_loads(data.get("action_payload_json"), {})
    data["emergency_previous_state"] = _json_loads(data.get("emergency_previous_state_json"), {})
    data.update(proposal_policy_from_row(data))
    return data


def validate_action_payload(action_type, action_value):
    if action_type not in ACTION_TYPES:
        return False, "不支援的提案動作"
    if action_type == "downgrade_level" and action_value not in {"newbie", "normal", "restricted", "suspended"}:
        return False, "downgrade_level 需指定 newbie/normal/restricted/suspended"
    return True, ""


def create_moderation_proposal(
    conn,
    *,
    target_user_id,
    action_type,
    action_value,
    proposed_by_user_id,
    reason,
    required_votes=None,
    ttl_hours=72,
    risk_level=None,
    required_root_approval=None,
    required_manager_approvals=None,
    action_payload=None,
    is_emergency=False,
    emergency_previous_state=None,
):
    ensure_moderation_proposals_schema(conn)
    ok, msg = validate_action_payload(action_type, action_value)
    if not ok:
        return None, msg
    fallback_policy = governance_policy_for_action(action_type)
    risk_level = risk_level or fallback_policy["risk_level"]
    required_root_approval = fallback_policy["required_root_approval"] if required_root_approval is None else bool(required_root_approval)
    try:
        required_manager_approvals = max(1, int(required_manager_approvals or fallback_policy["required_manager_approvals"]))
    except Exception:
        return None, "required_manager_approvals 格式錯誤"
    try:
        required_votes = max(1, int(required_votes or fallback_policy["required_votes"]))
    except Exception:
        return None, "required_votes 格式錯誤"
    reason = (reason or "").strip()
    if not reason:
        return None, "提案原因不可為空"
    try:
        ttl_hours = int(ttl_hours or 72)
    except Exception:
        return None, "提案有效期限格式錯誤"
    if ttl_hours < 1 or ttl_hours > MAX_PROPOSAL_TTL_HOURS:
        return None, f"提案有效期限需介於 1 到 {MAX_PROPOSAL_TTL_HOURS} 小時"
    now = datetime.now()
    expires_at = (now + timedelta(hours=ttl_hours)).isoformat()
    cur = conn.execute(
        "INSERT INTO moderation_proposals "
        "(target_user_id, action_type, action_value, proposed_by_user_id, reason, status, required_votes, "
        "approve_count, reject_count, expires_at, created_at, updated_at, risk_level, required_root_approval, required_manager_approvals, "
        "action_payload_json, is_emergency, emergency_previous_state_json) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(target_user_id),
            action_type,
            action_value,
            int(proposed_by_user_id),
            reason[:1000],
            required_votes,
            expires_at,
            now.isoformat(),
            now.isoformat(),
            risk_level,
            1 if required_root_approval else 0,
            required_manager_approvals,
            _json_dumps(action_payload),
            1 if is_emergency else 0,
            _json_dumps(emergency_previous_state),
        ),
    )
    row = conn.execute("SELECT * FROM moderation_proposals WHERE id=?", (cur.lastrowid,)).fetchone()
    return proposal_row_to_dict(row), None


def proposal_vote_state(conn, proposal):
    proposal = proposal_row_to_dict(proposal) if not isinstance(proposal, dict) else dict(proposal)
    votes = conn.execute(
        "SELECT v.vote, u.username, u.role "
        "FROM moderation_votes v JOIN users u ON u.id=v.voter_user_id "
        "WHERE v.proposal_id=?",
        (proposal["id"],),
    ).fetchall()
    approve_count = 0
    reject_count = 0
    manager_approve_count = 0
    manager_reject_count = 0
    root_approved = False
    root_rejected = False
    for vote in votes:
        vote_value = vote["vote"]
        username = vote["username"]
        role = vote["role"]
        if vote_value == "approve":
            approve_count += 1
            if username == "root":
                root_approved = True
            elif role == "manager":
                manager_approve_count += 1
        elif vote_value == "reject":
            reject_count += 1
            if username == "root":
                root_rejected = True
            elif role == "manager":
                manager_reject_count += 1
    policy = proposal_policy_from_row(proposal)
    requires_root = bool(policy["required_root_approval"])
    manager_required = int(policy["required_manager_approvals"])
    root_requirement_met = (not requires_root) or root_approved or proposal.get("proposed_by_username") == "root"
    manager_requirement_met = manager_approve_count >= manager_required
    normal_requirement_met = approve_count >= int(policy["required_votes"])
    approved = (root_requirement_met and manager_requirement_met) if requires_root else normal_requirement_met
    rejected = (root_rejected or manager_reject_count >= manager_required) if requires_root else reject_count >= int(policy["required_votes"])
    return {
        "approve_count": approve_count,
        "reject_count": reject_count,
        "manager_approve_count": manager_approve_count,
        "manager_reject_count": manager_reject_count,
        "root_approved": root_approved,
        "root_rejected": root_rejected,
        "root_requirement_met": root_requirement_met,
        "manager_requirement_met": manager_requirement_met,
        "approved": approved,
        "rejected": rejected,
        **policy,
    }


def refresh_proposal_vote_counts(conn, proposal_id):
    proposal = conn.execute("SELECT * FROM moderation_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not proposal:
        return None
    state = proposal_vote_state(conn, proposal)
    approve_count = int(state["approve_count"])
    reject_count = int(state["reject_count"])
    status = proposal["status"]
    now = datetime.now()
    if status == "pending" and proposal["expires_at"] <= now.isoformat():
        status = "expired"
    elif status == "pending" and state["approved"]:
        status = "approved"
    elif status == "pending" and state["rejected"]:
        status = "rejected"
    conn.execute(
        "UPDATE moderation_proposals SET approve_count=?, reject_count=?, status=?, updated_at=? WHERE id=?",
        (approve_count, reject_count, status, now.isoformat(), proposal_id),
    )
    row = conn.execute("SELECT * FROM moderation_proposals WHERE id=?", (proposal_id,)).fetchone()
    data = proposal_row_to_dict(row)
    data.update(proposal_vote_state(conn, data))
    return data


def cast_action_value(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip() or None
