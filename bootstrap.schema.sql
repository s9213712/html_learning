CREATE TABLE IF NOT EXISTS chat_message_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
 room_id INTEGER NOT NULL REFERENCES chat_rooms(id) ON DELETE CASCADE,
 reporter_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 reported_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 reason TEXT NOT NULL,
 content_snapshot TEXT,
 message_created_at TEXT,
 message_edited_at TEXT,
 status TEXT NOT NULL DEFAULT 'pending',
 reviewed_by TEXT,
 reviewed_at TEXT,
 review_note TEXT,
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 UNIQUE(message_id, reporter_user_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id        INTEGER NOT NULL REFERENCES chat_rooms(id) ON DELETE CASCADE,
            sender_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
            content        TEXT    NOT NULL,
            message_type   TEXT    NOT NULL DEFAULT 'text',
            sticker_key    TEXT,
            is_blocked     INTEGER NOT NULL DEFAULT 0,
            is_revoked     INTEGER NOT NULL DEFAULT 0,
            revoked_at     TEXT,
            revoked_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            edited_at      TEXT,
            edited_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            edit_count     INTEGER NOT NULL DEFAULT 0,
            original_content TEXT,
            blocked_reason TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );

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
        );

CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            display_name TEXT,
            bio TEXT,
            signature TEXT,
            location TEXT,
            website TEXT,
            friend_code TEXT,
            friend_code_rotated_at TEXT,
            custom_title TEXT,
            custom_title_status TEXT NOT NULL DEFAULT 'none',
            profile_visibility TEXT NOT NULL DEFAULT 'public',
            appearance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

CREATE TABLE IF NOT EXISTS chat_room_invites (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id         INTEGER NOT NULL REFERENCES chat_rooms(id) ON DELETE CASCADE,
            inviter_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            invitee_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status          TEXT    NOT NULL DEFAULT 'pending',
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(room_id, invitee_user_id),
            CHECK (status IN ('pending', 'accepted', 'rejected'))
        );

CREATE TABLE IF NOT EXISTS game_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_key TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'pvp',
    status TEXT NOT NULL DEFAULT 'active',
    white_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    black_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    human_side TEXT NOT NULL DEFAULT 'white',
    computer_difficulty TEXT NOT NULL DEFAULT 'normal',
    computer_config_json TEXT NOT NULL DEFAULT '{}',
    current_turn TEXT NOT NULL DEFAULT 'white',
    board_json TEXT NOT NULL,
    move_history_json TEXT NOT NULL DEFAULT '[]',
    winner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    result_reason TEXT,
    draw_offer_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    draw_offer_at TEXT,
    leaderboard_week TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    white_deleted_at TEXT,
    black_deleted_at TEXT,
    CHECK (game_key IN ('chess')),
    CHECK (mode IN ('pvp', 'computer')),
    CHECK (status IN ('active', 'finished', 'cancelled')),
    CHECK (current_turn IN ('white', 'black')),
    CHECK (human_side IN ('white', 'black')),
    CHECK (computer_difficulty IN ('easy', 'normal', 'hard', 'experiment', 'experiment 0:minimax2ply', 'experiment 1:search', 'experiment 2:nn', 'experiment 3:dl', 'experiment 4:pv', 'experiment 5:nnue', 'experiment 6:neuralnet', 'stockfish'))
);

CREATE TABLE IF NOT EXISTS game_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_key TEXT NOT NULL,
    inviter_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opponent_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    match_id INTEGER REFERENCES game_matches(id) ON DELETE SET NULL,
    message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    CHECK (game_key IN ('chess')),
    CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled', 'expired'))
);

CREATE TABLE IF NOT EXISTS game_leaderboard_rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_key TEXT NOT NULL,
    week_key TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    score INTEGER NOT NULL,
    reward_points INTEGER NOT NULL,
    ledger_uuid TEXT,
    awarded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(game_key, week_key, user_id)
);

CREATE TABLE IF NOT EXISTS chat_room_members (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id    INTEGER NOT NULL REFERENCES chat_rooms(id) ON DELETE CASCADE,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            anonymous_enabled INTEGER NOT NULL DEFAULT 0,
            joined_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(room_id, user_id)
        );

CREATE TABLE IF NOT EXISTS chat_rooms (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT    NOT NULL,
            owner_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            is_private     INTEGER NOT NULL DEFAULT 0,
            is_active      INTEGER NOT NULL DEFAULT 1,
            join_password_hash TEXT,
            join_password_required INTEGER NOT NULL DEFAULT 0,
            allow_anonymous INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );

CREATE TABLE IF NOT EXISTS csrf_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT    NOT NULL UNIQUE,
    username     TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS account_recovery_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose      TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    requested_ip TEXT,
    user_agent   TEXT,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    used_at      TEXT
);

