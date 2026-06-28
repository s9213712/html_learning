from pathlib import Path

import pytest

from services.core.sqlite_safe import table_columns


ROOT = Path(__file__).resolve().parents[2]


def test_admin_mutation_routes_use_session_scoped_csrf_guards():
    system_admin = (
        (ROOT / "routes" / "system_admin.py").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "routes" / "system_admin_sections" / "security_routes.py").read_text(encoding="utf-8")
    )
    auth = (ROOT / "services" / "users" / "auth.py").read_text(encoding="utf-8")

    assert '@app.route("/api/admin/security-center/thresholds", methods=["PUT"])\n    @require_csrf' in system_admin
    assert '@app.route("/api/admin/security-center/controls", methods=["PUT"])\n    @require_csrf' in system_admin
    assert 'CSRF_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}' in auth
    assert "def _rotate_authenticated_csrf(response, csrf_tok, username):" in auth
    assert 'response = _rotate_authenticated_csrf(response, csrf_tok, user)' in auth
    assert "elif csrf_tok:" in auth
    assert "consume_csrf_token(csrf_tok, csrf_owner)" in auth
    assert '"error": "csrf_invalid"' in auth


def test_community_combo_mutation_routes_dispatch_to_single_use_csrf():
    community = (ROOT / "routes" / "community.py").read_text(encoding="utf-8")

    assert "def require_csrf_by_method(fn):" in community
    assert "strict = require_csrf(fn)" in community
    assert 'if request.method in {"GET", "HEAD", "OPTIONS"}:' in community
    assert community.count("@require_csrf_by_method") >= 6
    assert '@app.route("/api/community/announcements", methods=["GET", "POST"])\n    @require_csrf_by_method' in community
    assert '@app.route("/api/community/categories", methods=["GET", "POST"])\n    @require_csrf_by_method' in community
    assert '@app.route("/api/community/boards", methods=["GET", "POST"])\n    @require_csrf_by_method' in community
    assert '@app.route("/api/community/boards/<int:board_id>/moderators", methods=["GET", "POST"])\n    @require_csrf_by_method' in community
    assert '@app.route("/api/community/boards/<int:board_id>/threads", methods=["GET", "POST"])\n    @require_csrf_by_method' in community
    assert '@app.route("/api/community/threads/<int:thread_id>", methods=["GET", "PUT", "DELETE"])\n    @require_csrf_by_method' in community


def test_community_numeric_bounds_and_unlisted_reply_access_are_guarded():
    community = (ROOT / "routes" / "community.py").read_text(encoding="utf-8")
    reward_route = community.split("def community_thread_reward", 1)[1].split("def community_thread_reply", 1)[0]
    reply_route = community.split("def community_thread_reply", 1)[1].split("def community_thread_pin", 1)[0]
    penalty_route = community.split("def community_post_penalty", 1)[1].split("def community_post_report", 1)[0]

    assert 'if points is None:' in reward_route
    assert "獎勵點數必須介於 1 到 50" in reward_route
    assert 'if points is None:' in penalty_route
    assert "違規點數必須介於 1 到 10" in penalty_route
    assert "board_open = (" in reply_route
    assert "accessible = manageable or board_open or thread[\"owner_user_id\"] == actor[\"id\"]" in reply_route
    assert "此討論區不開放留言" in reply_route


def test_margin_collateral_without_client_key_uses_stable_retry_key():
    trading = (ROOT / "services" / "trading" / "margin.py").read_text(encoding="utf-8")
    collateral_route = trading.split("def add_margin_collateral", 1)[1].split("def close_margin_position", 1)[0]

    assert 'fallback_key = idempotency_key or f"{position_uuid}:{amount}:{int(datetime.now().timestamp() // 60)}"' in collateral_route
    assert "uuid.uuid4()" not in collateral_route
    assert 'idempotency_key=f"trading:margin:collateral_add:{operation_key}"' in collateral_route


def test_points_spend_route_does_not_trust_client_ledger_provenance():
    economy = (ROOT / "routes" / "economy.py").read_text(encoding="utf-8")

    assert 'reference_type="price_catalog"' in economy
    assert 'reference_id=f"catalog:{item_key}"' in economy
    assert "metadata={}" in economy
    assert "_stable_spend_key" in economy
    stable_key = economy.split("def _stable_spend_key", 1)[1].split("def service_error", 1)[0]
    assert "minute_bucket" in stable_key
    assert "int(time.time() // 60)" in stable_key
    assert 'f"spend:{user_id}:{item_key}:{quantity}"' not in stable_key


