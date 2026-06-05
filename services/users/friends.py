from datetime import datetime

from services.system.notifications import create_notification, notifications_enabled

from services.users.profiles import (
    ensure_user_profile_schema,
    get_or_create_profile,
    public_profile_info_for_payload,
    profile_style_for_payload,
    public_account_fields_for_payload,
)


FRIEND_STATUSES = {"pending", "accepted", "rejected", "blocked"}
PRIVILEGED_ROLES = {"manager", "super_admin"}
OFFICIAL_TARGET_CONTEXTS = {
    "admin_users",
    "admin_notice",
    "member_governance",
    "official_chat",
    "official_room",
}
PERSONAL_TARGET_CONTEXTS = {
    "personal",
    "pm",
    "private_chat",
    "private_group",
    "chat_invite",
    "cloud_drive_share",
    "video_share",
    "game_invite",
    "game_multiplayer_invite",
}
PRIVILEGED_ALL_USER_CONTEXTS = OFFICIAL_TARGET_CONTEXTS | {"pm", "private_chat"}


def _now():
    return datetime.now().isoformat()


def _dict(row):
    return dict(row) if row else None


def _value(record, key, default=None):
    if not record:
        return default
    if isinstance(record, dict):
        return record.get(key, default)
    try:
        value = record[key]
    except Exception:
        return default
    return default if value is None else value


def _username(record):
    return str(_value(record, "username", "") or "").strip()


def _create_friend_notification(conn, *, user_id, note_type, title, body, request_id=None):
    if not notifications_enabled(default=True):
        return False
    try:
        return create_notification(
            conn,
            user_id=int(user_id),
            type=note_type,
            title=title,
            body=body,
            link="#profile:friends",
            severity="info",
            audience="user",
            source_module="friends",
            source_ref=str(request_id or "") or None,
        )
    except Exception:
        return False


def _notify_friend_request_created(conn, *, actor, target, request_id):
    actor_name = _username(actor) or "有使用者"
    return _create_friend_notification(
        conn,
        user_id=int(target["id"]),
        note_type="friend_request",
        title="新的好友申請",
        body=f"{actor_name} 想加你為好友，請到個人面板的好友頁處理。",
        request_id=request_id,
    )


def _notify_friend_request_reviewed(conn, *, actor, row, accepted):
    requester_id = int(row["requested_by"] or 0)
    if requester_id <= 0 or requester_id == int(actor["id"]):
        return False
    actor_name = _username(actor) or "對方"
    return _create_friend_notification(
        conn,
        user_id=requester_id,
        note_type="friend_request_accepted" if accepted else "friend_request_rejected",
        title="好友申請已接受" if accepted else "好友申請已拒絕",
        body=f"{actor_name} 已接受你的好友申請。" if accepted else f"{actor_name} 已拒絕你的好友申請。",
        request_id=row["id"],
    )


def _table_columns(conn, table):
    columns = set()
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        try:
            columns.add(row["name"])
        except Exception:
            columns.add(row[1])
    return columns


