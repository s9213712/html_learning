import sqlite3
from pathlib import Path

from services.platform import bootstrap


def _get_db_factory(db_path):
    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return get_db


def _ensure_session_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "is_revoked" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN is_revoked INTEGER NOT NULL DEFAULT 0")
    if "revoked_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN revoked_at TEXT")
    if "last_seen" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_seen TEXT")
    if "device_info" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN device_info TEXT")
    if "ip_country" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN ip_country TEXT")
    conn.execute("UPDATE sessions SET is_revoked=0 WHERE is_revoked IS NULL")
    conn.execute("UPDATE sessions SET last_seen=created_at WHERE last_seen IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_revoked ON sessions(is_revoked)")


def _noop(*args, **kwargs):
    return None


def test_init_db_repairs_legacy_sessions_before_schema_replay(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        );
        INSERT INTO schema_migrations (version, name, applied_at) VALUES
            (1, 'bootstrap schema_migrations metadata table', '2026-01-01T00:00:00'),
            (2, 'ensure legacy-compatible users columns', '2026-01-01T00:00:00'),
            (3, 'ensure violation_appeals columns', '2026-01-01T00:00:00'),
            (4, 'ensure system_settings baseline rows', '2026-01-01T00:00:00');

        CREATE TABLE sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            token_hash   TEXT    NOT NULL UNIQUE,
            ip_address   TEXT,
            user_agent   TEXT,
            expires_at   TEXT    NOT NULL,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE chat_rooms (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT    NOT NULL UNIQUE,
            owner_user_id  INTEGER,
            is_active      INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()

    schema_path = Path(__file__).resolve().parents[2] / "bootstrap.schema.sql"
    missing_json = str(tmp_path / "missing.json")
    original_state = dict(bootstrap._STATE)

    monkeypatch.setenv("HTML_LEARNING_ROOT_PASSWORD", "root")

    try:
        bootstrap.configure_bootstrap_service(
            get_db=_get_db_factory(str(db_path)),
            db_path=str(db_path),
            schema_path=str(schema_path),
            legacy_fail_log=missing_json,
            legacy_blocked_ips=missing_json,
            legacy_rate_limit=missing_json,
            legacy_audit_log=missing_json,
            chain_seed="seed",
            chain_hash=lambda prev_hash, entry_json: f"{prev_hash}:{len(entry_json)}",
            load_json=lambda path: {},
            normalize_text=lambda value: value if isinstance(value, str) else "",
            hash_password=lambda value: f"hash:{value}",
            audit=_noop,
            refresh_system_settings=_noop,
            init_system_settings_table=_noop,
            seed_missing_settings=_noop,
            import_legacy_settings_files=_noop,
            default_settings={},
        )
        bootstrap.init_db(
            ensure_secure_audit_columns=_noop,
            ensure_user_columns=_noop,
            ensure_appeal_columns=_noop,
            ensure_session_columns=_ensure_session_columns,
            ensure_security_support_schema=_noop,
            ensure_official_chat_room=_noop,
            hash_password=lambda value: f"hash:{value}",
        )
    finally:
        bootstrap._STATE.clear()
        bootstrap._STATE.update(original_state)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    session_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    chat_room_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chat_rooms)").fetchall()}
    chat_message_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
    friend_cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_friends)").fetchall()}
    chat_invite_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chat_room_invites)").fetchall()}
    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    login_location_cols = {row["name"] for row in conn.execute("PRAGMA table_info(login_locations)").fetchall()}
    member_rule_cols = {row["name"] for row in conn.execute("PRAGMA table_info(member_level_rules)").fetchall()}
    member_audit_cols = {row["name"] for row in conn.execute("PRAGMA table_info(member_level_audit)").fetchall()}
    proposal_cols = {row["name"] for row in conn.execute("PRAGMA table_info(moderation_proposals)").fetchall()}
    vote_cols = {row["name"] for row in conn.execute("PRAGMA table_info(moderation_votes)").fetchall()}
    moderation_action_cols = {row["name"] for row in conn.execute("PRAGMA table_info(moderation_actions)").fetchall()}
    mod_note_cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_mod_notes)").fetchall()}
    reputation_event_cols = {row["name"] for row in conn.execute("PRAGMA table_info(reputation_events)").fetchall()}
    snapshot_cols = {row["name"] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
    restore_event_cols = {row["name"] for row in conn.execute("PRAGMA table_info(snapshot_restore_events)").fetchall()}
    server_mode_cols = {row["name"] for row in conn.execute("PRAGMA table_info(server_modes)").fetchall()}
    uploaded_file_cols = {row["name"] for row in conn.execute("PRAGMA table_info(uploaded_files)").fetchall()}
    encrypted_key_cols = {row["name"] for row in conn.execute("PRAGMA table_info(encrypted_file_keys)").fetchall()}
    scan_result_cols = {row["name"] for row in conn.execute("PRAGMA table_info(file_scan_results)").fetchall()}
    access_log_cols = {row["name"] for row in conn.execute("PRAGMA table_info(file_access_logs)").fetchall()}
    cloud_policy_cols = {row["name"] for row in conn.execute("PRAGMA table_info(cloud_drive_security_policies)").fetchall()}
    user_storage_cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_storage)").fetchall()}
    storage_file_cols = {row["name"] for row in conn.execute("PRAGMA table_info(storage_files)").fetchall()}
    storage_quota_log_cols = {row["name"] for row in conn.execute("PRAGMA table_info(storage_quota_log)").fetchall()}
    storage_share_link_cols = {row["name"] for row in conn.execute("PRAGMA table_info(storage_share_links)").fetchall()}
    album_cols = {row["name"] for row in conn.execute("PRAGMA table_info(albums)").fetchall()}
    album_file_cols = {row["name"] for row in conn.execute("PRAGMA table_info(album_files)").fetchall()}
    cloud_ref_cols = {row["name"] for row in conn.execute("PRAGMA table_info(cloud_file_refs)").fetchall()}
    grant_cols = {row["name"] for row in conn.execute("PRAGMA table_info(file_access_grants)").fetchall()}
    announcement_request_cols = {row["name"] for row in conn.execute("PRAGMA table_info(announcement_attachment_requests)").fetchall()}
    report_cols = {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}
    notification_cols = {row["name"] for row in conn.execute("PRAGMA table_info(notifications)").fetchall()}
    captcha_cols = {row["name"] for row in conn.execute("PRAGMA table_info(captcha_challenges)").fetchall()}
    integrity_finding_cols = {row["name"] for row in conn.execute("PRAGMA table_info(integrity_findings)").fetchall()}
    integrity_run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(integrity_scan_runs)").fetchall()}
    integrity_manifest_cols = {row["name"] for row in conn.execute("PRAGMA table_info(integrity_manifest_versions)").fetchall()}
    points_wallet_cols = {row["name"] for row in conn.execute("PRAGMA table_info(points_wallets)").fetchall()}
    points_ledger_cols = {row["name"] for row in conn.execute("PRAGMA table_info(points_ledger)").fetchall()}
    points_block_cols = {row["name"] for row in conn.execute("PRAGMA table_info(points_chain_blocks)").fetchall()}
    game_match_cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_matches)").fetchall()}
    game_invite_cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_invites)").fetchall()}
    game_reward_cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_leaderboard_rewards)").fetchall()}
    trading_market_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_markets)").fetchall()}
    trading_market_registry_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_markets_registry)").fetchall()}
    trading_provider_mapping_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_market_provider_mappings)").fetchall()}
    trading_registry_audit_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_market_registry_audit)").fetchall()}
    trading_order_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_orders)").fetchall()}
    trading_fill_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_fills)").fetchall()}
    trading_position_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_spot_positions)").fetchall()}
    trading_margin_position_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_margin_positions)").fetchall()}
    trading_reserve_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trading_reserve_pool)").fetchall()}
    comfyui_image_ref_cols = {row["name"] for row in conn.execute("PRAGMA table_info(comfyui_image_refs)").fetchall()}
    comfyui_generation_history_cols = {row["name"] for row in conn.execute("PRAGMA table_info(comfyui_generation_history)").fetchall()}
    comfyui_workflow_preset_cols = {row["name"] for row in conn.execute("PRAGMA table_info(comfyui_workflow_presets)").fetchall()}
    comfyui_workflow_run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(comfyui_workflow_runs)").fetchall()}
    migration_versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
    default_users = {
        row["username"]: dict(row)
        for row in conn.execute(
            """
            SELECT u.username, u.role, u.must_change_password, u.is_default_password,
                   u.member_level, u.base_level, u.effective_level, p.password_hash
            FROM users u
            JOIN user_passwords p ON p.user_id = u.id
            WHERE u.username IN ('root', 'admin', 'test')
            """
        ).fetchall()
    }
    conn.close()

    assert {"is_revoked", "revoked_at", "last_seen", "device_info", "ip_country"} <= session_cols
    assert {"is_private", "join_password_hash", "join_password_required"} <= chat_room_cols
    assert {"message_type", "sticker_key", "is_revoked", "revoked_at", "revoked_by"} <= chat_message_cols
    assert {"user_id", "friend_user_id", "status", "requested_by", "updated_at"} <= friend_cols
    assert {"room_id", "inviter_user_id", "invitee_user_id", "status", "updated_at"} <= chat_invite_cols
    assert {
        "member_level", "base_level", "effective_level", "trust_score", "points", "reputation",
        "violation_score", "sanction_status", "sanction_until", "level_updated_at",
        "level_updated_by", "level_update_reason", "locked_until", "password_strength_score",
        "must_change_password", "is_default_password", "deleted_at",
        "avatar_file_id", "avatar_crop_json",
    } <= user_cols
    assert {"ip_hash", "login_at", "is_suspicious"} <= login_location_cols
    assert {
        "level", "can_post", "can_comment", "can_report", "daily_post_limit",
        "post_rate_limit_per_hour", "attachment_quota_mb", "report_weight",
        "downgrade_violation_threshold", "session_idle_timeout_minutes", "require_admin_approval",
    } <= member_rule_cols
    assert {"actor", "target_user", "old_base_level", "new_effective_level", "reason", "source"} <= member_audit_cols
    assert {
        "target_user_id", "action_type", "status", "required_votes", "approve_count",
        "risk_level", "required_root_approval", "required_manager_approvals",
    } <= proposal_cols
    assert {"proposal_id", "voter_user_id", "vote"} <= vote_cols
    assert {"moderator_id", "action_type", "target_type", "target_id"} <= moderation_action_cols
    assert {"moderator_id", "user_id", "note"} <= mod_note_cols
    assert {"user_id", "delta", "reason", "source_user_id"} <= reputation_event_cols
    assert {"id", "type", "status", "storage_path", "checksum"} <= snapshot_cols
    assert {"id", "snapshot_id", "restore_mode", "pre_restore_snapshot_id"} <= restore_event_cols
    assert {"current_mode", "previous_mode", "active_snapshot_id"} <= server_mode_cols
    assert {"privacy_mode", "risk_level", "scan_status", "client_scan_report_json"} <= uploaded_file_cols
    assert {"file_id", "recipient_user_id", "encrypted_file_key", "revoked_at"} <= encrypted_key_cols
    assert {"scanner_name", "result", "details_json"} <= scan_result_cols
    assert {"file_id", "actor_user_id", "action", "result"} <= access_log_cols
    assert {
        "scope", "block_unclean_downloads", "max_archive_files", "max_daily_downloads",
        "deep_archive_scan_enabled", "max_archive_depth", "office_macro_scan_enabled",
        "image_reencode_enabled", "image_reencode_max_pixels", "yara_enabled", "yara_command", "yara_rules_path",
    } <= cloud_policy_cols
    assert {"user_id", "quota_bytes", "used_bytes", "reserved_bytes", "file_count"} <= user_storage_cols
    assert {"file_id", "owner_user_id", "virtual_path", "is_trashed", "deleted_at"} <= storage_file_cols
    assert {"user_id", "delta_bytes", "before_used_bytes", "after_used_bytes", "source"} <= storage_quota_log_cols
    assert {
        "storage_file_id",
        "file_id",
        "token",
        "token_hash",
        "access_scope",
        "required_user_id",
        "max_views",
        "wrapped_file_key_envelope",
        "expires_at",
        "revoked_at",
        "access_count",
    } <= storage_share_link_cols
    assert {"owner_user_id", "title", "visibility", "cover_file_id", "deleted_at"} <= album_cols
    assert {"album_id", "storage_file_id", "file_id", "sort_order", "caption"} <= album_file_cols
    assert {"file_id", "context_type", "context_id", "permission_snapshot_json"} <= cloud_ref_cols
    assert {"file_id", "granted_to_user_id", "context_type", "can_download", "revoked_at"} <= grant_cols
    assert {"file_id", "requested_by", "announcement_id", "status", "reviewed_by"} <= announcement_request_cols
    assert {"target_type", "reporter_user_id", "reported_user_id", "status", "claimed_by_user_id"} <= report_cols
    assert {"user_id", "type", "title", "body", "is_read", "read_at"} <= notification_cols
    assert {"id", "mode", "answer_hash", "expires_at", "used_at"} <= captcha_cols
    assert {"file_path", "old_hash", "new_hash", "change_type", "status", "reviewed_by"} <= integrity_finding_cols
    assert {"started_at", "finished_at", "files_checked", "manifest_signature_valid"} <= integrity_run_cols
    assert {"manifest_hash", "manifest_signature", "approved_by"} <= integrity_manifest_cols
    assert {"user_id", "soft_balance", "hard_balance", "soft_frozen", "hard_frozen", "wallet_status"} <= points_wallet_cols
    assert {"ledger_uuid", "public_account_id", "currency_type", "direction", "amount", "ledger_hash", "previous_ledger_hash"} <= points_ledger_cols
    assert {"block_number", "previous_block_hash", "merkle_root", "block_hash", "first_ledger_id", "last_ledger_id"} <= points_block_cols
    assert {"game_key", "mode", "white_user_id", "black_user_id", "current_turn", "board_json", "winner_user_id", "white_deleted_at", "black_deleted_at"} <= game_match_cols
    assert {"game_key", "inviter_user_id", "opponent_user_id", "status", "match_id"} <= game_invite_cols
    assert {"game_key", "week_key", "user_id", "rank", "score", "reward_points", "ledger_uuid"} <= game_reward_cols
    assert {
        "symbol", "manual_price_points", "futures_enabled", "pvp_matching_enabled", "price_source",
        "display_quote_currency", "display_name", "market_type", "sort_order",
        "allow_margin", "allow_bots", "allow_risk_grade_usage",
        "price_precision", "quantity_precision", "min_order_size", "max_order_size",
        "lot_size", "tick_size", "live_price_enabled", "reference_price_enabled",
        "btc_trade_enabled", "provider_ids_json",
    } <= trading_market_cols
    assert {
        "symbol", "base_asset", "quote_asset", "display_name", "display_quote_currency",
        "enabled", "allow_spot", "allow_margin", "allow_bots", "allow_risk_grade_usage",
        "price_precision", "quantity_precision", "min_order_size", "max_order_size",
        "lot_size", "tick_size", "probe_status", "probe_summary_json", "created_by", "updated_by",
        "registry_source", "seed_version",
    } <= trading_market_registry_cols
    assert {
        "market_id", "provider", "provider_symbol", "supports_ticker", "supports_depth",
        "supports_candles", "enabled", "priority",
    } <= trading_provider_mapping_cols
    assert {"actor_id", "action", "market_symbol", "before_json", "after_json", "created_at"} <= trading_registry_audit_cols
    assert {"order_uuid", "user_id", "market_symbol", "side", "order_type", "status", "frozen_points"} <= trading_order_cols
    assert {"fill_uuid", "order_id", "notional_points", "fee_points", "points_ledger_uuids_json"} <= trading_fill_cols
    assert {"user_id", "market_symbol", "quantity_units", "locked_quantity_units"} <= trading_position_cols
    assert {"position_uuid", "position_type", "principal_points", "collateral_points", "interest_percent_daily"} <= trading_margin_position_cols
    assert {"id", "balance_points", "updated_at"} <= trading_reserve_cols
    assert {"ref_key", "owner_user_id", "prompt_id", "backend_url", "image_ref_json", "created_at", "last_used_at"} <= comfyui_image_ref_cols
    assert {"owner_user_id", "backend_url", "generation_mode", "payload_json", "input_assets_json", "controlnet_json", "result_json", "created_at", "updated_at"} <= comfyui_generation_history_cols
    assert {
        "owner_user_id", "title", "description", "visibility", "is_official", "workflow_json",
        "workflow_hash", "required_models_json", "required_loras_json", "required_controlnets_json",
        "default_params_json", "published_by_user_id", "published_at", "created_at", "updated_at",
        "purpose", "layout_json", "workflow_schema_version", "required_custom_nodes_json", "is_default",
    } <= comfyui_workflow_preset_cols
    assert {
        "preset_id", "actor_user_id", "prompt", "negative_prompt", "params_json", "workflow_json",
        "output_refs_json", "status", "error", "created_at", "updated_at",
    } <= comfyui_workflow_run_cols
    assert migration_versions == list(range(1, 32))
    assert set(default_users) == {"root", "admin", "test"}

    assert default_users["root"]["role"] == "super_admin"
    assert default_users["root"]["password_hash"] == "hash:root"
    assert default_users["root"]["must_change_password"] == 1
    assert default_users["root"]["is_default_password"] == 1
    assert default_users["root"]["member_level"] == "normal"
    assert default_users["root"]["base_level"] == "normal"
    assert default_users["root"]["effective_level"] == "normal"

    assert default_users["admin"]["role"] == "manager"
    assert default_users["admin"]["password_hash"] == "hash:admin"
    assert default_users["admin"]["must_change_password"] == 1
    assert default_users["admin"]["is_default_password"] == 1
    assert default_users["admin"]["member_level"] == "normal"
    assert default_users["admin"]["base_level"] == "normal"
    assert default_users["admin"]["effective_level"] == "normal"

    assert default_users["test"]["role"] == "user"
    assert default_users["test"]["password_hash"] == "hash:test"
    assert default_users["test"]["must_change_password"] == 1
    assert default_users["test"]["is_default_password"] == 1
    assert default_users["test"]["member_level"] == "trusted"
    assert default_users["test"]["base_level"] == "trusted"
    assert default_users["test"]["effective_level"] == "trusted"