def test_economy_admin_pending_rewards_are_disabled_in_blockchain_model():
    economy = (ROOT / "routes" / "economy.py").read_text(encoding="utf-8")
    pending_route = economy.split('def admin_points_pending_rewards():', 1)[1].split(
        '@app.route("/api/admin/points/pending-rewards/<int:pending_reward_id>/review"',
        1,
    )[0]

    assert "def parse_required_user_id" in economy
    assert '"code": "blockchain_permission_model"' in pending_route
    assert "官方撥款需改走治理提案與官方多簽" in pending_route
    assert "points_service.create_pending_reward" not in pending_route
    assert 'user_id=int(data.get("user_id"))' not in pending_route


def test_moderation_execute_claims_proposal_under_write_lock():
    moderation = (ROOT / "routes" / "moderation.py").read_text(encoding="utf-8")
    execute_route = moderation.split("def moderation_proposal_execute", 1)[1].split(
        '@app.route("/api/root/moderation/proposals/<int:proposal_id>/override"',
        1,
    )[0]

    assert 'conn.execute("BEGIN IMMEDIATE")' in execute_route
    assert "status='executing'" in execute_route
    assert execute_route.index('conn.execute("BEGIN IMMEDIATE")') < execute_route.index("refresh_proposal_vote_counts")
    assert execute_route.index("status='executing'") < execute_route.index("execute_proposal_action")


def test_root_economy_catalog_write_uses_single_use_csrf():
    economy = (ROOT / "routes" / "economy.py").read_text(encoding="utf-8")

    assert '@app.route("/api/root/economy/catalog", methods=["GET"])\n    @require_csrf_safe' in economy
    assert '@app.route("/api/root/economy/catalog", methods=["POST"])\n    @require_csrf' in economy


def test_avatar_admin_endpoint_uses_role_rank():
    users = (ROOT / "routes" / "users.py").read_text(encoding="utf-8")
    avatar_get = users.split('def user_avatar_get(user_id):', 1)[1].split('@app.route("/api/admin/users/<int:user_id>", methods=["PUT", "DELETE"])', 1)[0]

    assert "Avatars are public identity assets inside authenticated areas" in avatar_get
    assert 'role_rank(actor_role) < role_rank("manager")' not in avatar_get
    assert 'actor_role not in {"admin", "super_admin"}' not in avatar_get


def test_avatar_payloads_are_available_before_frontend_renders_images():
    public = (ROOT / "routes" / "public.py").read_text(encoding="utf-8")
    chat = (ROOT / "routes" / "chat.py").read_text(encoding="utf-8")
    community = (ROOT / "routes" / "community.py").read_text(encoding="utf-8")

    assert '"avatar_file_id": ((avatar_row["avatar_file_id"] if avatar_row and "avatar_file_id" in avatar_row.keys() else dict(ctx).get("avatar_file_id")) or "")' in public
    assert '"sender_avatar_file_id": avatar_file_id' in chat
    assert 'if anonymous_to_viewer and not is_self:\n            avatar_file_id = ""' in chat
    assert '"author_avatar_file_id": row_value(row, "author_avatar_file_id", "")' in community
    assert "COALESCE(u.avatar_file_id, '') AS author_avatar_file_id" in community


def test_admin_users_post_uses_method_aware_csrf_guard():
    users = (ROOT / "routes" / "users.py").read_text(encoding="utf-8")

    assert "def require_csrf_by_method(fn):" in users
    assert "strict = require_csrf(fn)" in users
    assert 'if request.method in {"GET", "HEAD", "OPTIONS"}:' in users
    assert '@app.route("/api/admin/users", methods=["GET","POST"])\n    @require_csrf_by_method' in users


def test_admin_password_reset_review_hashes_reviewed_credential():
    users = (ROOT / "routes" / "users.py").read_text(encoding="utf-8")
    recovery = (ROOT / "services" / "users" / "recovery.py").read_text(encoding="utf-8")
    reset_helper = users.split("def _apply_reviewed_password_reset", 1)[1].split("def admin_users", 1)[0]

    assert "hash_password(new_credential)" in reset_helper
    assert "hash_password(password)" not in reset_helper
    assert "u.username AS username" in recovery
    assert 'delete_csrf_tokens_for_username(request_row["username"])' in reset_helper