def ensure_user_friends_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_friends (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            friend_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status         TEXT    NOT NULL DEFAULT 'pending',
            requested_by   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, friend_user_id),
            CHECK (user_id <> friend_user_id),
            CHECK (status IN ('pending', 'accepted', 'rejected', 'blocked'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_friends_user_status ON user_friends(user_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_friends_friend_status ON user_friends(friend_user_id, status)")


def ensure_user_follows_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_follows (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            follower_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            followed_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(follower_user_id, followed_user_id),
            CHECK (follower_user_id <> followed_user_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_follows_follower ON user_follows(follower_user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_follows_followed ON user_follows(followed_user_id, created_at)")


def ensure_social_schema(conn):
    ensure_user_profile_schema(conn)
    ensure_user_friends_schema(conn)
    ensure_user_follows_schema(conn)


def normalized_pair(user_id, friend_user_id):
    a, b = sorted([int(user_id), int(friend_user_id)])
    if a == b:
        raise ValueError("same user")
    return a, b


def is_privileged_actor(actor):
    if not actor:
        return False
    return _value(actor, "username") == "root" or _value(actor, "role") in PRIVILEGED_ROLES


def _active_user_clause(conn, alias="u"):
    cols = _table_columns(conn, "users")
    clauses = []
    if "deleted_at" in cols:
        clauses.append(f"({alias}.deleted_at IS NULL OR {alias}.deleted_at='')")
    if "status" in cols:
        clauses.append(f"({alias}.status IS NULL OR {alias}.status='active')")
    return (" AND " + " AND ".join(clauses)) if clauses else ""


def _user_payload(row, profile=None):
    user = _dict(row) or {}
    profile = profile or {}
    role = user.get("role") or "user"
    username = user.get("username") or ""
    return {
        "id": user.get("id"),
        "username": username,
        "role": role,
        "role_label": "Root" if username == "root" else ("Manager" if role == "manager" else ("管理員" if role == "super_admin" else "使用者")),
        "is_official": bool(username == "root" or role in PRIVILEGED_ROLES),
        "member_level": user.get("member_level") or user.get("effective_level") or "",
        "avatar_file_id": user.get("avatar_file_id") or "",
        "display_name": profile.get("display_name") or username,
    }


def _public_target_payload(conn, target):
    target = _dict(target)
    if not target:
        return {}
    profile = get_or_create_profile(conn, target["id"])
    return _user_payload(target, profile)


def find_active_user(conn, *, user_id=None, username=None):
    if user_id is None and username is None:
        return None
    where = "u.id=?" if user_id is not None else "u.username=?"
    params = (int(user_id),) if user_id is not None else (str(username or "").strip(),)
    row = conn.execute(
        f"SELECT u.* FROM users u WHERE {where}{_active_user_clause(conn, 'u')} LIMIT 1",
        params,
    ).fetchone()
    return _dict(row)


def get_friend_row(conn, user_id, friend_user_id):
    user_a, user_b = normalized_pair(user_id, friend_user_id)
    return _dict(
        conn.execute(
            "SELECT * FROM user_friends WHERE user_id=? AND friend_user_id=?",
            (user_a, user_b),
        ).fetchone()
    )


def describe_relationship(conn, viewer_id, target_user_id):
    if not viewer_id:
        return {"status": "anonymous", "request_id": None}
    if int(viewer_id) == int(target_user_id):
        return {"status": "self", "request_id": None}
    try:
        row = get_friend_row(conn, viewer_id, target_user_id)
    except ValueError:
        return {"status": "self", "request_id": None}
    if not row:
        return {"status": "none", "request_id": None}
    status = row.get("status")
    if status == "pending":
        status = "pending_outgoing" if int(row.get("requested_by") or 0) == int(viewer_id) else "pending_incoming"
    elif status == "blocked":
        status = "blocked" if int(row.get("requested_by") or 0) == int(viewer_id) else "blocked_by_them"
    return {"status": status, "request_id": row.get("id"), "requested_by": row.get("requested_by")}


def is_accepted_friend(conn, user_id, friend_user_id):
    if not user_id or not friend_user_id:
        return False
    ensure_user_friends_schema(conn)
    try:
        row = get_friend_row(conn, user_id, friend_user_id)
    except ValueError:
        return False
    return bool(row and row.get("status") == "accepted")


def is_blocked_relationship(conn, user_id, friend_user_id):
    if not user_id or not friend_user_id:
        return False
    ensure_user_friends_schema(conn)
    try:
        row = get_friend_row(conn, user_id, friend_user_id)
    except ValueError:
        return False
    return bool(row and row.get("status") == "blocked")


def is_following(conn, follower_user_id, followed_user_id):
    if not follower_user_id or not followed_user_id:
        return False
    ensure_user_follows_schema(conn)
    if int(follower_user_id) == int(followed_user_id):
        return False
    row = conn.execute(
        """
        SELECT 1 FROM user_follows
        WHERE follower_user_id=? AND followed_user_id=?
        LIMIT 1
        """,
        (int(follower_user_id), int(followed_user_id)),
    ).fetchone()
    return bool(row)


def follow_counts(conn, user_id):
    ensure_user_follows_schema(conn)
    uid = int(user_id)
    follower_count = conn.execute(
        "SELECT COUNT(*) AS total FROM user_follows WHERE followed_user_id=?",
        (uid,),
    ).fetchone()["total"]
    following_count = conn.execute(
        "SELECT COUNT(*) AS total FROM user_follows WHERE follower_user_id=?",
        (uid,),
    ).fetchone()["total"]
    return {"follower_count": int(follower_count or 0), "following_count": int(following_count or 0)}


def friend_count(conn, user_id):
    ensure_user_friends_schema(conn)
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM user_friends
        WHERE (user_id=? OR friend_user_id=?) AND status='accepted'
        """,
        (int(user_id), int(user_id)),
    ).fetchone()
    return int(row["total"] or 0)


def follower_user_ids(conn, user_id):
    ensure_user_follows_schema(conn)
    rows = conn.execute(
        """
        SELECT follower_user_id
        FROM user_follows
        WHERE followed_user_id=?
        ORDER BY created_at DESC, id DESC
        """,
        (int(user_id),),
    ).fetchall()
    return [int(row["follower_user_id"]) for row in rows]


def accepted_friend_ids(conn, user_id):
    if not user_id:
        return set()
    ensure_user_friends_schema(conn)
    uid = int(user_id)
    rows = conn.execute(
        """
        SELECT CASE WHEN user_id=? THEN friend_user_id ELSE user_id END AS friend_id
        FROM user_friends
        WHERE (user_id=? OR friend_user_id=?)
          AND status='accepted'
        """,
        (uid, uid, uid),
    ).fetchall()
    return {int(row["friend_id"]) for row in rows}


def _target_context(context):
    value = str(context or "personal").strip().lower()
    return value if value in OFFICIAL_TARGET_CONTEXTS or value in PERSONAL_TARGET_CONTEXTS else "personal"


def _privileged_actor_can_target_all(ctx, actor):
    return ctx in PRIVILEGED_ALL_USER_CONTEXTS and is_privileged_actor(actor)


def can_target_user(conn, actor, target_user_id, *, context="personal"):
    ensure_social_schema(conn)
    if not actor:
        return False, "未登入"
    try:
        actor_id = int(actor["id"])
        target_id = int(target_user_id)
    except Exception:
        return False, "指定對象格式錯誤"
    if actor_id == target_id:
        return False, "不能指定自己"
    ctx = _target_context(context)
    if is_blocked_relationship(conn, actor_id, target_id) and not _privileged_actor_can_target_all(ctx, actor):
        return False, "你與對方目前有封鎖關係，不能指定互動"
    if is_accepted_friend(conn, actor_id, target_id):
        return True, ""
    if _privileged_actor_can_target_all(ctx, actor):
        return True, ""
    return False, "一般指定對象只能選擇已成為好友的使用者"


def assert_can_target_user(conn, actor, target_user_id, *, context="personal"):
    allowed, msg = can_target_user(conn, actor, target_user_id, context=context)
    if not allowed:
        return False, msg
    return True, ""


def _target_option(row, *, actor_id, is_friend=False):
    user = _dict(row) or {}
    username = user.get("username") or ""
    role = user.get("role") or "user"
    official = bool(username == "root" or role in PRIVILEGED_ROLES)
    display = user.get("display_name") or username
    label_parts = [display or username]
    if username and display != username:
        label_parts.append(f"@{username}")
    if official:
        label_parts.append("官方/管理者")
    if is_friend:
        label_parts.append("好友")
    return {
        "id": user.get("id"),
        "username": username,
        "display_name": display,
        "role": role,
        "role_label": "Root" if username == "root" else ("Manager" if role == "manager" else ("管理員" if role == "super_admin" else "使用者")),
        "is_friend": bool(is_friend),
        "is_official": official,
        "label": " · ".join(part for part in label_parts if part),
    }


def list_targetable_users(conn, actor, *, context="personal", query="", limit=80):
    ensure_social_schema(conn)
    if not actor:
        return []
    try:
        actor_id = int(actor["id"])
    except Exception:
        return []
    ctx = _target_context(context)
    try:
        limit = min(max(int(limit), 1), 200)
    except Exception:
        limit = 80
    search = str(query or "").strip()
    user_cols = _table_columns(conn, "users")
    avatar_expr = "u.avatar_file_id" if "avatar_file_id" in user_cols else "'' AS avatar_file_id"
    member_expr = "u.member_level" if "member_level" in user_cols else "'' AS member_level"
    where = [f"u.id<>?", _active_user_clause(conn, "u").replace(" AND ", "", 1) or "1=1"]
    params = [actor_id]
    if search:
        where.append("(u.username LIKE ? OR COALESCE(p.display_name, '') LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if not _privileged_actor_can_target_all(ctx, actor):
        rows = conn.execute(
            f"""
            SELECT u.id, u.username, u.role, {avatar_expr}, {member_expr}, p.display_name
            FROM user_friends f
            JOIN users u ON u.id=CASE WHEN f.user_id=? THEN f.friend_user_id ELSE f.user_id END
            LEFT JOIN user_profiles p ON p.user_id=u.id
            WHERE (f.user_id=? OR f.friend_user_id=?)
              AND f.status='accepted'
              AND u.id<>?
              AND {_active_user_clause(conn, 'u').replace(' AND ', '', 1) or '1=1'}
              {("AND (u.username LIKE ? OR COALESCE(p.display_name, '') LIKE ?)" if search else "")}
            ORDER BY
              CASE WHEN u.username='root' THEN 0 WHEN u.role IN ('manager','super_admin') THEN 1 ELSE 2 END,
              COALESCE(p.display_name, u.username) COLLATE NOCASE
            LIMIT ?
            """,
            tuple([actor_id, actor_id, actor_id, actor_id] + ([f"%{search}%", f"%{search}%"] if search else []) + [limit]),
        ).fetchall()
        return [_target_option(row, actor_id=actor_id, is_friend=True) for row in rows]
    rows = conn.execute(
        f"""
        SELECT u.id, u.username, u.role, {avatar_expr}, {member_expr}, p.display_name
        FROM users u
        LEFT JOIN user_profiles p ON p.user_id=u.id
        WHERE {" AND ".join(where)}
        LIMIT 500
        """,
        tuple(params),
    ).fetchall()
    friend_ids = accepted_friend_ids(conn, actor_id)
    options = []
    for row in rows:
        friend = int(row["id"]) in friend_ids
        options.append(_target_option(row, actor_id=actor_id, is_friend=friend))
    options.sort(key=lambda item: (
        0 if item["is_friend"] else 1,
        0 if item["is_official"] else 1,
        str(item.get("display_name") or item.get("username") or "").lower(),
    ))
    return options[:limit]


def get_profile_payload(conn, *, target_user_id, viewer=None):
    ensure_social_schema(conn)
    target = find_active_user(conn, user_id=target_user_id)
    if not target:
        return None
    profile = get_or_create_profile(conn, target["id"])
    viewer_id = _value(viewer, "id") if viewer else None
    relation = describe_relationship(conn, viewer_id, target["id"])
    owner = relation["status"] == "self"
    privileged = is_privileged_actor(viewer)
    visibility = profile.get("profile_visibility") or "public"
    show_private = owner or privileged or visibility == "public" or (visibility == "members" and viewer_id)
    if visibility == "friends":
        show_private = owner or privileged or relation["status"] == "accepted"
    public_fields = {
        "bio": profile.get("bio") or "",
        "signature": profile.get("signature") or "",
        "location": profile.get("location") or "",
        "website": profile.get("website") or "",
        "profile_public_info": public_profile_info_for_payload(profile),
    } if show_private else {"bio": "", "signature": "", "location": "", "website": "", "profile_public_info": []}
    blocked_state = relation["status"] in {"blocked", "blocked_by_them"}
    can_interact = relation["status"] == "accepted" or privileged
    if blocked_state and not privileged:
        can_interact = False
    followed_by_viewer = bool(viewer_id and not owner and is_following(conn, viewer_id, target["id"]))
    follow_totals = follow_counts(conn, target["id"])
    payload = {
        **_user_payload(target, profile),
        **public_fields,
        "profile_visibility": visibility,
        "profile_template": profile.get("profile_template") or "classic",
        "profile_accent": profile.get("profile_accent") or "default",
        "profile_density": profile.get("profile_density") or "comfortable",
        "profile_style": profile_style_for_payload(profile),
        "friend_status": relation["status"],
        "friend_request_id": relation.get("request_id"),
        "friend_count": friend_count(conn, target["id"]),
        **follow_totals,
        "follow_status": "self" if owner else ("following" if followed_by_viewer else "not_following"),
        "can_request_friend": bool(viewer_id and relation["status"] in {"none", "rejected"}),
        "can_accept_friend": bool(viewer_id and relation["status"] == "pending_incoming"),
        "can_follow": bool(viewer_id and not owner and not blocked_state and not followed_by_viewer),
        "can_unfollow": bool(viewer_id and not owner and followed_by_viewer),
        "can_block": bool(viewer_id and not owner and not blocked_state),
        "can_unblock": bool(viewer_id and relation["status"] == "blocked"),
        "can_pm": bool(can_interact and not owner),
        "can_invite_game": bool(can_interact and not owner),
        "profile_updated_at": profile.get("updated_at"),
    }
    if owner:
        payload["friend_code"] = profile.get("friend_code") or ""
        payload["friend_code_rotated_at"] = profile.get("friend_code_rotated_at") or ""
        payload["display_timezone"] = profile.get("display_timezone") or "auto"
        payload["profile_public_account_fields"] = public_account_fields_for_payload(profile)
    return payload


def _friend_item_from_row(row, actor_id):
    item = _dict(row)
    other_is_right = int(item["user_id"]) == int(actor_id)
    other = {
        "id": item["friend_user_id"] if other_is_right else item["user_id"],
        "username": item["friend_username"] if other_is_right else item["user_username"],
        "role": item["friend_role"] if other_is_right else item["user_role"],
        "avatar_file_id": item.get("friend_avatar_file_id") if other_is_right else item.get("user_avatar_file_id"),
        "display_name": item.get("friend_display_name") if other_is_right else item.get("user_display_name"),
    }
    requested_by_username = item.get("requested_by_username") or ""
    return {
        "id": item["id"],
        "user_id": item["user_id"],
        "friend_user_id": item["friend_user_id"],
        "other_user_id": other["id"],
        "other_username": other["username"],
        "other_display_name": other["display_name"] or other["username"],
        "other_role": other["role"] or "user",
        "other_avatar_file_id": other["avatar_file_id"] or "",
        "other_is_official": bool(other["username"] == "root" or other["role"] in PRIVILEGED_ROLES),
        "status": item["status"],
        "requested_by": item["requested_by"],
        "requested_by_username": requested_by_username,
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def list_friend_state(conn, actor):
    ensure_social_schema(conn)
    user_cols = _table_columns(conn, "users")
    user_avatar_expr = "u1.avatar_file_id AS user_avatar_file_id" if "avatar_file_id" in user_cols else "'' AS user_avatar_file_id"
    friend_avatar_expr = "u2.avatar_file_id AS friend_avatar_file_id" if "avatar_file_id" in user_cols else "'' AS friend_avatar_file_id"
    rows = conn.execute(
        f"""
        SELECT f.id, f.user_id, f.friend_user_id, f.status, f.requested_by, f.created_at, f.updated_at,
               u1.username AS user_username, u1.role AS user_role, {user_avatar_expr},
               p1.display_name AS user_display_name,
               u2.username AS friend_username, u2.role AS friend_role, {friend_avatar_expr},
               p2.display_name AS friend_display_name,
               req.username AS requested_by_username
        FROM user_friends f
        JOIN users u1 ON u1.id=f.user_id
        JOIN users u2 ON u2.id=f.friend_user_id
        LEFT JOIN user_profiles p1 ON p1.user_id=u1.id
        LEFT JOIN user_profiles p2 ON p2.user_id=u2.id
        LEFT JOIN users req ON req.id=f.requested_by
        WHERE (f.user_id=? OR f.friend_user_id=?)
          {_active_user_clause(conn, 'u1')}
          {_active_user_clause(conn, 'u2')}
        ORDER BY
          CASE
            WHEN (CASE WHEN f.user_id=? THEN u2.username ELSE u1.username END)='root' THEN 0
            WHEN (CASE WHEN f.user_id=? THEN u2.role ELSE u1.role END) IN ('manager','super_admin') THEN 1
            ELSE 2
          END,
          f.updated_at DESC,
          f.id DESC
        """,
        (actor["id"], actor["id"], actor["id"], actor["id"]),
    ).fetchall()
    friends = []
    incoming = []
    outgoing = []
    blocked = []
    for row in rows:
        item = _friend_item_from_row(row, actor["id"])
        if item["status"] == "accepted":
            friends.append(item)
        elif item["status"] == "pending" and int(item["requested_by"]) == int(actor["id"]):
            outgoing.append(item)
        elif item["status"] == "pending":
            incoming.append(item)
        elif item["status"] == "blocked" and int(item["requested_by"]) == int(actor["id"]):
            blocked.append(item)
    return {"friends": friends, "incoming": incoming, "outgoing": outgoing, "blocked": blocked}


def create_friend_request(conn, actor, *, target_user_id=None, username=None):
    ensure_social_schema(conn)
    target = find_active_user(conn, user_id=target_user_id, username=username)
    if not target:
        return None, "找不到指定帳號", 404
    if int(target["id"]) == int(actor["id"]):
        return None, "不能加自己為好友", 400
    user_a, user_b = normalized_pair(actor["id"], target["id"])
    existing = get_friend_row(conn, actor["id"], target["id"])
    public_target = _public_target_payload(conn, target)
    now = _now()
    if existing:
        status = existing["status"]
        if status == "accepted":
            return {"status": "accepted", "target": public_target}, "已經是好友", 200
        if status == "blocked":
            return None, "無法送出好友申請", 403
        if status == "pending":
            if int(existing["requested_by"]) != int(actor["id"]):
                conn.execute(
                    "UPDATE user_friends SET status='accepted', updated_at=? WHERE id=?",
                    (now, existing["id"]),
                )
                _notify_friend_request_reviewed(conn, actor=actor, row=existing, accepted=True)
                return {"status": "accepted", "target": public_target, "request_id": existing["id"]}, "已加入好友", 200
            return {"status": "pending", "target": public_target, "request_id": existing["id"]}, "好友邀請已存在", 200
        conn.execute(
            "UPDATE user_friends SET status='pending', requested_by=?, updated_at=? WHERE id=?",
            (actor["id"], now, existing["id"]),
        )
        _notify_friend_request_created(conn, actor=actor, target=target, request_id=existing["id"])
        return {"status": "pending", "target": public_target, "request_id": existing["id"]}, "好友邀請已送出", 200
    cur = conn.execute(
        "INSERT INTO user_friends (user_id, friend_user_id, status, requested_by, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?, ?)",
        (user_a, user_b, actor["id"], now, now),
    )
    _notify_friend_request_created(conn, actor=actor, target=target, request_id=cur.lastrowid)
    return {"status": "pending", "target": public_target, "request_id": cur.lastrowid}, "好友邀請已送出", 200


def accept_friend_by_code(conn, actor, *, friend_code):
    ensure_social_schema(conn)
    code = str(friend_code or "").strip()
    if not code:
        return None, "請輸入好友代碼", 400
    row = conn.execute(
        f"""
        SELECT u.* FROM user_profiles p
        JOIN users u ON u.id=p.user_id
        WHERE p.friend_code=?{_active_user_clause(conn, 'u')}
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    target = _dict(row)
    if not target:
        return None, "查無使用者", 404
    if int(target["id"]) == int(actor["id"]):
        return None, "不能加自己為好友", 400
    user_a, user_b = normalized_pair(actor["id"], target["id"])
    existing = get_friend_row(conn, actor["id"], target["id"])
    public_target = _public_target_payload(conn, target)
    now = _now()
    if existing:
        if existing["status"] == "accepted":
            return {"status": "accepted", "target": public_target, "request_id": existing["id"]}, "已經是好友", 200
        if existing["status"] == "blocked":
            return None, "無法建立好友關係", 403
        conn.execute(
            "UPDATE user_friends SET status='accepted', requested_by=?, updated_at=? WHERE id=?",
            (actor["id"], now, existing["id"]),
        )
        if int(existing["requested_by"] or 0) != int(actor["id"]):
            _notify_friend_request_reviewed(conn, actor=actor, row=existing, accepted=True)
        return {"status": "accepted", "target": public_target, "request_id": existing["id"]}, "已加入好友", 200
    cur = conn.execute(
        "INSERT INTO user_friends (user_id, friend_user_id, status, requested_by, created_at, updated_at) VALUES (?, ?, 'accepted', ?, ?, ?)",
        (user_a, user_b, actor["id"], now, now),
    )
    return {"status": "accepted", "target": public_target, "request_id": cur.lastrowid}, "已加入好友", 200


def review_friend_request(conn, actor, *, request_id, decision):
    ensure_social_schema(conn)
    if decision not in {"accept", "reject"}:
        return None, "不支援的操作", 400
    row = _dict(conn.execute("SELECT * FROM user_friends WHERE id=?", (int(request_id),)).fetchone())
    if not row:
        return None, "找不到好友邀請", 404
    if row["status"] != "pending":
        return None, "好友邀請已處理", 409
    if int(row["requested_by"]) == int(actor["id"]) or int(actor["id"]) not in {int(row["user_id"]), int(row["friend_user_id"])}:
        return None, "你不能處理這筆好友邀請", 403
    status = "accepted" if decision == "accept" else "rejected"
    conn.execute(
        "UPDATE user_friends SET status=?, updated_at=? WHERE id=?",
        (status, _now(), int(request_id)),
    )
    _notify_friend_request_reviewed(conn, actor=actor, row=row, accepted=(decision == "accept"))
    return {"status": status, "request_id": int(request_id)}, "已加入好友" if decision == "accept" else "已拒絕好友邀請", 200


def remove_friend(conn, actor, *, friend_user_id):
    ensure_social_schema(conn)
    try:
        user_a, user_b = normalized_pair(actor["id"], friend_user_id)
    except ValueError:
        return False, "不能解除自己", 400
    cur = conn.execute(
        "DELETE FROM user_friends WHERE user_id=? AND friend_user_id=? AND status='accepted'",
        (user_a, user_b),
    )
    if cur.rowcount < 1:
        return False, "找不到好友關係", 404
    return True, "已解除好友", 200


def block_user(conn, actor, *, target_user_id):
    ensure_social_schema(conn)
    target = find_active_user(conn, user_id=target_user_id)
    if not target:
        return None, "找不到指定帳號", 404
    if int(target["id"]) == int(actor["id"]):
        return None, "不能封鎖自己", 400
    user_a, user_b = normalized_pair(actor["id"], target["id"])
    existing = get_friend_row(conn, actor["id"], target["id"])
    public_target = _public_target_payload(conn, target)
    now = _now()
    if existing:
        if existing["status"] == "blocked":
            if int(existing.get("requested_by") or 0) == int(actor["id"]):
                return {"status": "blocked", "target": public_target, "request_id": existing["id"]}, "已封鎖使用者", 200
            return None, "對方已封鎖你，無法變更封鎖狀態", 403
        conn.execute(
            "UPDATE user_friends SET status='blocked', requested_by=?, updated_at=? WHERE id=?",
            (actor["id"], now, existing["id"]),
        )
        request_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO user_friends (user_id, friend_user_id, status, requested_by, created_at, updated_at) VALUES (?, ?, 'blocked', ?, ?, ?)",
            (user_a, user_b, actor["id"], now, now),
        )
        request_id = cur.lastrowid
    conn.execute(
        """
        DELETE FROM user_follows
        WHERE (follower_user_id=? AND followed_user_id=?)
           OR (follower_user_id=? AND followed_user_id=?)
        """,
        (int(actor["id"]), int(target["id"]), int(target["id"]), int(actor["id"])),
    )
    return {"status": "blocked", "target": public_target, "request_id": request_id}, "已封鎖使用者", 200


def unblock_user(conn, actor, *, target_user_id):
    ensure_social_schema(conn)
    try:
        user_a, user_b = normalized_pair(actor["id"], target_user_id)
    except ValueError:
        return False, "不能解除封鎖自己", 400
    row = get_friend_row(conn, actor["id"], target_user_id)
    if not row or row.get("status") != "blocked":
        return False, "找不到封鎖關係", 404
    if int(row.get("requested_by") or 0) != int(actor["id"]):
        return False, "只能解除自己建立的封鎖", 403
    cur = conn.execute(
        "DELETE FROM user_friends WHERE user_id=? AND friend_user_id=? AND status='blocked'",
        (user_a, user_b),
    )
    if cur.rowcount < 1:
        return False, "找不到封鎖關係", 404
    return True, "已解除封鎖", 200


def follow_user(conn, actor, *, target_user_id):
    ensure_social_schema(conn)
    target = find_active_user(conn, user_id=target_user_id)
    if not target:
        return None, "找不到指定帳號", 404
    if int(target["id"]) == int(actor["id"]):
        return None, "不能追蹤自己", 400
    if is_blocked_relationship(conn, actor["id"], target["id"]):
        return None, "你與對方目前有封鎖關係，不能追蹤", 403
    public_target = _public_target_payload(conn, target)
    now = _now()
    conn.execute(
        """
        INSERT OR IGNORE INTO user_follows (follower_user_id, followed_user_id, created_at)
        VALUES (?, ?, ?)
        """,
        (int(actor["id"]), int(target["id"]), now),
    )
    return {"status": "following", "target": public_target}, "已追蹤", 200


def unfollow_user(conn, actor, *, target_user_id):
    ensure_social_schema(conn)
    if int(target_user_id) == int(actor["id"]):
        return False, "不能取消追蹤自己", 400
    cur = conn.execute(
        "DELETE FROM user_follows WHERE follower_user_id=? AND followed_user_id=?",
        (int(actor["id"]), int(target_user_id)),
    )
    if cur.rowcount < 1:
        return False, "尚未追蹤此使用者", 404
    return True, "已取消追蹤", 200