CREATE TABLE IF NOT EXISTS mail_outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient  TEXT    NOT NULL,
    subject    TEXT    NOT NULL,
    body       TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'queued',
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS captcha_challenges (
    id           TEXT PRIMARY KEY,
    mode         TEXT NOT NULL,
    answer_hash  TEXT NOT NULL,
    ip_hash      TEXT,
    expires_at   TEXT NOT NULL,
    used_at      TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ip_blocks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address     TEXT    NOT NULL UNIQUE,
    blocked_until  TEXT    NOT NULL,
    reason         TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    ip_address   TEXT,
    user_agent   TEXT,
    success      INTEGER NOT NULL DEFAULT 0,
    attempted_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS login_locations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ip_hash       TEXT NOT NULL,
    country       TEXT,
    city          TEXT,
    login_at      TEXT NOT NULL,
    is_suspicious INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS member_level_rules (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    level                  TEXT NOT NULL UNIQUE,
    can_post               INTEGER NOT NULL DEFAULT 1,
    can_comment            INTEGER NOT NULL DEFAULT 1,
    can_send_dm            INTEGER NOT NULL DEFAULT 1,
    can_upload_attachment  INTEGER NOT NULL DEFAULT 0,
    can_report             INTEGER NOT NULL DEFAULT 1,
    daily_post_limit       INTEGER NOT NULL DEFAULT 10,
    daily_dm_limit         INTEGER NOT NULL DEFAULT 20,
    post_rate_limit_per_hour INTEGER NOT NULL DEFAULT 10,
    comment_rate_limit_per_hour INTEGER NOT NULL DEFAULT 40,
    dm_rate_limit_per_day  INTEGER NOT NULL DEFAULT 20,
    upload_rate_limit_per_day INTEGER NOT NULL DEFAULT 0,
    max_attachment_size_mb INTEGER NOT NULL DEFAULT 0,
    attachment_quota_mb    INTEGER NOT NULL DEFAULT 0,
    requires_moderation    INTEGER NOT NULL DEFAULT 0,
    report_weight          INTEGER NOT NULL DEFAULT 1,
    min_account_age_days   INTEGER NOT NULL DEFAULT 0,
    min_approved_content_count INTEGER NOT NULL DEFAULT 0,
    min_points             INTEGER NOT NULL DEFAULT 0,
    min_trust_score        INTEGER NOT NULL DEFAULT 0,
    min_reputation         INTEGER NOT NULL DEFAULT 0,
    max_violation_score    INTEGER NOT NULL DEFAULT 0,
    downgrade_violation_threshold INTEGER NOT NULL DEFAULT 0,
    session_idle_timeout_minutes INTEGER NOT NULL DEFAULT 3,
    require_admin_approval INTEGER NOT NULL DEFAULT 0,
    require_root_approval  INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_level_audit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    actor               TEXT NOT NULL,
    target_user         TEXT NOT NULL,
    old_base_level      TEXT,
    new_base_level      TEXT,
    old_effective_level TEXT,
    new_effective_level TEXT,
    reason              TEXT NOT NULL,
    source              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moderation_proposals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    target_user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type         TEXT NOT NULL,
    action_value        TEXT,
    proposed_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reason              TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    required_votes      INTEGER NOT NULL DEFAULT 2,
    risk_level          TEXT NOT NULL DEFAULT 'normal',
    required_root_approval INTEGER NOT NULL DEFAULT 0,
    required_manager_approvals INTEGER NOT NULL DEFAULT 1,
    approve_count       INTEGER NOT NULL DEFAULT 0,
    reject_count        INTEGER NOT NULL DEFAULT 0,
    expires_at          TEXT NOT NULL,
    executed_at         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moderation_votes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id    INTEGER NOT NULL REFERENCES moderation_proposals(id) ON DELETE CASCADE,
    voter_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vote           TEXT NOT NULL,
    comment        TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE(proposal_id, voter_user_id)
);

CREATE TABLE IF NOT EXISTS moderation_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    moderator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type  TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    INTEGER NOT NULL,
    reason       TEXT,
    is_auto      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_mod_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    moderator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    note         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reputation_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta          INTEGER NOT NULL,
    reason         TEXT NOT NULL,
    source_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    source_post_id INTEGER,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                  TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    status              TEXT NOT NULL,
    created_by          INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    completed_at        TEXT,
    app_version         TEXT,
    schema_version      TEXT,
    source_mode         TEXT,
    includes_json       TEXT NOT NULL,
    storage_path        TEXT NOT NULL,
    db_dump_path        TEXT,
    files_archive_path  TEXT,
    config_archive_path TEXT,
    checksum            TEXT,
    size_bytes          INTEGER,
    notes               TEXT,
    error_message       TEXT
);