def test_user_demote_accepts_optional_json_body_and_frontend_sends_json():
    users = (ROOT / "routes" / "users.py").read_text(encoding="utf-8")
    auth_users_js = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    demote_route = users.split('def admin_user_demote(user_id):', 1)[1].split('def admin_user_violation', 1)[0]
    demote_frontend = auth_users_js.split('async function demoteUser', 1)[1].split('// ── Module', 1)[0]

    assert 'request.get_json(silent=True) or {}' in demote_route
    assert 'request.get_json(force=True) or {}' not in demote_route
    assert '"Content-Type": "application/json"' in demote_frontend
    assert 'body: JSON.stringify({})' in demote_frontend


def test_user_promote_button_is_rendered_and_frontend_sends_json():
    users_js = (ROOT / "public" / "js" / "10-users.js").read_text(encoding="utf-8")
    auth_users_js = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    promote_frontend = auth_users_js.split('async function promoteUser', 1)[1].split('async function updateUserMemberLevel', 1)[0]
    promote_route = (ROOT / "routes" / "users.py").read_text(encoding="utf-8").split('def admin_user_promote(user_id):', 1)[1].split('def admin_user_demote', 1)[0]

    assert 'currentRole === "super_admin" && u.role === "user" && !isSelf' in users_js
    assert 'promoteUser(u.id, u.username)' in users_js
    assert '"Content-Type": "application/json"' in promote_frontend
    assert 'body: JSON.stringify({})' in promote_frontend
    assert 'flash(msgEl, json.msg || "升級完成", true);' in promote_frontend
    assert 'flash(msgEl, json.msg || `升級失敗（HTTP ${res.status}）`, false);' in promote_frontend
    assert promote_route.index("conn.commit()") < promote_route.index("delete_csrf_tokens_for_username")


def test_manual_points_adjustment_flow_is_disabled_in_blockchain_model():
    economy_route = (ROOT / "routes" / "economy.py").read_text(encoding="utf-8")
    economy_js = (ROOT / "public" / "js" / "55-economy.js").read_text(encoding="utf-8")
    adjust_route = economy_route.split("def admin_points_adjust():", 1)[1].split(
        '@app.route("/api/admin/points/pending-rewards"',
        1,
    )[0]

    assert '"code": "blockchain_permission_model"' in adjust_route
    assert "私有鏈模式已停用手動加減積分" in adjust_route
    assert "points_service.record_transaction" not in adjust_route
    assert "async function submitEconomyAdjustment" not in economy_js
    assert "economyRequestId(\"admin-adjust\")" not in economy_js


def test_address_dispute_serializer_redacts_account_identity_fields():
    service = (ROOT / "services" / "points_chain" / "service.py").read_text(encoding="utf-8")
    serializer = service.split("def _serialize_transaction_dispute", 1)[1].split("def list_transaction_disputes", 1)[0]

    for forbidden in (
        "reporter_user_id",
        "reporter_username",
        '"username"',
        '"email"',
        '"ip"',
        '"ip_address"',
        '"client_ip"',
    ):
        assert forbidden not in serializer
    assert '"reviewed_by": "governance_operator"' in serializer