def test_init_db_allows_existing_root_password_without_bootstrap_env(tmp_path, monkeypatch):
    db_path = tmp_path / "existing-root.db"
    schema_path = Path(__file__).resolve().parents[2] / "bootstrap.schema.sql"

    conn = sqlite3.connect(db_path)
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    now = "2026-01-01T00:00:00"
    cur = conn.execute(
        "INSERT INTO users (username, status, role, member_level, base_level, effective_level, created_at, updated_at) VALUES (?, 'active', 'super_admin', 'vip', 'vip', 'vip', ?, ?)",
        ("root", now, now),
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
        (cur.lastrowid, "hash:root", now),
    )
    conn.commit()
    conn.close()

    missing_json = str(tmp_path / "missing.json")
    original_state = dict(bootstrap._STATE)
    monkeypatch.delenv("HTML_LEARNING_ROOT_PASSWORD", raising=False)

    try:
        bootstrap.configure_bootstrap_service(
            get_db=_get_db_factory(str(db_path)),
            db_path=str(db_path),
            schema_path=str(schema_path),
            legacy_fail_log=missing_json,
            legacy_blocked_ips=missing_json,
            legacy_rate_limit=missing_json,
            legacy_audit_log=missing_json,
            chain_seed="seed",
            chain_hash=lambda prev_hash, entry_json: f"{prev_hash}:{len(entry_json)}",
            load_json=lambda path: {},
            normalize_text=lambda value: value if isinstance(value, str) else "",
            hash_password=lambda value: f"hash:{value}",
            audit=_noop,
            refresh_system_settings=_noop,
            init_system_settings_table=_noop,
            seed_missing_settings=_noop,
            import_legacy_settings_files=_noop,
            default_settings={},
        )
        bootstrap.init_db(
            ensure_secure_audit_columns=_noop,
            ensure_user_columns=_noop,
            ensure_appeal_columns=_noop,
            ensure_session_columns=_ensure_session_columns,
            ensure_security_support_schema=_noop,
            ensure_official_chat_room=_noop,
            hash_password=lambda value: f"hash:{value}",
        )
    finally:
        bootstrap._STATE.clear()
        bootstrap._STATE.update(original_state)

    conn = sqlite3.connect(db_path)
    root_password_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM user_passwords p
        JOIN users u ON u.id = p.user_id
        WHERE u.username='root'
        """
    ).fetchone()[0]
    default_user_count = conn.execute("SELECT COUNT(*) FROM users WHERE username IN ('root', 'admin', 'test')").fetchone()[0]
    root = conn.execute("SELECT member_level, base_level, effective_level FROM users WHERE username='root'").fetchone()
    conn.close()
    assert root_password_count == 1
    assert default_user_count == 3
    assert tuple(root) == ("normal", "normal", "normal")


def test_init_db_marks_existing_default_account_when_env_password_still_matches(tmp_path, monkeypatch):
    db_path = tmp_path / "existing-default.db"
    schema_path = Path(__file__).resolve().parents[2] / "bootstrap.schema.sql"

    conn = sqlite3.connect(db_path)
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    now = "2026-01-01T00:00:00"
    cur = conn.execute(
        "INSERT INTO users (username, status, role, must_change_password, is_default_password, password_changed_at, created_at, updated_at) "
        "VALUES (?, 'active', 'super_admin', 0, 0, ?, ?, ?)",
        ("root", "2026-01-02T00:00:00", now, now),
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
        (cur.lastrowid, "hash:RootDefault123!", now),
    )
    conn.commit()
    conn.close()

    missing_json = str(tmp_path / "missing.json")
    original_state = dict(bootstrap._STATE)
    monkeypatch.setenv("HTML_LEARNING_ROOT_PASSWORD", "RootDefault123!")

    try:
        bootstrap.configure_bootstrap_service(
            get_db=_get_db_factory(str(db_path)),
            db_path=str(db_path),
            schema_path=str(schema_path),
            legacy_fail_log=missing_json,
            legacy_blocked_ips=missing_json,
            legacy_rate_limit=missing_json,
            legacy_audit_log=missing_json,
            chain_seed="seed",
            chain_hash=lambda prev_hash, entry_json: f"{prev_hash}:{len(entry_json)}",
            load_json=lambda path: {},
            normalize_text=lambda value: value if isinstance(value, str) else "",
            hash_password=lambda value: f"hash:{value}",
            verify_password=lambda hashed, raw: hashed == f"hash:{raw}",
            audit=_noop,
            refresh_system_settings=_noop,
            init_system_settings_table=_noop,
            seed_missing_settings=_noop,
            import_legacy_settings_files=_noop,
            default_settings={},
        )
        bootstrap.init_db(
            ensure_secure_audit_columns=_noop,
            ensure_user_columns=_noop,
            ensure_appeal_columns=_noop,
            ensure_session_columns=_ensure_session_columns,
            ensure_security_support_schema=_noop,
            ensure_official_chat_room=_noop,
            hash_password=lambda value: f"hash:{value}",
        )
    finally:
        bootstrap._STATE.clear()
        bootstrap._STATE.update(original_state)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT must_change_password, is_default_password FROM users WHERE username='root'").fetchone()
    conn.close()
    assert row == (1, 1)


def test_init_db_relaxes_default_password_gate_for_dev_default_accounts(tmp_path, monkeypatch):
    db_path = tmp_path / "existing-default-dev.db"
    schema_path = Path(__file__).resolve().parents[2] / "bootstrap.schema.sql"

    conn = sqlite3.connect(db_path)
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    now = "2026-01-01T00:00:00"
    cur = conn.execute(
        "INSERT INTO users (username, status, role, must_change_password, is_default_password, password_changed_at, created_at, updated_at) "
        "VALUES (?, 'active', 'super_admin', 1, 1, ?, ?, ?)",
        ("root", "2026-01-02T00:00:00", now, now),
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
        (cur.lastrowid, "hash:RootDefault123!", now),
    )
    conn.commit()
    conn.close()

    missing_json = str(tmp_path / "missing.json")
    original_state = dict(bootstrap._STATE)
    monkeypatch.setenv("HTML_LEARNING_ROOT_PASSWORD", "RootDefault123!")
    monkeypatch.setenv("HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS", "1")
    monkeypatch.setenv("HACKME_DEV_SECURITY_ENABLED", "0")
    monkeypatch.setenv("HACKME_DEV_SERVER_MODE", "dev_ready")

    try:
        bootstrap.configure_bootstrap_service(
            get_db=_get_db_factory(str(db_path)),
            db_path=str(db_path),
            schema_path=str(schema_path),
            legacy_fail_log=missing_json,
            legacy_blocked_ips=missing_json,
            legacy_rate_limit=missing_json,
            legacy_audit_log=missing_json,
            chain_seed="seed",
            chain_hash=lambda prev_hash, entry_json: f"{prev_hash}:{len(entry_json)}",
            load_json=lambda path: {},
            normalize_text=lambda value: value if isinstance(value, str) else "",
            hash_password=lambda value: f"hash:{value}",
            verify_password=lambda hashed, raw: hashed == f"hash:{raw}",
            audit=_noop,
            refresh_system_settings=_noop,
            init_system_settings_table=_noop,
            seed_missing_settings=_noop,
            import_legacy_settings_files=_noop,
            default_settings={},
        )
        bootstrap.init_db(
            ensure_secure_audit_columns=_noop,
            ensure_user_columns=_noop,
            ensure_appeal_columns=_noop,
            ensure_session_columns=_ensure_session_columns,
            ensure_security_support_schema=_noop,
            ensure_official_chat_room=_noop,
            hash_password=lambda value: f"hash:{value}",
        )
    finally:
        bootstrap._STATE.clear()
        bootstrap._STATE.update(original_state)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT must_change_password, is_default_password FROM users WHERE username='root'").fetchone()
    conn.close()
    assert row == (0, 0)