CREATE TABLE IF NOT EXISTS snapshot_restore_events (
    id                      TEXT PRIMARY KEY,
    snapshot_id             TEXT NOT NULL,
    restored_by             INTEGER NOT NULL,
    started_at              TEXT NOT NULL,
    completed_at            TEXT,
    status                  TEXT NOT NULL,
    restore_mode            TEXT NOT NULL,
    pre_restore_snapshot_id TEXT,
    checksum_verified       INTEGER NOT NULL DEFAULT 0,
    dry_run                 INTEGER NOT NULL DEFAULT 0,
    error_message           TEXT
);

CREATE TABLE IF NOT EXISTS server_modes (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    current_mode       TEXT NOT NULL,
    previous_mode      TEXT,
    active_snapshot_id TEXT,
    checkpoint_id      TEXT,
    mode_changed_by    INTEGER,
    mode_changed_at    TEXT,
    notes              TEXT,
    reason             TEXT,
    config_json        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS server_checkpoints (
    id                         TEXT PRIMARY KEY,
    snapshot_id                TEXT NOT NULL,
    checkpoint_type            TEXT NOT NULL,
    from_mode                  TEXT,
    target_mode                TEXT NOT NULL,
    created_by                 INTEGER NOT NULL,
    created_at                 TEXT NOT NULL,
    status                     TEXT NOT NULL,
    db_snapshot_hash           TEXT,
    config_hash                TEXT,
    security_settings_hash     TEXT,
    points_chain_hash          TEXT,
    cloud_drive_metadata_hash  TEXT,
    integrity_manifest_hash    TEXT,
    components_json            TEXT NOT NULL DEFAULT '{}',
    error_message              TEXT
);

CREATE TABLE IF NOT EXISTS mode_switch_logs (
    id                  TEXT PRIMARY KEY,
    event_uuid          TEXT,
    from_mode           TEXT,
    to_mode             TEXT NOT NULL,
    actor_user_id       INTEGER,
    actor_id            INTEGER,
    actor_role          TEXT,
    source_ip           TEXT,
    user_agent          TEXT,
    request_id          TEXT,
    reason              TEXT,
    checkpoint_id       TEXT,
    snapshot_id         TEXT,
    success             INTEGER NOT NULL DEFAULT 0,
    error_message       TEXT,
    config_diff_json    TEXT NOT NULL DEFAULT '{}',
    restore_result_json TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    prev_hash           TEXT NOT NULL DEFAULT '',
    row_hash            TEXT NOT NULL DEFAULT '',
    server_boot_id      TEXT,
    hmac_signature      TEXT,
    key_version         TEXT
);

CREATE TABLE IF NOT EXISTS security_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose     TEXT NOT NULL,
    key_version TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    rotated_at  TEXT,
    disabled_at TEXT,
    status      TEXT NOT NULL,
    UNIQUE(purpose, key_version)
);