def test_member_rights_changes_send_notice_and_appeal_path():
    users = (ROOT / "routes" / "users.py").read_text(encoding="utf-8")
    economy = (ROOT / "routes" / "economy.py").read_text(encoding="utf-8")
    appeals = (ROOT / "routes" / "appeals.py").read_text(encoding="utf-8")
    notices = (ROOT / "services" / "governance" / "sanction_notices.py").read_text(encoding="utf-8")
    violations = (ROOT / "services" / "governance" / "violations.py").read_text(encoding="utf-8")
    users_notice = users.split("def _send_member_governance_notice", 1)[1].split("def _send_admin_sanction_notice", 1)[0]
    economy_notice = economy.split("def notify_member_points_action", 1)[1].split("@app.route(\"/api/points/wallet\"", 1)[0]

    assert "def _send_member_governance_notice" in users
    assert "governance_notice_needed = True" in users
    assert 'action_label=f"違規點數 +{points}"' in users
    assert 'action_label=f"角色 {from_role} -> {to_role}"' in users
    assert "def notify_member_points_action" in economy
    assert "會員點數權益變更" in economy
    assert "blockchain_permission_model" in economy
    assert "私有鏈模式已停用手動加減積分" in economy
    assert "私有鏈模式不允許 root 直接處分用戶錢包" in economy
    assert "points_ledger_uuid" in notices
    assert "points_service.compensate_ledger" in appeals
    assert 'link="/appeals"' in notices
    assert "你可以到「申覆」分頁提出申覆" in notices
    assert "if not appealable:" in notices
    assert "violation_id = cur.lastrowid" in violations
    assert "return_violation_id=True" not in users_notice
    assert "return_violation_id=True" not in economy_notice
    assert "violation_id=None" in notices or "violation_id is not None" in notices
    assert "def _latest_violation_id" not in users
    assert "SELECT id FROM secure_violations WHERE user_id=? ORDER BY id DESC LIMIT 1" not in economy
    assert "violation_id < 0" in appeals
    assert "LEFT JOIN admin_sanction_appeal_contexts asc2" not in appeals
    assert "LEFT JOIN admin_sanction_appeal_contexts asc2" not in violations
    assert "用戶授權交易觸發" in economy
    assert "appealable=False" in users


def test_pending_reward_review_enforces_maker_checker():
    points_chain = (ROOT / "services" / "points_chain" / "service.py").read_text(encoding="utf-8")
    review = points_chain.split("def review_pending_reward", 1)[1].split("def rollback_ledger", 1)[0]

    assert "blockchain_permission_model" in review
    assert "pending reward review is disabled" in review


def test_comfyui_image_refs_are_owner_bound():
    comfyui = (
        (ROOT / "routes" / "comfyui.py").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "routes" / "comfyui_sections" / "image_routes.py").read_text(encoding="utf-8")
    )

    assert "CREATE TABLE IF NOT EXISTS comfyui_image_refs" in comfyui
    assert "_register_comfyui_image_refs" in comfyui
    assert "_verify_comfyui_image_ref_owner" in comfyui
    assert "COMFYUI_IMAGE_REF_DENIED" in comfyui


def test_pending_reward_review_enforces_maker_checker():
    points_chain = (ROOT / "services" / "points_chain" / "service.py").read_text(encoding="utf-8")
    review = points_chain.split("def review_pending_reward", 1)[1].split("def rollback_ledger", 1)[0]

    assert "blockchain_permission_model" in review
    assert "pending reward review is disabled" in review


def test_comfyui_image_refs_are_owner_bound():
    comfyui = (
        (ROOT / "routes" / "comfyui.py").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "routes" / "comfyui_sections" / "image_routes.py").read_text(encoding="utf-8")
    )

    assert "CREATE TABLE IF NOT EXISTS comfyui_image_refs" in comfyui
    assert "_register_comfyui_image_refs" in comfyui
    assert "_verify_comfyui_image_ref_owner" in comfyui
    assert "COMFYUI_IMAGE_REF_DENIED" in comfyui


def test_appeal_approval_rolls_back_points_before_committing_review():
    appeals = (ROOT / "routes" / "appeals.py").read_text(encoding="utf-8")
    review = appeals.split("def admin_violation_appeal_review", 1)[1].split("def ", 1)[0]

    assert "申覆點數補償交易失敗，申覆狀態尚未變更，請修復後重試" in review
    assert review.index("points_service.compensate_ledger") < review.index("UPDATE violation_appeals SET status=?")
    assert review.index("points_service.compensate_ledger") < review.index("conn.commit()")


def test_album_share_links_revoked_and_deleted_albums_not_resolved():
    storage_albums = (ROOT / "services" / "storage" / "storage_albums.py").read_text(encoding="utf-8")
    files = (ROOT / "routes" / "files.py").read_text(encoding="utf-8")
    revoke = storage_albums.split("def revoke_album_share_links", 1)[1].split("def _is_album_media_storage_row", 1)[0]
    resolver = storage_albums.split("def resolve_album_share_token", 1)[1].split("def mark_album_share_link_accessed", 1)[0]

    assert 'album["deleted_at"]' not in revoke
    assert "UPDATE album_share_links SET revoked_at=?" in revoke
    assert "a.deleted_at IS NULL" in resolver
    assert "def _html_safe_json" in files
    assert "safe_token = _html_safe_json(token)" in files
    assert 'safe_token = json.dumps(str(token or ""))' not in files


def test_manual_points_adjustment_is_root_only():
    economy = (ROOT / "routes" / "economy.py").read_text(encoding="utf-8")
    adjust_route = economy.split("def admin_points_adjust():", 1)[1].split(
        '@app.route("/api/root/points/official-wallet/grant"',
        1,
    )[0]

    assert '"code": "blockchain_permission_model"' in adjust_route
    assert "官方撥款需改走治理提案與官方多簽" in adjust_route
    assert "points_service.record_transaction" not in adjust_route
    assert "actor, err = manager_or_403()" not in adjust_route


def test_storage_upgrade_purchase_rechecks_capacity_after_points_spend():
    files = (ROOT / "routes" / "files.py").read_text(encoding="utf-8")

    assert '_refund_storage_upgrade_spend' in files
    assert 'conn.execute("BEGIN IMMEDIATE")' in files
    assert "storage allocation failed after debit" in files
    assert files.count("can_allocate_storage_bytes(conn, storage_root, additional_bytes)") >= 2
    assert "會員承諾容量已達或超過 Host 可用容量，目前停用容量購買" in files
    assert '"host_storage_total_commitment_exceeds_available" in set(capacity_audit.get("reasons") or [])' in files
    assert '"host_storage_overcommitted" in set(capacity_audit.get("reasons") or [])' in files


def test_upload_records_do_not_store_client_controlled_public_mime():
    upload_security = (ROOT / "services" / "security" / "upload_security.py").read_text(encoding="utf-8")

    assert "def safe_public_mime_type(" in upload_security
    assert "UNSAFE_PUBLIC_MIME_TYPES" in upload_security
    assert "safe_public_mime_type(original_filename, mime_type)" in upload_security
    assert "None if is_e2ee else (mime_type or None)" not in upload_security


def test_secure_cookie_defaults_are_secure():
    server = (ROOT / "server.py").read_text(encoding="utf-8")

    assert 'FORCE_HTTPS = _env_bool("FORCE_HTTPS", default=True)' in server
    assert 'SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", default=True)' in server


def test_flask_base_security_guardrails_are_configured():
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    request_guards = (ROOT / "services" / "server" / "request_guards.py").read_text(encoding="utf-8")

    assert 'MAX_FORM_MEMORY_KB = _env_int("HTML_LEARNING_MAX_FORM_MEMORY_KB", 512, minimum=64)' in server
    assert 'MAX_FORM_PARTS = _env_int("HTML_LEARNING_MAX_FORM_PARTS", 1000, minimum=100)' in server
    assert 'app.config["MAX_FORM_MEMORY_SIZE"] = MAX_FORM_MEMORY_KB * 1024' in server
    assert 'app.config["MAX_FORM_PARTS"] = MAX_FORM_PARTS' in server
    assert 'app.config["TRUSTED_HOSTS"] = TRUSTED_HOSTS' in server
    assert "SecurityError" in server
    assert '"HTML_LEARNING_PUBLIC_HOSTS"' in server
    assert '"HTML_LEARNING_PUBLIC_HOST"' in server
    assert '"HACKME_PUBLIC_HOSTS"' in server
    assert '"HACKME_PUBLIC_HOST"' in server
    assert '"HACKME_DEV_PUBLIC_HOST"' in server
    assert 'host_with_port = f"{host}:{port}"' in server
    assert 'TRUSTED_HOST_CHECKS_ENABLED = _db_bool_setting(' in server
    assert '"trusted_host_checks_enabled"' in server
    assert "if TRUSTED_HOSTS_DISABLED:" in server
    assert 'request_obj.headers.get("X-Maintenance-Bypass-Token", "")' in request_guards
    assert 'request_obj.args.get("maintenance_bypass_token"' not in request_guards


def test_rc1_multisig_scope_is_official_only_for_spending():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    economy_js = (ROOT / "public" / "js" / "55-economy.js").read_text(encoding="utf-8")
    service = (ROOT / "services" / "points_chain" / "service.py").read_text(encoding="utf-8")
    wallet_identity = (ROOT / "services" / "points_chain" / "wallet_identity.py").read_text(encoding="utf-8")
    routes = (ROOT / "routes" / "economy.py").read_text(encoding="utf-8")

    assert "economy-wallet-create-multisig-btn" not in index_html
    assert "createMultisigWallet" not in economy_js
    assert "official_treasury_signer_center" in service
    assert "/api/admin/points/governance/treasury-signer-center" in routes
    assert "一般用戶多簽目前僅支援收款/觀察，不支援轉出" in service
    assert '"user_multisig_preview"' in wallet_identity
    assert '"rc1_user_multisig": "receive_only"' in wallet_identity