CREATE TABLE IF NOT EXISTS tester_token_audit (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id         TEXT,
    route            TEXT,
    normalized_route TEXT,
    method           TEXT,
    allowed          INTEGER NOT NULL DEFAULT 0,
    reason           TEXT,
    source_ip        TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tester_tokens (
    id                      TEXT PRIMARY KEY,
    token_hash              TEXT NOT NULL,
    tester_user_id          INTEGER,
    mode_scope_json         TEXT NOT NULL DEFAULT '["test","internal_test"]',
    route_scope_json        TEXT NOT NULL DEFAULT '[]',
    method_scope_json       TEXT NOT NULL DEFAULT '["GET","POST","PUT","PATCH","DELETE"]',
    allowed_features_json   TEXT NOT NULL DEFAULT '[]',
    allowed_routes_json     TEXT NOT NULL DEFAULT '[]',
    expires_at              TEXT NOT NULL,
    issued_at               TEXT,
    nonce                   TEXT,
    max_requests_per_minute INTEGER NOT NULL DEFAULT 60,
    can_modify_own_role     INTEGER NOT NULL DEFAULT 0,
    can_modify_own_points   INTEGER NOT NULL DEFAULT 0,
    can_run_security_tests  INTEGER NOT NULL DEFAULT 0,
    created_by              INTEGER NOT NULL,
    created_at              TEXT NOT NULL,
    revoked_at              TEXT,
    hmac_signature          TEXT,
    key_version             TEXT
);

CREATE TABLE IF NOT EXISTS superweak_dirty_writes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_epoch TEXT NOT NULL,
    table_name    TEXT,
    operation     TEXT,
    row_ref       TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploaded_files (
    id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    storage_path TEXT NOT NULL,
    privacy_mode TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    scan_status TEXT NOT NULL,
    original_filename_encrypted TEXT,
    original_filename_plain_for_public TEXT,
    mime_type_encrypted TEXT,
    mime_type_plain_for_public TEXT,
    size_bytes INTEGER NOT NULL,
    ciphertext_sha256 TEXT,
    plaintext_sha256 TEXT,
    encryption_algorithm TEXT,
    encryption_version TEXT,
    nonce TEXT,
    client_scan_report_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS encrypted_file_keys (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
    recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    encrypted_file_key TEXT NOT NULL,
    wrapped_by TEXT NOT NULL,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS file_scan_results (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
    scanner_name TEXT NOT NULL,
    scanner_version TEXT,
    scan_started_at TEXT,
    scan_completed_at TEXT,
    result TEXT NOT NULL,
    malware_name TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_access_logs (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
    actor_user_id INTEGER,
    action TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    result TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_type_policies (
    category TEXT PRIMARY KEY,
    extensions_json TEXT NOT NULL,
    public_allowed INTEGER NOT NULL,
    server_readable_allowed INTEGER NOT NULL,
    e2ee_allowed INTEGER NOT NULL,
    default_risk_level TEXT NOT NULL,
    allow_public_share INTEGER NOT NULL,
    requires_scan INTEGER NOT NULL,
    warn_on_download INTEGER NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cloud_drive_security_policies (
    scope TEXT PRIMARY KEY,
    require_scan_before_download INTEGER NOT NULL,
    block_unclean_downloads INTEGER NOT NULL,
    warn_high_risk_downloads INTEGER NOT NULL,
    allow_inline_preview_for_high_risk INTEGER NOT NULL,
    e2ee_server_scan_claim_allowed INTEGER NOT NULL,
    revoke_shares_on_suspension INTEGER NOT NULL,
    scanner_enabled INTEGER NOT NULL,
    scanner_backend TEXT NOT NULL,
    scanner_command TEXT,
    scanner_timeout_seconds INTEGER NOT NULL,
    fail_closed_on_scanner_error INTEGER NOT NULL,
    quarantine_on_infected INTEGER NOT NULL,
    validate_magic_mime INTEGER NOT NULL,
    deep_archive_scan_enabled INTEGER NOT NULL DEFAULT 1,
    max_archive_depth INTEGER NOT NULL DEFAULT 2,
    office_macro_scan_enabled INTEGER NOT NULL DEFAULT 1,
    image_reencode_enabled INTEGER NOT NULL DEFAULT 1,
    image_reencode_max_pixels INTEGER NOT NULL DEFAULT 25000000,
    yara_enabled INTEGER NOT NULL DEFAULT 0,
    yara_command TEXT,
    yara_rules_path TEXT,
    max_archive_files INTEGER NOT NULL,
    max_archive_uncompressed_bytes INTEGER NOT NULL,
    max_daily_downloads INTEGER NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_uploaded_files_owner ON uploaded_files(owner_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_uploaded_files_risk ON uploaded_files(risk_level, scan_status);
CREATE INDEX IF NOT EXISTS idx_encrypted_file_keys_file_recipient ON encrypted_file_keys(file_id, recipient_user_id);
CREATE INDEX IF NOT EXISTS idx_file_scan_results_file ON file_scan_results(file_id, created_at);
CREATE INDEX IF NOT EXISTS idx_file_access_logs_file ON file_access_logs(file_id, created_at);

CREATE TABLE IF NOT EXISTS user_storage (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    quota_bytes INTEGER NOT NULL DEFAULT 0,
    used_bytes INTEGER NOT NULL DEFAULT 0,
    reserved_bytes INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storage_quota_overrides (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    quota_bytes INTEGER,
    max_file_size_bytes INTEGER,
    upload_rate_limit_per_day INTEGER,
    can_upload_override INTEGER,
    reason TEXT,
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storage_files (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES storage_files(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    virtual_path TEXT NOT NULL,
    is_trashed INTEGER NOT NULL DEFAULT 0,
    trashed_at TEXT,
    restored_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(owner_user_id, virtual_path)
);

CREATE TABLE IF NOT EXISTS storage_folders (
    id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    virtual_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(owner_user_id, virtual_path)
);

CREATE TABLE IF NOT EXISTS storage_quota_log (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id TEXT REFERENCES uploaded_files(id) ON DELETE SET NULL,
    delta_bytes INTEGER NOT NULL,
    before_used_bytes INTEGER NOT NULL,
    after_used_bytes INTEGER NOT NULL,
    source TEXT NOT NULL,
    reason TEXT,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storage_quota_reduction_notices (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    old_level TEXT,
    new_level TEXT NOT NULL,
    old_quota_bytes INTEGER,
    new_quota_bytes INTEGER NOT NULL,
    used_bytes_at_notice INTEGER NOT NULL,
    deadline_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    notice_message TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    purged_at TEXT,
    deleted_file_count INTEGER NOT NULL DEFAULT 0,
    deleted_bytes INTEGER NOT NULL DEFAULT 0,
    CHECK (status IN ('pending', 'resolved', 'purged'))
);

CREATE TABLE IF NOT EXISTS albums (
    id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    visibility TEXT NOT NULL DEFAULT 'private',
    cover_file_id TEXT REFERENCES uploaded_files(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS album_files (
    id TEXT PRIMARY KEY,
    album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    storage_file_id TEXT REFERENCES storage_files(id) ON DELETE SET NULL,
    file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    caption TEXT,
    added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(album_id, file_id)
);

CREATE TABLE IF NOT EXISTS storage_share_links (
    id TEXT PRIMARY KEY,
    storage_file_id TEXT NOT NULL REFERENCES storage_files(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    can_download INTEGER NOT NULL DEFAULT 1,
    can_preview INTEGER NOT NULL DEFAULT 0,
    access_scope TEXT NOT NULL DEFAULT 'link',
    required_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    max_views INTEGER NOT NULL DEFAULT 0,
    wrapped_file_key_envelope TEXT,
    expires_at TEXT,
    revoked_at TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_storage_files_owner_path ON storage_files(owner_user_id, virtual_path);
CREATE INDEX IF NOT EXISTS idx_storage_files_file ON storage_files(file_id);
CREATE INDEX IF NOT EXISTS idx_storage_folders_owner_path ON storage_folders(owner_user_id, virtual_path);
CREATE INDEX IF NOT EXISTS idx_storage_quota_log_user ON storage_quota_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_storage_quota_overrides_enabled ON storage_quota_overrides(enabled);
CREATE INDEX IF NOT EXISTS idx_storage_quota_notices_due ON storage_quota_reduction_notices(status, deadline_at);
CREATE INDEX IF NOT EXISTS idx_storage_quota_notices_user ON storage_quota_reduction_notices(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_albums_owner ON albums(owner_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_album_files_album ON album_files(album_id, sort_order, created_at);
CREATE INDEX IF NOT EXISTS idx_storage_share_links_owner ON storage_share_links(owner_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_storage_share_links_file ON storage_share_links(storage_file_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_storage_share_links_required_user ON storage_share_links(required_user_id, revoked_at);

CREATE TABLE IF NOT EXISTS cloud_file_refs (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    context_type TEXT NOT NULL,
    context_id TEXT NOT NULL,
    attached_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    permission_snapshot_json TEXT
);

CREATE TABLE IF NOT EXISTS file_access_grants (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
    granted_to_user_id INTEGER,
    granted_to_role TEXT,
    granted_to_group_id TEXT,
    context_type TEXT NOT NULL,
    context_id TEXT NOT NULL,
    can_download INTEGER NOT NULL DEFAULT 1,
    can_preview INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS announcement_attachment_requests (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    announcement_id INTEGER REFERENCES announcements(id),
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cloud_file_refs_file ON cloud_file_refs(file_id);
CREATE INDEX IF NOT EXISTS idx_cloud_file_refs_context ON cloud_file_refs(context_type, context_id);
CREATE INDEX IF NOT EXISTS idx_cloud_file_refs_owner ON cloud_file_refs(owner_user_id, created_at);

CREATE TABLE IF NOT EXISTS cloud_resumable_upload_sessions (
    session_id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    target TEXT NOT NULL DEFAULT 'cloud_drive',
    status TEXT NOT NULL DEFAULT 'uploading',
    filename TEXT NOT NULL,
    mime_type TEXT,
    total_bytes INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL,
    total_chunks INTEGER NOT NULL,
    received_bytes INTEGER NOT NULL DEFAULT 0,
    received_chunks_json TEXT NOT NULL DEFAULT '[]',
    temp_dir TEXT NOT NULL,
    privacy_mode TEXT NOT NULL DEFAULT 'standard_plain',
    metadata_json TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cloud_resumable_owner_status ON cloud_resumable_upload_sessions(owner_user_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_file_access_grants_file ON file_access_grants(file_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_file_access_grants_user_context ON file_access_grants(granted_to_user_id, context_type, context_id);
CREATE INDEX IF NOT EXISTS idx_announcement_attachment_requests_status ON announcement_attachment_requests(status, created_at);

CREATE TABLE IF NOT EXISTS integrity_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    category TEXT,
    risk_level TEXT NOT NULL,
    change_type TEXT NOT NULL,
    old_hash TEXT,
    new_hash TEXT,
    old_size INTEGER,
    new_size INTEGER,
    old_mtime TEXT,
    new_mtime TEXT,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT
);

CREATE TABLE IF NOT EXISTS integrity_scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    files_checked INTEGER NOT NULL DEFAULT 0,
    findings_created INTEGER NOT NULL DEFAULT 0,
    high_risk_count INTEGER NOT NULL DEFAULT 0,
    manifest_valid INTEGER NOT NULL DEFAULT 0,
    manifest_signature_valid INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS integrity_manifest_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL,
    manifest_signature TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_integrity_findings_status ON integrity_findings(status, risk_level, detected_at);
CREATE INDEX IF NOT EXISTS idx_integrity_findings_path ON integrity_findings(file_path, status);

CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        );

CREATE TABLE IF NOT EXISTS secure_audit (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            action      TEXT    NOT NULL,
            ip          TEXT,
            user        TEXT,
            success     INTEGER NOT NULL DEFAULT 0,
            ua          TEXT,
            detail      TEXT,
            chain_hash  TEXT    NOT NULL   /* SHA256HMAC(prev_hash || entry_json) */
        , prev_hash TEXT, entry_hash TEXT);

CREATE TABLE IF NOT EXISTS secure_violations (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            username       TEXT    NOT NULL,
            points         INTEGER NOT NULL DEFAULT 1,
            reason         TEXT    NOT NULL,
            triggered_by   TEXT    NOT NULL,   /* 'system' | 'manager' | 'super_admin' */
            actor_username TEXT    NOT NULL,   /* 操作者 */
            created_at     TEXT    NOT NULL,
            prev_hash      TEXT    NOT NULL,   /* 上一筆記錄的 chain_hash */
            entry_hash     TEXT    NOT NULL    /* 本筆記錄的 hash */
        );

CREATE TABLE IF NOT EXISTS security_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type   TEXT    NOT NULL,   /* 'login_fail' | 'ip_block' | 'rate_limit' | '403_access' */
            ip_address   TEXT    NOT NULL,
            target_user  TEXT,
            detail       TEXT,
            created_at   TEXT    NOT NULL
        );

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT    NOT NULL UNIQUE,
    ip_address   TEXT,
    user_agent   TEXT,
    device_info  TEXT,
    ip_country   TEXT,
    expires_at   TEXT    NOT NULL,
    is_revoked   INTEGER NOT NULL DEFAULT 0,
    revoked_at   TEXT,
    last_seen    TEXT,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    auth_scope   TEXT    NOT NULL DEFAULT '',
    allowed_features_json TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS system_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            updated_by  TEXT
        );

CREATE TABLE IF NOT EXISTS user_passwords (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    password_hash   TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    NOT NULL UNIQUE,
    email      TEXT,
    -- Personal info (reserved for future expansion)
    real_name        TEXT,
    birthdate        TEXT,
    id_number        TEXT,
    phone            TEXT,
    -- Account status
    status     TEXT    NOT NULL DEFAULT 'active',
    member_level TEXT  NOT NULL DEFAULT 'normal',
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
    email_verified INTEGER NOT NULL DEFAULT 0,
    two_factor_enabled INTEGER NOT NULL DEFAULT 0,
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_strength_score INTEGER NOT NULL DEFAULT 0,
    last_login_at TEXT,
    password_changed_at TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    is_default_password INTEGER NOT NULL DEFAULT 0,
    avatar_file_id TEXT,
    avatar_crop_json TEXT,
    deleted_at TEXT,
    -- Timestamps
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
, role TEXT NOT NULL DEFAULT 'user', nickname TEXT, blocked_until TEXT, violation_count INTEGER NOT NULL DEFAULT 0, chat_violation_warned INTEGER NOT NULL DEFAULT 0);

CREATE INDEX IF NOT EXISTS idx_users_status_id ON users(status, id);
CREATE INDEX IF NOT EXISTS idx_users_role_status_id ON users(role, status, id);
CREATE INDEX IF NOT EXISTS idx_users_effective_level_status_id ON users(effective_level, status, id);
CREATE INDEX IF NOT EXISTS idx_users_sanction_status_until ON users(sanction_status, sanction_until);
CREATE INDEX IF NOT EXISTS idx_users_last_login_id ON users(last_login_at, id);
CREATE INDEX IF NOT EXISTS idx_users_lower_username ON users(lower(username));
CREATE INDEX IF NOT EXISTS idx_users_lower_email ON users(lower(email));

CREATE TABLE IF NOT EXISTS violation_appeals (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                 INTEGER NOT NULL,
            username                TEXT    NOT NULL,
            latest_violation_id     INTEGER,
            violation_count_snapshot INTEGER NOT NULL DEFAULT 0,
            penalty_points          INTEGER NOT NULL DEFAULT 0,
            pre_status              TEXT    NOT NULL DEFAULT 'active',
            pre_role                TEXT    NOT NULL DEFAULT 'user',
            reason                  TEXT    NOT NULL,
            status                  TEXT    NOT NULL DEFAULT 'pending',  /* pending / reviewing_approve / approved / rejected */
            reviewed_by             TEXT,
            reviewed_at             TEXT,
            review_note             TEXT,
            created_at              TEXT    NOT NULL,
            CONSTRAINT fk_appeal_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

CREATE INDEX IF NOT EXISTS idx_appeal_created_at ON violation_appeals(created_at);

CREATE INDEX IF NOT EXISTS idx_appeal_status     ON violation_appeals(status);

CREATE INDEX IF NOT EXISTS idx_appeal_user      ON violation_appeals(user_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_appeal_user_violation
    ON violation_appeals(user_id, latest_violation_id);

CREATE TABLE IF NOT EXISTS admin_sanction_appeal_contexts (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            violation_id          INTEGER NOT NULL UNIQUE,
            user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            pre_status            TEXT,
            pre_role              TEXT,
            pre_base_level        TEXT,
            pre_member_level      TEXT,
            pre_effective_level   TEXT,
            pre_sanction_status   TEXT,
            pre_sanction_until    TEXT,
            action_label          TEXT NOT NULL,
            reason                TEXT NOT NULL,
            actor_username        TEXT NOT NULL,
            created_at            TEXT NOT NULL
        );

CREATE INDEX IF NOT EXISTS idx_admin_sanction_context_user ON admin_sanction_appeal_contexts(user_id, created_at);

CREATE TABLE IF NOT EXISTS reports (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type          TEXT NOT NULL,
    target_id            INTEGER,
    reporter_user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reported_user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reason               TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
    claimed_by_user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    claimed_by_username  TEXT,
    claimed_at           TEXT,
    reviewed_by          TEXT,
    reviewed_at          TEXT,
    review_note          TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE(target_type, target_id, reporter_user_id, reason)
);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    link            TEXT,
    is_read         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    read_at         TEXT
);

CREATE TABLE IF NOT EXISTS comfyui_image_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_key TEXT NOT NULL UNIQUE,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prompt_id TEXT,
    backend_url TEXT NOT NULL DEFAULT '',
    image_ref_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS comfyui_generation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    backend_url TEXT NOT NULL DEFAULT '',
    generation_mode TEXT NOT NULL DEFAULT 'txt2img',
    payload_json TEXT NOT NULL,
    input_assets_json TEXT NOT NULL DEFAULT '{}',
    controlnet_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comfyui_image_favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL DEFAULT 'manual',
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    civitai_image_id INTEGER,
    image_url TEXT NOT NULL DEFAULT '',
    backend_url TEXT NOT NULL DEFAULT '',
    image_ref_json TEXT NOT NULL DEFAULT '{}',
    cloud_file_id TEXT NOT NULL DEFAULT '',
    storage_file_id TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    prompt TEXT NOT NULL DEFAULT '',
    negative_prompt TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    vae TEXT NOT NULL DEFAULT '',
    sampler_name TEXT NOT NULL DEFAULT '',
    scheduler TEXT NOT NULL DEFAULT '',
    seed TEXT NOT NULL DEFAULT '',
    steps INTEGER,
    cfg REAL,
    width INTEGER,
    height INTEGER,
    params_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comfyui_workflow_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private',
    is_official INTEGER NOT NULL DEFAULT 0,
    system_bundle_id TEXT,
    purpose TEXT NOT NULL DEFAULT 'custom',
    comfyui_version TEXT NOT NULL DEFAULT '',
    project_version TEXT NOT NULL DEFAULT '',
    workflow_schema_version TEXT NOT NULL DEFAULT '1',
    workflow_json TEXT NOT NULL,
    workflow_hash TEXT NOT NULL DEFAULT '',
    layout_json TEXT NOT NULL DEFAULT '{}',
    required_models_json TEXT NOT NULL DEFAULT '[]',
    required_loras_json TEXT NOT NULL DEFAULT '[]',
    required_controlnets_json TEXT NOT NULL DEFAULT '[]',
    required_custom_nodes_json TEXT NOT NULL DEFAULT '[]',
    default_params_json TEXT NOT NULL DEFAULT '{}',
    is_default INTEGER NOT NULL DEFAULT 0,
    published_by_user_id INTEGER,
    published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comfyui_workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preset_id INTEGER NOT NULL REFERENCES comfyui_workflow_presets(id) ON DELETE CASCADE,
    actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prompt TEXT NOT NULL DEFAULT '',
    negative_prompt TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    workflow_json TEXT NOT NULL DEFAULT '{}',
    output_refs_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at);
CREATE INDEX IF NOT EXISTS idx_reports_claimed ON reports(claimed_by_user_id, status);
CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, is_read, created_at);
CREATE INDEX IF NOT EXISTS idx_comfyui_image_refs_owner ON comfyui_image_refs(owner_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comfyui_generation_history_owner ON comfyui_generation_history(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_comfyui_image_favorites_owner ON comfyui_image_favorites(owner_user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comfyui_image_favorites_civitai_owner ON comfyui_image_favorites(owner_user_id, civitai_image_id) WHERE civitai_image_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_comfyui_workflow_presets_owner ON comfyui_workflow_presets(owner_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_comfyui_workflow_presets_official ON comfyui_workflow_presets(is_official, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_comfyui_workflow_runs_preset ON comfyui_workflow_runs(preset_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_messages_room     ON chat_messages(room_id);

CREATE INDEX IF NOT EXISTS idx_chat_messages_time     ON chat_messages(created_at);

CREATE INDEX IF NOT EXISTS idx_user_friends_user_status ON user_friends(user_id, status);
CREATE INDEX IF NOT EXISTS idx_user_friends_friend_status ON user_friends(friend_user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profiles_friend_code ON user_profiles(friend_code) WHERE friend_code IS NOT NULL AND friend_code<>'';
CREATE INDEX IF NOT EXISTS idx_chat_room_invites_invitee ON chat_room_invites(invitee_user_id, status);
CREATE INDEX IF NOT EXISTS idx_game_matches_players ON game_matches(game_key, status, white_user_id, black_user_id);
CREATE INDEX IF NOT EXISTS idx_game_matches_finished ON game_matches(game_key, mode, finished_at);
CREATE INDEX IF NOT EXISTS idx_game_invites_user_status ON game_invites(game_key, opponent_user_id, status);
CREATE INDEX IF NOT EXISTS idx_game_rewards_week ON game_leaderboard_rewards(game_key, week_key);

CREATE INDEX IF NOT EXISTS idx_chat_reports_message ON chat_message_reports(message_id);

CREATE INDEX IF NOT EXISTS idx_chat_reports_status ON chat_message_reports(status);

CREATE INDEX IF NOT EXISTS idx_chat_room_members_room ON chat_room_members(room_id);

CREATE INDEX IF NOT EXISTS idx_chat_room_members_user ON chat_room_members(user_id);

CREATE INDEX IF NOT EXISTS idx_csrf_token_hash ON csrf_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_csrf_expires_at ON csrf_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_csrf_username_expires ON csrf_tokens(username, expires_at);

CREATE INDEX IF NOT EXISTS idx_account_recovery_tokens_hash ON account_recovery_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_account_recovery_tokens_user ON account_recovery_tokens(user_id, purpose, created_at);
CREATE INDEX IF NOT EXISTS idx_mail_outbox_kind ON mail_outbox(kind, created_at);

CREATE INDEX IF NOT EXISTS idx_ip_blocks_ip ON ip_blocks(ip_address);
CREATE INDEX IF NOT EXISTS idx_ip_blocks_until ON ip_blocks(blocked_until);

CREATE INDEX IF NOT EXISTS idx_login_attempts_ip    ON login_attempts(ip_address);

CREATE INDEX IF NOT EXISTS idx_login_attempts_time   ON login_attempts(attempted_at);

CREATE INDEX IF NOT EXISTS idx_login_attempts_user   ON login_attempts(user_id);
CREATE INDEX IF NOT EXISTS idx_login_attempts_user_success_time ON login_attempts(user_id, success, attempted_at);

CREATE INDEX IF NOT EXISTS idx_member_level_rules_level ON member_level_rules(level);
CREATE INDEX IF NOT EXISTS idx_member_level_audit_target ON member_level_audit(target_user, created_at);

CREATE INDEX IF NOT EXISTS idx_moderation_proposals_status ON moderation_proposals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_moderation_proposals_target ON moderation_proposals(target_user_id);
CREATE INDEX IF NOT EXISTS idx_moderation_votes_proposal ON moderation_votes(proposal_id);
CREATE INDEX IF NOT EXISTS idx_moderation_actions_target ON moderation_actions(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_moderation_actions_moderator ON moderation_actions(moderator_id, created_at);

CREATE INDEX IF NOT EXISTS idx_user_mod_notes_user ON user_mod_notes(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_reputation_events_user ON reputation_events(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_snapshots_type_status ON snapshots(type, status, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshot_restore_events_snapshot ON snapshot_restore_events(snapshot_id);

CREATE INDEX IF NOT EXISTS idx_sec_event_ip    ON security_events(ip_address);

CREATE INDEX IF NOT EXISTS idx_sec_event_time  ON security_events(created_at);

CREATE INDEX IF NOT EXISTS idx_sec_event_type  ON security_events(event_type);
CREATE INDEX IF NOT EXISTS idx_sec_event_type_ip_time ON security_events(event_type, ip_address, created_at);

CREATE INDEX IF NOT EXISTS idx_sec_viol_actor  ON secure_violations(actor_username);

CREATE INDEX IF NOT EXISTS idx_sec_viol_reason  ON secure_violations(reason);

CREATE INDEX IF NOT EXISTS idx_sec_viol_user   ON secure_violations(user_id);

CREATE INDEX IF NOT EXISTS idx_secure_audit_action ON secure_audit(action);

CREATE INDEX IF NOT EXISTS idx_secure_audit_ts    ON secure_audit(ts);

CREATE INDEX IF NOT EXISTS idx_secure_audit_user   ON secure_audit(user);

CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);

CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen);

CREATE INDEX IF NOT EXISTS idx_sessions_revoked ON sessions(is_revoked);
CREATE INDEX IF NOT EXISTS idx_sessions_user_active ON sessions(user_id, is_revoked, expires_at, last_seen);
CREATE INDEX IF NOT EXISTS idx_sessions_active_expires ON sessions(is_revoked, expires_at);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id     ON sessions(user_id);