def test_root_notifications_skip_normal_session_revocation_noise():
    events = (ROOT / "services" / "security" / "events.py").read_text(encoding="utf-8")

    assert "def _should_create_root_notification(event_type, detail=\"\"):" in events
    assert 'return detail_text not in {"idle_timeout", "single_session_logout", "user_sessions_revoked"}' in events


def test_frontend_boot_does_not_probe_disabled_chat_feature():
    core = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    admin = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")

    assert 'if (tabModuleChat) tabModuleChat.style.display = canAccessModule("chat") ? "" : "none";' in core
    assert 'if (normTab === "chat" && canAccessChat && typeof loadChatRooms === "function") {' in admin
    assert '    loadChatRooms();' in admin


def test_frontend_boot_does_not_eager_load_economy_before_module_selection():
    core = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    boot_body = core.split('if (currentRole === "manager" || currentRole === "super_admin") {', 1)[1].split("switchModuleTab(initialModule);", 1)[0]

    assert "loadEconomyDashboard();" not in boot_body
    assert "loadChatRooms();" not in boot_body


def test_trading_stress_pentest_covers_margin_risk_controls():
    script = (ROOT / "scripts" / "security" / "pentest" / "trading_stress_pentest.py").read_text(encoding="utf-8")

    assert "functional_correctness" in script
    assert "abnormal_operations" in script
    assert "security_pentest" in script
    assert "traceback_leaked" in script
    assert "error_response_not_json" in script
    assert "margin long rejects below initial margin" in script
    assert "short selling rejects below initial margin" in script
    assert "margin risk exposes initial and maintenance margin" in script
    assert "margin_add_collateral" in script
    assert "initial_margin_points" in script
    assert "maintenance_margin_points" in script


def test_latest_password_lookup_uses_monotonic_id_not_timestamp_text_order():
    bootstrap = (ROOT / "services" / "platform" / "bootstrap.py").read_text(encoding="utf-8")
    public = (ROOT / "routes" / "public.py").read_text(encoding="utf-8")
    users = (ROOT / "routes" / "users.py").read_text(encoding="utf-8")

    combined = "\n".join([bootstrap, public, users])
    assert "FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT 1" in combined
    assert "FROM user_passwords WHERE user_id=? ORDER BY created_at DESC LIMIT 1" not in combined
    assert "FROM user_passwords WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 1" not in combined


def test_trading_bot_tables_are_snapshot_scoped():
    snapshots = (ROOT / "services" / "snapshots" / "schema.py").read_text(encoding="utf-8")
    trading = (ROOT / "services" / "trading" / "trading_engine.py").read_text(encoding="utf-8")

    assert '"trading_bots"' in snapshots
    assert '"trading_bot_runs"' in snapshots
    assert "CREATE TABLE IF NOT EXISTS trading_bots" in trading
    assert "CREATE TABLE IF NOT EXISTS trading_bot_runs" in trading
    assert "bot_type TEXT NOT NULL DEFAULT 'conditional'" in trading
    assert "budget_points INTEGER NOT NULL DEFAULT 0" in trading
    assert "def backtest_trading_bot" in trading


def test_table_columns_rejects_unsafe_identifiers(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "safe.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    assert table_columns(conn, "users") == {"id", "username"}
    with pytest.raises(ValueError, match="unsafe SQLite identifier"):
        table_columns(conn, 'users); SELECT * FROM sqlite_master;--')


def test_trading_write_guard_does_not_full_replay_on_every_write():
    trading_engine = (ROOT / "services" / "trading" / "trading_engine.py").read_text(encoding="utf-8")
    guard = trading_engine.split("def _assert_writable", 1)[1].split("def _market", 1)[0]

    assert "_verify_state_on_conn" not in guard
    assert "trading.enabled" in guard


def test_trading_market_update_remains_available_in_safe_mode():
    trading_engine = (ROOT / "services" / "trading" / "trading_engine.py").read_text(encoding="utf-8")
    update_market = trading_engine.split("def update_market(", 1)[1].split("def allocate_reserve", 1)[0]

    assert "self._assert_writable(conn)" not in update_market
    assert "TRADING_MARKET_UPDATED" in update_market


def test_trading_fill_ledger_verification_uses_batch_lookup():
    trading_engine = (ROOT / "services" / "trading" / "trading_engine.py").read_text(encoding="utf-8")
    verifier = trading_engine.split("def _verify_fill_ledgers", 1)[1].split("def _verify_open_order_locks", 1)[0]

    assert "ledger_by_uuid" in verifier
    assert "self._ledger_row" not in verifier


def test_root_margin_trading_uses_simulated_funds_not_pointschain():
    trading_engine = (ROOT / "services" / "trading" / "trading_engine.py").read_text(encoding="utf-8")
    trading_margin = (ROOT / "services" / "trading" / "margin.py").read_text(encoding="utf-8")
    open_margin = trading_margin.split("def open_margin_position", 1)[1].split("def add_margin_collateral", 1)[0]
    close_margin = trading_engine.split("def close_margin_position", 1)[1].split("def scan_margin_liquidations", 1)[0]
    sim_verify = trading_engine.split("def _verify_sim_accounts", 1)[1].split("def _verify_margin_position_locks", 1)[0]
    margin_verify = trading_engine.split("def _verify_margin_position_locks", 1)[1].split("def _verify_spot_realized_pnl", 1)[0]

    assert "is_root_simulated = service._is_root_actor(actor)" in open_margin
    assert "service._sim_delta(conn, user_id, balance_delta=-collateral, locked_delta=collateral)" in open_margin
    assert "fee_micro = fee_micropoints(notional, float(market[\"fee_rate_percent\"] or 0))" in open_margin
    assert "fee = 0" in open_margin
    assert '"funding_mode": "root_simulated"' in open_margin
    assert "close_margin_position_helper(" in close_margin
    assert "is_root_simulated = service._is_root_user_id(conn, user_id)" in trading_margin
    assert "simulated_return = max(0, collateral + delta)" in trading_margin
    assert "service._sim_delta(" in trading_margin
    assert "balance_delta=simulated_return, locked_delta=-collateral" in trading_margin
    assert "TRADING_ROOT_SIM_MARGIN_BAD_DEBT" in trading_margin
    assert "FROM trading_margin_positions p" in sim_verify
    assert "u.username='root'" in sim_verify
    assert "is_root_simulated = user_id in root_user_ids" in margin_verify
    assert 'expected = 0 if is_root_simulated else (int(position["collateral_chain_points"] or 0)' in margin_verify


def test_trading_margin_errors_are_user_readable():
    trading_routes = (ROOT / "routes" / "trading.py").read_text(encoding="utf-8")
    service_error = trading_routes.split("def service_error", 1)[1].split("def price_to_points", 1)[0]

    assert "保證金不足，至少需要" in service_error
    assert "root 模擬交易資金不足" in service_error
    assert "進階交易尚未啟用" in service_error


def test_margin_collateral_and_account_maintenance_are_supported():
    trading_engine = (ROOT / "services" / "trading" / "trading_engine.py").read_text(encoding="utf-8")
    trading_margin = (ROOT / "services" / "trading" / "margin.py").read_text(encoding="utf-8")
    trading_routes = (ROOT / "routes" / "trading.py").read_text(encoding="utf-8")
    dashboard = trading_engine.split("def user_dashboard", 1)[1].split("def _is_executable", 1)[0]

    assert "maintenance_ratio_percent" in trading_margin
    assert "liquidation_price_points" in trading_margin
    assert "unrealized_pnl_points" in trading_margin
    assert "margin_long_financing_percent" in trading_engine
    assert "short_collateral_percent" in trading_engine
    assert "def _minimum_margin_collateral_points" in trading_engine
    assert "risk_reason" in trading_margin
    assert "借券放空在價格上漲時會虧損" in trading_margin
    assert "def add_margin_collateral" in trading_margin
    assert "TRADING_MARGIN_COLLATERAL_ADDED" in trading_margin
    assert '"margin_summary": self._margin_summary_payload(conn, user_id, margin_positions)' in dashboard
    assert '@app.route("/api/trading/margin/<position_uuid>/collateral", methods=["POST"])' in trading_routes
    assert "def _margin_account_payload" in trading_engine
    assert "cross_margin_ratio_percent" in trading_margin
    assert "auto_transfer_rule" in trading_margin
