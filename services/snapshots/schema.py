import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from services.platform.bootstrap import CURRENT_SCHEMA_VERSION
from services.platform.release_info import APP_RELEASE_ID
from services.platform.settings import MANAGEMENT_ONLY_RESET_SETTINGS
from services.server.runtime import default_runtime_root_path

SNAPSHOT_ID_RE = re.compile(r"^snap_\d{8}_\d{6}_[a-f0-9]{6}$")
SHA256_REPORT_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
SERVER_BOOT_ID = os.environ.get("SERVER_BOOT_ID") or secrets.token_hex(16)
SNAPSHOT_TYPES = {
    "manual",
    "before_superweak",
    "before_incident_lockdown",
    "mode_checkpoint",
    "scheduled",
    "pre_restore",
    "pre_reset",
    "pre_migration",
    "emergency",
}
RESTORE_MODES = {"full", "db_only", "files_only", "config_only", "dry_run"}
SERVER_MODES = {"production", "preprod", "dev_ready", "internal_test", "test", "superweak", "maintenance", "incident_lockdown"}
MODE_CONFIRM_PHRASES = {
    "superweak": "ENABLE_SUPERWEAK",
    "test": "SWITCH_TO_TEST",
    "internal_test": "SWITCH_TO_INTERNAL_TEST",
    "dev_ready": "SWITCH_TO_DEV_READY",
    "preprod": "SWITCH_TO_DEV_READY",
    "production": "GO_LIVE",
    "maintenance": "ENTER_MAINTENANCE",
    "incident_lockdown": "ENTER_INCIDENT_LOCKDOWN",
}
PRODUCTION_REQUIRED_REPORT_TYPES = (
    "clean_smoke",
    "adversarial",
    "redteam_l2",
    "pytest",
    "log_chain_verify",
    "integrity_guard",
    "stress",
    "permission",
    "functional",
    "pentest",
    "snapshot_restore",
    "points_chain_consistency",
    "cloud_drive_quota_permission",
    "ai_agent_boundary",
    "operational_campaign_24h",
)
PORTABLE_SNAPSHOT_FILES = ("metadata.json", "checksums.sha256", "db.sqlite3.backup", "uploads.tar.gz", "config.tar.gz", "manifest.json")
DEFAULT_ACCOUNT_NAMES = ("root", "admin", "test")
TEST_ACCOUNT_NAMES = ("test",)


def _default_runtime_base_dir():
    raw = str(os.environ.get("HACKME_RUNTIME_DIR") or "").strip()
    if raw:
        candidate = Path(os.path.expanduser(os.path.expandvars(raw))).resolve()
    else:
        candidate = default_runtime_root_path()
    if candidate.exists() and not candidate.is_dir():
        raise RuntimeError(
            "runtime path is blocked by a non-directory file; set HACKME_RUNTIME_DIR "
            "or provide an explicit runtime base dir"
        )
    return candidate


def _json_hash(payload):
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _mode_switch_log_payload(row, prev_hash):
    get = row.get if isinstance(row, dict) else lambda key, default=None: row[key] if key in row.keys() else default
    return {
        "id": get("id", ""),
        "from_mode": get("from_mode", ""),
        "to_mode": get("to_mode", ""),
        "actor_user_id": get("actor_user_id", None),
        "reason": get("reason", ""),
        "checkpoint_id": get("checkpoint_id", ""),
        "snapshot_id": get("snapshot_id", ""),
        "success": int(get("success", 0) or 0),
        "error_message": get("error_message", ""),
        "config_diff_json": get("config_diff_json", "{}") or "{}",
        "restore_result_json": get("restore_result_json", "{}") or "{}",
        "created_at": get("created_at", ""),
        "prev_hash": prev_hash or "",
        "event_uuid": get("event_uuid", ""),
        "actor_id": get("actor_id", get("actor_user_id", None)),
        "actor_role": get("actor_role", ""),
        "source_ip": get("source_ip", ""),
        "user_agent": get("user_agent", ""),
        "request_id": get("request_id", ""),
        "server_boot_id": get("server_boot_id", ""),
        "key_version": get("key_version", ""),
    }


def _mode_switch_log_hash(row, prev_hash):
    return _json_hash(_mode_switch_log_payload(row, prev_hash))


def _mode_switch_signature_payload(row):
    get = row.get if isinstance(row, dict) else lambda key, default=None: row[key] if key in row.keys() else default
    return {
        "id": get("id", ""),
        "event_uuid": get("event_uuid", ""),
        "prev_hash": get("prev_hash", ""),
        "row_hash": get("row_hash", ""),
        "from_mode": get("from_mode", ""),
        "to_mode": get("to_mode", ""),
        "actor_id": get("actor_id", get("actor_user_id", None)),
        "actor_role": get("actor_role", ""),
        "source_ip": get("source_ip", ""),
        "request_id": get("request_id", ""),
        "server_boot_id": get("server_boot_id", ""),
        "created_at": get("created_at", ""),
        "key_version": get("key_version", ""),
    }


def _hmac_sha256(secret, payload):
    return hmac.new(
        secret.encode("utf-8"),
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _canonical_json_text(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _production_report_signature_payload(row):
    get = row.get if isinstance(row, dict) else lambda key, default=None: row[key] if key in row.keys() else default
    return {
        "report_type": get("report_type", ""),
        "report_hash": get("report_hash", ""),
        "target_commit": get("target_commit", ""),
        "target_branch": get("target_branch", ""),
        "server_mode": get("server_mode", ""),
        "test_result": get("test_result", ""),
        "pass": int(get("pass", 0) or 0),
        "critical_findings_count": int(get("critical_findings_count", 0) or 0),
        "high_findings_count": int(get("high_findings_count", 0) or 0),
        "unresolved_findings_json": get("unresolved_findings_json", "[]") or "[]",
        "tester": get("tester", ""),
        "raw_report_json": get("raw_report_json", "") or "",
        "report_source": get("report_source", "") or "",
        "key_version": get("key_version", ""),
    }


def _tester_token_signature_payload(row):
    get = row.get if isinstance(row, dict) else lambda key, default=None: row[key] if key in row.keys() else default
    return {
        "id": get("id", ""),
        "token_hash": get("token_hash", ""),
        "tester_user_id": get("tester_user_id", None),
        "mode_scope_json": get("mode_scope_json", "[]") or "[]",
        "route_scope_json": get("route_scope_json", get("allowed_routes_json", "[]")) or "[]",
        "method_scope_json": get("method_scope_json", "[]") or "[]",
        "expires_at": get("expires_at", ""),
        "issued_at": get("issued_at", get("created_at", "")),
        "nonce": get("nonce", ""),
        "max_requests_per_minute": int(get("max_requests_per_minute", 0) or 0),
        "key_version": get("key_version", ""),
    }


def _backfill_mode_switch_log_hashes(conn):
    try:
        rows = conn.execute(
            """
            SELECT * FROM mode_switch_logs
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    except Exception:
        return
    prev_hash = ""
    for row in rows:
        row_dict = dict(row)
        expected_hash = _mode_switch_log_hash(row_dict, prev_hash)
        if row_dict.get("prev_hash") != prev_hash or row_dict.get("row_hash") != expected_hash:
            conn.execute(
                "UPDATE mode_switch_logs SET prev_hash=?, row_hash=? WHERE id=?",
                (prev_hash, expected_hash, row_dict["id"]),
            )
        prev_hash = expected_hash


def verify_mode_switch_log_hash_chain(conn):
    try:
        rows = conn.execute(
            """
            SELECT * FROM mode_switch_logs
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    except Exception as exc:
        return {"ok": False, "msg": "mode_switch_logs unavailable", "error": str(exc), "count": 0}
    prev_hash = ""
    mismatches = []
    for index, row in enumerate(rows):
        row_dict = dict(row)
        expected_hash = _mode_switch_log_hash(row_dict, prev_hash)
        if row_dict.get("prev_hash") != prev_hash or row_dict.get("row_hash") != expected_hash:
            mismatches.append({
                "index": index,
                "id": row_dict.get("id"),
                "expected_prev_hash": prev_hash,
                "actual_prev_hash": row_dict.get("prev_hash", ""),
                "expected_row_hash": expected_hash,
                "actual_row_hash": row_dict.get("row_hash", ""),
            })
        prev_hash = row_dict.get("row_hash") or expected_hash
    return {
        "ok": not mismatches,
        "count": len(rows),
        "latest_hash": prev_hash,
        "mismatches": mismatches,
    }


def _normalize_mode_route(route):
    raw = str(route or "")
    decoded = urllib.parse.unquote(raw)
    lowered = raw.lower()
    if ";" in raw or "\\" in decoded or ".." in decoded or any(marker in lowered for marker in ("%2f", "%5c", "%2e")):
        return None, "route contains traversal, encoded slash/backslash/dot, or semicolon params"
    collapsed = re.sub(r"/+", "/", decoded.split("?", 1)[0])
    if not collapsed.startswith("/"):
        collapsed = "/" + collapsed
    return collapsed.rstrip("/") or "/", ""

BUILTIN_SECURITY_PROFILES = {
    "production": {
        "label": "production（上線）",
        "description": "正式上線設定檔：安全機制全開、停用測試帳戶，並要求預設帳戶重新設定強密碼。",
        "settings": {
            "maintenance_mode": False,
            "allow_register": False,
            "server_ssl_enabled": True,
            "audit_chain_enabled": True,
            "ip_blocking_enabled": True,
            "login_violation_enabled": True,
            "rate_limit_violation_enabled": True,
            "root_ip_whitelist_enabled": False,
            "root_ip_whitelist": "",
            "browser_only_mode_enabled": True,
            "production_single_ip_account_lock_enabled": False,
            "production_single_account_ip_lock_enabled": False,
            "integrity_guard_enabled": True,
            "integrity_guard_strict_mode": True,
            "feature_account_security_enabled": True,
            "feature_advanced_security_enabled": True,
            "feature_identity_governance_enabled": True,
            "feature_member_governance_enabled": True,
            "feature_server_modes_enabled": True,
            "feature_snapshot_restore_enabled": True,
            "feature_audit_log_enabled": True,
            "feature_economy_enabled": True,
            "feature_points_chain_enabled": True,
            "feature_violation_center_enabled": True,
            "feature_health_center_enabled": True,
            "captcha_mode": "math",
        },
        "thresholds": {
            "security_pending_chat_reports_threshold": 5,
            "security_pending_appeals_threshold": 5,
            "security_pending_moderation_proposals_threshold": 5,
            "security_quarantined_files_threshold": 0,
            "security_unknown_encrypted_files_threshold": 0,
        },
    },
    "dev_ready": {
        "label": "dev ready（準上線 / 開發就緒）",
        "description": "平時開發與準上線驗證模式；保留基本登入、root 權限與 CSRF，但暫停 production audit chain 與 integrity guard。交易在隔離 runtime 內可直接驗證。",
        "settings": {
            "maintenance_mode": False,
            "server_ssl_enabled": True,
            "audit_chain_enabled": False,
            "ip_blocking_enabled": False,
            "login_violation_enabled": False,
            "rate_limit_violation_enabled": True,
            "root_ip_whitelist_enabled": False,
            "root_ip_whitelist": "",
            "browser_only_mode_enabled": False,
            "integrity_guard_enabled": False,
            "integrity_guard_strict_mode": False,
            "feature_audit_log_enabled": True,
            "feature_economy_enabled": True,
            "feature_points_chain_enabled": True,
            "feature_trading_enabled": True,
        },
        "thresholds": {
            "security_pending_chat_reports_threshold": 10,
            "security_pending_appeals_threshold": 10,
            "security_pending_moderation_proposals_threshold": 10,
            "security_quarantined_files_threshold": 0,
            "security_unknown_encrypted_files_threshold": 50,
        },
        "color": "blue",
    },
    "internal_test": {
        "label": "internal test（內測）",
        "description": "內測模式：只有 root 可直接登入；其他帳號必須提供 root 發出的內測登入 token。Tester trading 走 shadow tables，不可寫 production wallet。",
        "settings": {
            "maintenance_mode": False,
            "allow_register": False,
            "server_ssl_enabled": True,
            "audit_chain_enabled": True,
            "ip_blocking_enabled": True,
            "login_violation_enabled": True,
            "rate_limit_violation_enabled": True,
            "root_ip_whitelist_enabled": False,
            "root_ip_whitelist": "",
            "browser_only_mode_enabled": True,
            "integrity_guard_enabled": True,
            "integrity_guard_strict_mode": True,
            "feature_account_security_enabled": True,
            "feature_server_modes_enabled": True,
            "feature_audit_log_enabled": True,
            "feature_economy_enabled": True,
            "feature_points_chain_enabled": True,
            # SERVER_MODE_V2_PROFILE_MATRIX.md §Mode Behavior Matrix footnote 2:
            # internal_test: trading shadow only / no production wallet write.
            # Tester actions are routed through test_shadow_* tables.
            "feature_trading_enabled": False,
        },
        "thresholds": {
            "security_pending_chat_reports_threshold": 10,
            "security_pending_appeals_threshold": 10,
            "security_pending_moderation_proposals_threshold": 10,
            "security_quarantined_files_threshold": 0,
            "security_unknown_encrypted_files_threshold": 25,
        },
        "color": "orange",
    },
    "test": {
        "label": "test（隔離 QA 測試場）",
        "description": (
            "Isolated QA bench：開發者 / QA agent 跑 curl / 自動化 / fuzz / 例外輸入用。"
            "Hard rules：不得用在公開正式站；不得接 production wallet / production ledger；"
            "必須顯示明顯 TEST MODE banner。browser_only 預設關方便自動化；"
            "密碼強度與強制改密可放寬；測試帳號與測試資料可 reset。"
        ),
        "settings": {
            "maintenance_mode": False,
            "server_ssl_enabled": True,
            "audit_chain_enabled": True,
            "ip_blocking_enabled": True,
            "login_violation_enabled": True,
            "rate_limit_violation_enabled": True,
            "root_ip_whitelist_enabled": False,
            "root_ip_whitelist": "",
            "browser_only_mode_enabled": False,
            "integrity_guard_enabled": True,
            "integrity_guard_strict_mode": False,
            "feature_audit_log_enabled": True,
            # SERVER_MODE_V2_PROFILE_MATRIX.md §test mode positioning:
            # test mode is "isolated QA bench" — economy on so that QA can
            # exercise wallet / ledger flows in isolated runtime, but trading
            # remains off because trading is the heaviest economic write and
            # accidentally pointing test runtime at production is high-impact.
            # If you need trading-flow QA, run it in `internal_test` mode and
            # let writes land in test_shadow_* tables.
            "feature_economy_enabled": True,
            "feature_points_chain_enabled": True,
            "feature_trading_enabled": False,
        },
        "thresholds": {
            "security_pending_chat_reports_threshold": 20,
            "security_pending_appeals_threshold": 20,
            "security_pending_moderation_proposals_threshold": 20,
            "security_quarantined_files_threshold": 0,
            "security_unknown_encrypted_files_threshold": 100,
        },
        "color": "orange",
    },
    "superweak": {
        "label": "superweak（弱化測試）",
        "description": "高風險弱化測試模式，進入前會建立 before_superweak snapshot，並關閉主要安全機制。",
        "settings": {
            "maintenance_mode": False,
            "server_ssl_enabled": False,
            "audit_chain_enabled": False,
            "ip_blocking_enabled": False,
            "login_violation_enabled": False,
            "rate_limit_violation_enabled": False,
            "root_ip_whitelist_enabled": False,
            "root_ip_whitelist": "",
            "browser_only_mode_enabled": False,
            "integrity_guard_enabled": False,
            "integrity_guard_strict_mode": False,
            "feature_audit_log_enabled": False,
            "feature_economy_enabled": False,
            "feature_points_chain_enabled": False,
            "feature_trading_enabled": False,
            "captcha_mode": "none",
        },
        "thresholds": {
            "security_pending_chat_reports_threshold": 50,
            "security_pending_appeals_threshold": 50,
            "security_pending_moderation_proposals_threshold": 50,
            "security_quarantined_files_threshold": 10,
            "security_unknown_encrypted_files_threshold": 250,
        },
        "color": "red",
    },
    "maintenance": {
        "label": "maintenance（維護）",
        "description": "更新、修復、migration、備份與 PointsChain 維護用模式；普通使用者不可正常操作。",
        "settings": {
            "maintenance_mode": True,
            "allow_register": False,
            "server_ssl_enabled": True,
            "audit_chain_enabled": True,
            "ip_blocking_enabled": True,
            "login_violation_enabled": True,
            "rate_limit_violation_enabled": True,
            "integrity_guard_enabled": True,
            "integrity_guard_strict_mode": False,
            # SERVER_MODE_V2_PROFILE_MATRIX.md §Mode Behavior Matrix footnote 4:
            # maintenance / incident_lockdown: browser_only_mode is "n/a"
            # because maintenance_mode already blocks normal user UI. We still
            # set it explicitly True here so that any operator UI surface is
            # browser-only on top of maintenance_mode (defense in depth).
            "browser_only_mode_enabled": True,
            "feature_audit_log_enabled": True,
            "feature_economy_enabled": True,
            "feature_points_chain_enabled": True,
            "feature_trading_enabled": False,
            "feature_comfyui_enabled": False,
            "feature_games_enabled": False,
        },
        "thresholds": {
            "security_pending_chat_reports_threshold": 10,
            "security_pending_appeals_threshold": 10,
            "security_pending_moderation_proposals_threshold": 10,
            "security_quarantined_files_threshold": 0,
            "security_unknown_encrypted_files_threshold": 25,
        },
        "color": "purple",
    },
    "incident_lockdown": {
        "label": "incident lockdown（事故封鎖）",
        "description": "疑似入侵、restore 失敗、PointsChain 或 integrity 異常時的強制封鎖模式。",
        "settings": {
            "maintenance_mode": True,
            "allow_register": False,
            "server_ssl_enabled": True,
            "audit_chain_enabled": True,
            "ip_blocking_enabled": True,
            "login_violation_enabled": True,
            "rate_limit_violation_enabled": True,
            "integrity_guard_enabled": True,
            "integrity_guard_strict_mode": True,
            # SERVER_MODE_V2_PROFILE_MATRIX.md §Mode Behavior Matrix footnote 4:
            # maintenance / incident_lockdown: browser_only_mode is "n/a"
            # because maintenance_mode already blocks normal user UI. We still
            # set it explicitly True here so that any operator UI surface is
            # browser-only on top of maintenance_mode (defense in depth).
            "browser_only_mode_enabled": True,
            "feature_audit_log_enabled": True,
            "feature_economy_enabled": True,
            "feature_points_chain_enabled": True,
            "feature_trading_enabled": False,
            "feature_comfyui_enabled": False,
            "feature_games_enabled": False,
        },
        "thresholds": {
            "security_pending_chat_reports_threshold": 1,
            "security_pending_appeals_threshold": 1,
            "security_pending_moderation_proposals_threshold": 1,
            "security_quarantined_files_threshold": 0,
            "security_unknown_encrypted_files_threshold": 0,
        },
        "color": "black_red",
    },
}
PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
RESETTABLE_TABLES = {
    "ai_agent_conversations",
    "appeals",
    "chat_message_reports",
    "chat_messages",
    "chat_room_invites",
    "chat_room_members",
    "chat_rooms",
    "comments",
    "cloud_resumable_upload_sessions",
    "comfyui_generation_history",
    "comfyui_generation_jobs",
    "comfyui_image_favorites",
    "comfyui_image_refs",
    "comfyui_template_preview_tokens",
    "comfyui_workflow_layout_versions",
    "comfyui_workflow_presets",
    "comfyui_workflow_runs",
    "direct_messages",
    "dm_threads",
    "encrypted_file_keys",
    "file_access_logs",
    "file_scan_results",
    "cloud_file_refs",
    "file_access_grants",
    "forum_boards",
    "forum_categories",
    "forum_post_reactions",
    "forum_post_reports",
    "forum_thread_reactions",
    "forum_thread_views",
    "forum_posts",
    "forum_threads",
    "game_daily_challenge_rewards",
    "game_invites",
    "game_leaderboard_rewards",
    "game_matches",
    "game_multiplayer_events",
    "game_multiplayer_invites",
    "game_multiplayer_player_states",
    "game_multiplayer_rooms",
    "game_solo_scores",
    "announcements",
    "announcement_attachment_requests",
    "album_files",
    "album_share_links",
    "albums",
    "job_center_events",
    "job_center_jobs",
    "media_stream_assets",
    "media_stream_jobs",
    "media_stream_segments",
    "media_stream_subtitles",
    "media_stream_variants",
    "messages",
    "moderation_actions",
    "moderation_proposals",
    "moderation_votes",
    "notifications",
    "posts",
    "reports",
    "share_access_events",
    "storage_quota_overrides",
    "storage_quota_purchases",
    "storage_quota_reduction_notices",
    "user_feature_restrictions",
    "user_follows",
    "user_friends",
    "violation_fine_appeals",
    "violation_fines",
    "violation_appeals",
    "storage_folders",
    "storage_files",
    "storage_quota_log",
    "storage_share_links",
    "trading_audit_events",
    "trading_bot_audit_findings",
    "trading_bot_audit_runs",
    "trading_bot_competition_rewards",
    "trading_bot_runs",
    "trading_bots",
    "trading_fills",
    "trading_futures_positions",
    "trading_grid_bots",
    "trading_grid_orders",
    "trading_margin_positions",
    "trading_market_price_snapshots",
    "trading_market_provider_mappings",
    "trading_market_registry_audit",
    "trading_markets_registry",
    "trading_markets",
    "trading_operation_idempotency",
    "trading_orders",
    "trading_pending_profit",
    "trading_reserve_pool",
    "trading_reserve_pool_events",
    "trading_settings",
    "trading_sim_accounts",
    "trading_state",
    "trading_spot_realized_pnl",
    "trading_spot_positions",
    "trading_trial_credits",
    "trading_trial_position_costs",
    "trading_user_volume_stats",
    "uploaded_files",
    "user_storage",
    "video_comments",
    "video_danmaku",
    "video_likes",
    "video_share_links",
    "video_tips",
    "video_views",
    "videos",
    "user_mod_notes",
    "violations",
}


@dataclass
class SnapshotResult:
    ok: bool
    snapshot_id: str = ""
    status: str = ""
    error: str = ""
    metadata: dict | None = None


def ensure_snapshot_schema(conn):
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        """
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
        )
        """
    )
    server_mode_cols = {row["name"] for row in conn.execute("PRAGMA table_info(server_modes)").fetchall()}
    for col, ddl in (
        ("previous_mode", "ALTER TABLE server_modes ADD COLUMN previous_mode TEXT"),
        ("active_snapshot_id", "ALTER TABLE server_modes ADD COLUMN active_snapshot_id TEXT"),
        ("checkpoint_id", "ALTER TABLE server_modes ADD COLUMN checkpoint_id TEXT"),
        ("mode_changed_by", "ALTER TABLE server_modes ADD COLUMN mode_changed_by INTEGER"),
        ("mode_changed_at", "ALTER TABLE server_modes ADD COLUMN mode_changed_at TEXT"),
        ("notes", "ALTER TABLE server_modes ADD COLUMN notes TEXT"),
        ("reason", "ALTER TABLE server_modes ADD COLUMN reason TEXT"),
        ("config_json", "ALTER TABLE server_modes ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in server_mode_cols:
            conn.execute(ddl)
    conn.execute(
        "INSERT OR IGNORE INTO server_modes "
        "(id, current_mode, previous_mode, active_snapshot_id, checkpoint_id, mode_changed_by, mode_changed_at, notes, reason, config_json) "
        "VALUES (1, 'dev_ready', NULL, NULL, NULL, NULL, ?, '', '', '{}')",
        (datetime.now().isoformat(),),
    )
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mode_switch_logs (
            id                 TEXT PRIMARY KEY,
            event_uuid         TEXT,
            from_mode          TEXT,
            to_mode            TEXT NOT NULL,
            actor_user_id      INTEGER,
            actor_id           INTEGER,
            actor_role         TEXT,
            source_ip          TEXT,
            user_agent         TEXT,
            request_id         TEXT,
            reason             TEXT,
            checkpoint_id      TEXT,
            snapshot_id        TEXT,
            success            INTEGER NOT NULL DEFAULT 0,
            error_message      TEXT,
            config_diff_json   TEXT NOT NULL DEFAULT '{}',
            restore_result_json TEXT NOT NULL DEFAULT '{}',
            created_at         TEXT NOT NULL,
            prev_hash          TEXT NOT NULL DEFAULT '',
            row_hash           TEXT NOT NULL DEFAULT '',
            server_boot_id     TEXT,
            hmac_signature     TEXT,
            key_version        TEXT
        )
        """
    )
    mode_log_cols = {row["name"] for row in conn.execute("PRAGMA table_info(mode_switch_logs)").fetchall()}
    for col, ddl in (
        ("event_uuid", "ALTER TABLE mode_switch_logs ADD COLUMN event_uuid TEXT"),
        ("actor_id", "ALTER TABLE mode_switch_logs ADD COLUMN actor_id INTEGER"),
        ("actor_role", "ALTER TABLE mode_switch_logs ADD COLUMN actor_role TEXT"),
        ("source_ip", "ALTER TABLE mode_switch_logs ADD COLUMN source_ip TEXT"),
        ("user_agent", "ALTER TABLE mode_switch_logs ADD COLUMN user_agent TEXT"),
        ("request_id", "ALTER TABLE mode_switch_logs ADD COLUMN request_id TEXT"),
        ("prev_hash", "ALTER TABLE mode_switch_logs ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''"),
        ("row_hash", "ALTER TABLE mode_switch_logs ADD COLUMN row_hash TEXT NOT NULL DEFAULT ''"),
        ("server_boot_id", "ALTER TABLE mode_switch_logs ADD COLUMN server_boot_id TEXT"),
        ("hmac_signature", "ALTER TABLE mode_switch_logs ADD COLUMN hmac_signature TEXT"),
        ("key_version", "ALTER TABLE mode_switch_logs ADD COLUMN key_version TEXT"),
    ):
        if col not in mode_log_cols:
            conn.execute(ddl)
    conn.execute("UPDATE mode_switch_logs SET event_uuid=id WHERE event_uuid IS NULL OR event_uuid=''")
    conn.execute("UPDATE mode_switch_logs SET actor_id=actor_user_id WHERE actor_id IS NULL")
    conn.execute("UPDATE mode_switch_logs SET actor_role='' WHERE actor_role IS NULL")
    conn.execute("UPDATE mode_switch_logs SET source_ip='' WHERE source_ip IS NULL")
    conn.execute("UPDATE mode_switch_logs SET user_agent='' WHERE user_agent IS NULL")
    conn.execute("UPDATE mode_switch_logs SET request_id='' WHERE request_id IS NULL")
    conn.execute("UPDATE mode_switch_logs SET server_boot_id='' WHERE server_boot_id IS NULL")
    conn.execute("UPDATE mode_switch_logs SET key_version='' WHERE key_version IS NULL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS security_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL,
            key_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            rotated_at TEXT,
            disabled_at TEXT,
            status TEXT NOT NULL,
            UNIQUE(purpose, key_version)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tester_token_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT,
            route TEXT,
            normalized_route TEXT,
            method TEXT,
            allowed INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            source_ip TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS superweak_dirty_writes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sandbox_epoch TEXT NOT NULL,
            table_name TEXT,
            operation TEXT,
            row_ref TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_mode_switch_logs_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_mode_switch_logs_no_delete")
    _backfill_mode_switch_log_hashes(conn)
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_mode_switch_logs_no_update
        BEFORE UPDATE ON mode_switch_logs
        BEGIN
            SELECT RAISE(ABORT, 'mode_switch_logs append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_mode_switch_logs_no_delete
        BEFORE DELETE ON mode_switch_logs
        BEGIN
            SELECT RAISE(ABORT, 'mode_switch_logs append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tester_tokens (
            id                       TEXT PRIMARY KEY,
            token_hash               TEXT NOT NULL,
            tester_user_id           INTEGER,
            mode_scope_json          TEXT NOT NULL DEFAULT '["test","internal_test"]',
            route_scope_json         TEXT NOT NULL DEFAULT '[]',
            method_scope_json        TEXT NOT NULL DEFAULT '["GET","POST","PUT","PATCH","DELETE"]',
            allowed_features_json    TEXT NOT NULL DEFAULT '[]',
            allowed_routes_json      TEXT NOT NULL DEFAULT '[]',
            expires_at               TEXT NOT NULL,
            issued_at                TEXT,
            nonce                    TEXT,
            max_requests_per_minute  INTEGER NOT NULL DEFAULT 60,
            can_modify_own_role      INTEGER NOT NULL DEFAULT 0,
            can_modify_own_points    INTEGER NOT NULL DEFAULT 0,
            can_run_security_tests   INTEGER NOT NULL DEFAULT 0,
            created_by               INTEGER NOT NULL,
            created_at               TEXT NOT NULL,
            revoked_at               TEXT,
            hmac_signature           TEXT,
            key_version              TEXT
        )
        """
    )
    tester_token_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tester_tokens)").fetchall()}
    for col, ddl in (
        ("mode_scope_json", "ALTER TABLE tester_tokens ADD COLUMN mode_scope_json TEXT NOT NULL DEFAULT '[\"test\",\"internal_test\"]'"),
        ("route_scope_json", "ALTER TABLE tester_tokens ADD COLUMN route_scope_json TEXT NOT NULL DEFAULT '[]'"),
        ("method_scope_json", "ALTER TABLE tester_tokens ADD COLUMN method_scope_json TEXT NOT NULL DEFAULT '[\"GET\",\"POST\",\"PUT\",\"PATCH\",\"DELETE\"]'"),
        ("issued_at", "ALTER TABLE tester_tokens ADD COLUMN issued_at TEXT"),
        ("nonce", "ALTER TABLE tester_tokens ADD COLUMN nonce TEXT"),
        ("hmac_signature", "ALTER TABLE tester_tokens ADD COLUMN hmac_signature TEXT"),
        ("key_version", "ALTER TABLE tester_tokens ADD COLUMN key_version TEXT"),
    ):
        if col not in tester_token_cols:
            conn.execute(ddl)
    conn.execute("UPDATE tester_tokens SET route_scope_json=allowed_routes_json WHERE route_scope_json IS NULL OR route_scope_json='' OR route_scope_json='[]'")
    conn.execute("UPDATE tester_tokens SET issued_at=created_at WHERE issued_at IS NULL OR issued_at=''")
    conn.execute("UPDATE tester_tokens SET nonce=id WHERE nonce IS NULL OR nonce=''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tester_token_request_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id     TEXT NOT NULL,
            route        TEXT NOT NULL,
            ip_address   TEXT,
            created_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_shadow_roles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tester_user_id  INTEGER NOT NULL,
            original_role   TEXT,
            shadow_role     TEXT NOT NULL,
            token_id        TEXT,
            created_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_shadow_wallets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tester_user_id  INTEGER NOT NULL,
            user_id         INTEGER,
            balance_points  INTEGER NOT NULL DEFAULT 0,
            frozen_points   INTEGER NOT NULL DEFAULT 0,
            total_points_earned INTEGER NOT NULL DEFAULT 0,
            total_points_spent INTEGER NOT NULL DEFAULT 0,
            wallet_status   TEXT NOT NULL DEFAULT 'active',
            risk_level      TEXT NOT NULL DEFAULT 'normal',
            token_id        TEXT,
            created_at      TEXT,
            updated_at      TEXT NOT NULL
        )
        """
    )
    shadow_wallet_cols = {row["name"] for row in conn.execute("PRAGMA table_info(test_shadow_wallets)").fetchall()}
    for col, ddl in (
        ("user_id", "ALTER TABLE test_shadow_wallets ADD COLUMN user_id INTEGER"),
        ("frozen_points", "ALTER TABLE test_shadow_wallets ADD COLUMN frozen_points INTEGER NOT NULL DEFAULT 0"),
        ("total_points_earned", "ALTER TABLE test_shadow_wallets ADD COLUMN total_points_earned INTEGER NOT NULL DEFAULT 0"),
        ("total_points_spent", "ALTER TABLE test_shadow_wallets ADD COLUMN total_points_spent INTEGER NOT NULL DEFAULT 0"),
        ("wallet_status", "ALTER TABLE test_shadow_wallets ADD COLUMN wallet_status TEXT NOT NULL DEFAULT 'active'"),
        ("risk_level", "ALTER TABLE test_shadow_wallets ADD COLUMN risk_level TEXT NOT NULL DEFAULT 'normal'"),
        ("created_at", "ALTER TABLE test_shadow_wallets ADD COLUMN created_at TEXT"),
    ):
        if col not in shadow_wallet_cols:
            conn.execute(ddl)
    conn.execute("UPDATE test_shadow_wallets SET user_id=tester_user_id WHERE user_id IS NULL")
    if "soft_balance" in shadow_wallet_cols or "hard_balance" in shadow_wallet_cols:
        conn.execute(
            """
            UPDATE test_shadow_wallets
            SET balance_points=
                CASE
                    WHEN COALESCE(balance_points, 0) != 0 THEN balance_points
                    ELSE COALESCE(soft_balance, 0) + COALESCE(hard_balance, 0)
                END
            """
        )
    if "soft_frozen" in shadow_wallet_cols or "hard_frozen" in shadow_wallet_cols:
        conn.execute(
            """
            UPDATE test_shadow_wallets
            SET frozen_points=
                CASE
                    WHEN COALESCE(frozen_points, 0) != 0 THEN frozen_points
                    ELSE COALESCE(soft_frozen, 0) + COALESCE(hard_frozen, 0)
                END
            """
        )
    if "total_soft_earned" in shadow_wallet_cols or "total_hard_earned" in shadow_wallet_cols:
        conn.execute(
            """
            UPDATE test_shadow_wallets
            SET total_points_earned=
                CASE
                    WHEN COALESCE(total_points_earned, 0) != 0 THEN total_points_earned
                    ELSE COALESCE(total_soft_earned, 0) + COALESCE(total_hard_earned, 0)
                END
            """
        )
    if "total_soft_spent" in shadow_wallet_cols or "total_hard_spent" in shadow_wallet_cols:
        conn.execute(
            """
            UPDATE test_shadow_wallets
            SET total_points_spent=
                CASE
                    WHEN COALESCE(total_points_spent, 0) != 0 THEN total_points_spent
                    ELSE COALESCE(total_soft_spent, 0) + COALESCE(total_hard_spent, 0)
                END
            """
        )
    conn.execute("UPDATE test_shadow_wallets SET created_at=updated_at WHERE created_at IS NULL OR created_at=''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_shadow_transactions (
            id              TEXT PRIMARY KEY,
            tester_user_id  INTEGER NOT NULL,
            delta_points    INTEGER NOT NULL,
            reason          TEXT,
            token_id        TEXT,
            created_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_chain_blocks (
            id              TEXT PRIMARY KEY,
            prev_hash       TEXT,
            block_hash      TEXT NOT NULL,
            transactions_json TEXT NOT NULL DEFAULT '[]',
            created_at      TEXT NOT NULL
        )
        """
    )
    # SERVER_MODE_V2_IMPLEMENTATION_PLAN.md Phase 4: shadow tables
    # mirroring the production trading_orders / trading_spot_positions /
    # points_ledger schemas. Phase 5 (trading dual-engine) will route
    # internal_test orders / fills / ledger entries here. Schemas are
    # intentionally simplified vs production — shadow only captures the
    # fields needed for matching + isolation verification, not every
    # production refinement (futures / margin / fills detail). Phase 5
    # may extend these as it lands the routing logic.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_shadow_orders (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            order_uuid          TEXT NOT NULL UNIQUE,
            tester_user_id      INTEGER NOT NULL,
            user_id             INTEGER,
            market_symbol       TEXT NOT NULL,
            side                TEXT NOT NULL,
            order_type          TEXT NOT NULL,
            funding_mode        TEXT NOT NULL DEFAULT 'points_chain',
            source_wallet_address TEXT NOT NULL DEFAULT '',
            execution_mode      TEXT NOT NULL DEFAULT 'house_counterparty',
            quantity_units      INTEGER NOT NULL CHECK (quantity_units > 0),
            limit_price_points  INTEGER,
            execution_price_points INTEGER,
            status              TEXT NOT NULL DEFAULT 'open',
            frozen_points       INTEGER NOT NULL DEFAULT 0,
            trial_frozen_points INTEGER NOT NULL DEFAULT 0,
            chain_frozen_points INTEGER NOT NULL DEFAULT 0,
            fee_points          INTEGER NOT NULL DEFAULT 0,
            fee_micropoints     INTEGER NOT NULL DEFAULT 0,
            filled_quantity_units INTEGER NOT NULL DEFAULT 0,
            reason              TEXT,
            token_id            TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            CHECK (side IN ('buy', 'sell')),
            CHECK (order_type IN ('market', 'limit')),
            CHECK (status IN ('open', 'partially_filled', 'filled', 'cancelled', 'rejected')),
            CHECK (execution_mode IN ('house_counterparty', 'pvp_matching', 'hybrid_liquidity'))
        )
        """
    )
    shadow_order_cols = {row["name"] for row in conn.execute("PRAGMA table_info(test_shadow_orders)").fetchall()}
    for col, ddl in (
        ("user_id", "ALTER TABLE test_shadow_orders ADD COLUMN user_id INTEGER"),
        ("funding_mode", "ALTER TABLE test_shadow_orders ADD COLUMN funding_mode TEXT NOT NULL DEFAULT 'points_chain'"),
        ("source_wallet_address", "ALTER TABLE test_shadow_orders ADD COLUMN source_wallet_address TEXT NOT NULL DEFAULT ''"),
        ("execution_mode", "ALTER TABLE test_shadow_orders ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'house_counterparty'"),
        ("trial_frozen_points", "ALTER TABLE test_shadow_orders ADD COLUMN trial_frozen_points INTEGER NOT NULL DEFAULT 0"),
        ("chain_frozen_points", "ALTER TABLE test_shadow_orders ADD COLUMN chain_frozen_points INTEGER NOT NULL DEFAULT 0"),
        ("fee_micropoints", "ALTER TABLE test_shadow_orders ADD COLUMN fee_micropoints INTEGER NOT NULL DEFAULT 0"),
        ("stop_loss_percent", "ALTER TABLE test_shadow_orders ADD COLUMN stop_loss_percent REAL"),
        ("take_profit_percent", "ALTER TABLE test_shadow_orders ADD COLUMN take_profit_percent REAL"),
    ):
        if col not in shadow_order_cols:
            conn.execute(ddl)
    conn.execute("UPDATE test_shadow_orders SET user_id=tester_user_id WHERE user_id IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_orders_tester ON test_shadow_orders(tester_user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_orders_market_status ON test_shadow_orders(market_symbol, status)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_shadow_positions (
            user_id              INTEGER,
            tester_user_id      INTEGER NOT NULL,
            market_symbol       TEXT NOT NULL,
            quantity_units      INTEGER NOT NULL DEFAULT 0 CHECK (quantity_units >= 0),
            locked_quantity_units INTEGER NOT NULL DEFAULT 0 CHECK (locked_quantity_units >= 0),
            avg_cost_points     INTEGER NOT NULL DEFAULT 0,
            fee_carry_micropoints INTEGER NOT NULL DEFAULT 0,
            token_id            TEXT,
            updated_at          TEXT NOT NULL,
            PRIMARY KEY (tester_user_id, market_symbol)
        )
        """
    )
    shadow_position_cols = {row["name"] for row in conn.execute("PRAGMA table_info(test_shadow_positions)").fetchall()}
    if "user_id" not in shadow_position_cols:
        conn.execute("ALTER TABLE test_shadow_positions ADD COLUMN user_id INTEGER")
    if "fee_carry_micropoints" not in shadow_position_cols:
        conn.execute("ALTER TABLE test_shadow_positions ADD COLUMN fee_carry_micropoints INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE test_shadow_positions SET user_id=tester_user_id WHERE user_id IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_positions_tester ON test_shadow_positions(tester_user_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_shadow_margin_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_uuid TEXT NOT NULL UNIQUE,
            tester_user_id INTEGER NOT NULL,
            user_id INTEGER,
            market_symbol TEXT NOT NULL,
            position_type TEXT NOT NULL,
            quantity_units INTEGER NOT NULL CHECK (quantity_units > 0),
            entry_price_points INTEGER NOT NULL CHECK (entry_price_points > 0),
            principal_points INTEGER NOT NULL DEFAULT 0 CHECK (principal_points >= 0),
            collateral_points INTEGER NOT NULL CHECK (collateral_points > 0),
            open_fee_points INTEGER NOT NULL DEFAULT 0,
            close_fee_points INTEGER NOT NULL DEFAULT 0,
            open_fee_micropoints INTEGER NOT NULL DEFAULT 0,
            close_fee_micropoints INTEGER NOT NULL DEFAULT 0,
            exit_price_points INTEGER,
            realized_pnl_points INTEGER NOT NULL DEFAULT 0,
            interest_percent_daily REAL NOT NULL DEFAULT 0,
            interest_points INTEGER NOT NULL DEFAULT 0,
            interest_paid_points INTEGER NOT NULL DEFAULT 0,
            interest_accrued_hours INTEGER NOT NULL DEFAULT 0,
            interest_carry_micropoints INTEGER NOT NULL DEFAULT 0,
            interest_interval_hours INTEGER NOT NULL DEFAULT 1,
            interest_minimum_hours INTEGER NOT NULL DEFAULT 1,
            borrowed_asset_symbol TEXT NOT NULL DEFAULT 'POINTS',
            status TEXT NOT NULL DEFAULT 'open',
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            updated_at TEXT NOT NULL,
            collateral_trial_points INTEGER NOT NULL DEFAULT 0 CHECK (collateral_trial_points >= 0),
            collateral_chain_points INTEGER NOT NULL DEFAULT 0 CHECK (collateral_chain_points >= 0),
            open_fee_trial_points INTEGER NOT NULL DEFAULT 0 CHECK (open_fee_trial_points >= 0),
            open_fee_chain_points INTEGER NOT NULL DEFAULT 0 CHECK (open_fee_chain_points >= 0),
            CHECK (position_type IN ('margin_long', 'short')),
            CHECK (status IN ('open', 'closed', 'liquidated'))
        )
        """
    )
    shadow_margin_cols = {row["name"] for row in conn.execute("PRAGMA table_info(test_shadow_margin_positions)").fetchall()}
    if "user_id" not in shadow_margin_cols:
        conn.execute("ALTER TABLE test_shadow_margin_positions ADD COLUMN user_id INTEGER")
    if "stop_loss_percent" not in shadow_margin_cols:
        conn.execute("ALTER TABLE test_shadow_margin_positions ADD COLUMN stop_loss_percent REAL")
    if "take_profit_percent" not in shadow_margin_cols:
        conn.execute("ALTER TABLE test_shadow_margin_positions ADD COLUMN take_profit_percent REAL")
    if "open_fee_micropoints" not in shadow_margin_cols:
        conn.execute("ALTER TABLE test_shadow_margin_positions ADD COLUMN open_fee_micropoints INTEGER NOT NULL DEFAULT 0")
    if "close_fee_micropoints" not in shadow_margin_cols:
        conn.execute("ALTER TABLE test_shadow_margin_positions ADD COLUMN close_fee_micropoints INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE test_shadow_margin_positions SET user_id=tester_user_id WHERE user_id IS NULL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_margin_positions_tester_status ON test_shadow_margin_positions(tester_user_id, status, market_symbol)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_shadow_ledger (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ledger_uuid         TEXT NOT NULL UNIQUE,
            tester_user_id      INTEGER NOT NULL,
            user_id             INTEGER,
            public_account_id   TEXT,
            currency_type       TEXT NOT NULL,
            direction           TEXT NOT NULL,
            amount              INTEGER NOT NULL CHECK (amount > 0),
            balance_before      INTEGER NOT NULL,
            balance_after       INTEGER NOT NULL,
            action_type         TEXT NOT NULL,
            reference_type      TEXT,
            reference_id        TEXT,
            idempotency_key     TEXT UNIQUE,
            reason              TEXT,
            public_metadata_json TEXT,
            private_metadata_json TEXT,
            sensitive_metadata_encrypted TEXT,
            metadata_hash       TEXT,
            previous_ledger_hash TEXT,
            ledger_hash         TEXT,
            risk_flag           TEXT DEFAULT 'none',
            risk_score          INTEGER NOT NULL DEFAULT 0,
            created_by          INTEGER,
            created_by_role     TEXT,
            status              TEXT NOT NULL DEFAULT 'confirmed',
            token_id            TEXT,
            created_at          TEXT NOT NULL,
            CHECK (currency_type IN ('soft', 'hard')),
            CHECK (direction IN ('credit', 'debit', 'freeze', 'unfreeze', 'reverse', 'transfer_in', 'transfer_out'))
        )
        """
    )
    shadow_ledger_cols = {row["name"] for row in conn.execute("PRAGMA table_info(test_shadow_ledger)").fetchall()}
    for col, ddl in (
        ("user_id", "ALTER TABLE test_shadow_ledger ADD COLUMN user_id INTEGER"),
        ("public_account_id", "ALTER TABLE test_shadow_ledger ADD COLUMN public_account_id TEXT"),
        ("idempotency_key", "ALTER TABLE test_shadow_ledger ADD COLUMN idempotency_key TEXT UNIQUE"),
        ("public_metadata_json", "ALTER TABLE test_shadow_ledger ADD COLUMN public_metadata_json TEXT"),
        ("private_metadata_json", "ALTER TABLE test_shadow_ledger ADD COLUMN private_metadata_json TEXT"),
        ("sensitive_metadata_encrypted", "ALTER TABLE test_shadow_ledger ADD COLUMN sensitive_metadata_encrypted TEXT"),
        ("metadata_hash", "ALTER TABLE test_shadow_ledger ADD COLUMN metadata_hash TEXT"),
        ("previous_ledger_hash", "ALTER TABLE test_shadow_ledger ADD COLUMN previous_ledger_hash TEXT"),
        ("ledger_hash", "ALTER TABLE test_shadow_ledger ADD COLUMN ledger_hash TEXT"),
        ("risk_flag", "ALTER TABLE test_shadow_ledger ADD COLUMN risk_flag TEXT DEFAULT 'none'"),
        ("risk_score", "ALTER TABLE test_shadow_ledger ADD COLUMN risk_score INTEGER NOT NULL DEFAULT 0"),
        ("created_by", "ALTER TABLE test_shadow_ledger ADD COLUMN created_by INTEGER"),
        ("created_by_role", "ALTER TABLE test_shadow_ledger ADD COLUMN created_by_role TEXT"),
        ("status", "ALTER TABLE test_shadow_ledger ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'"),
    ):
        if col not in shadow_ledger_cols:
            conn.execute(ddl)
    conn.execute("UPDATE test_shadow_ledger SET user_id=tester_user_id WHERE user_id IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_ledger_tester ON test_shadow_ledger(tester_user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_ledger_action ON test_shadow_ledger(action_type, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS production_entry_reports (
            id                      TEXT PRIMARY KEY,
            report_type             TEXT NOT NULL,
            report_hash             TEXT NOT NULL,
            target_commit           TEXT,
            target_branch           TEXT,
            server_mode             TEXT,
            test_result             TEXT,
            pass                    INTEGER NOT NULL DEFAULT 0,
            critical_findings_count INTEGER NOT NULL DEFAULT 0,
            high_findings_count     INTEGER NOT NULL DEFAULT 0,
            unresolved_findings_json TEXT NOT NULL DEFAULT '[]',
            tester                  TEXT,
            signature               TEXT,
            raw_report_json         TEXT NOT NULL DEFAULT '{}',
            report_source           TEXT NOT NULL DEFAULT 'manual_upload',
            trust_level             TEXT NOT NULL DEFAULT 'unverified',
            key_version             TEXT NOT NULL DEFAULT '',
            verified_at             TEXT NOT NULL DEFAULT '',
            created_at              TEXT NOT NULL
        )
        """
    )
    production_report_cols = {row["name"] for row in conn.execute("PRAGMA table_info(production_entry_reports)").fetchall()}
    for col, ddl in (
        ("raw_report_json", "ALTER TABLE production_entry_reports ADD COLUMN raw_report_json TEXT NOT NULL DEFAULT '{}'"),
        ("report_source", "ALTER TABLE production_entry_reports ADD COLUMN report_source TEXT NOT NULL DEFAULT 'manual_upload'"),
        ("trust_level", "ALTER TABLE production_entry_reports ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'unverified'"),
        ("key_version", "ALTER TABLE production_entry_reports ADD COLUMN key_version TEXT NOT NULL DEFAULT ''"),
        ("verified_at", "ALTER TABLE production_entry_reports ADD COLUMN verified_at TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in production_report_cols:
            conn.execute(ddl)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_reports (
            id                  TEXT PRIMARY KEY,
            status              TEXT NOT NULL,
            trigger_type        TEXT NOT NULL,
            reason              TEXT,
            entered_by          INTEGER,
            entered_at          TEXT NOT NULL,
            resolved_by         INTEGER,
            resolved_at         TEXT,
            resolution_notes    TEXT,
            verification_json   TEXT NOT NULL DEFAULT '{}',
            previous_mode       TEXT,
            previous_mode_json  TEXT NOT NULL DEFAULT '{}',
            previous_settings_json TEXT NOT NULL DEFAULT '{}',
            previous_settings_hash TEXT NOT NULL DEFAULT '',
            checkpoint_id       TEXT,
            snapshot_id         TEXT,
            entry_state         TEXT NOT NULL DEFAULT 'active',
            entry_error         TEXT NOT NULL DEFAULT '',
            restore_result_json TEXT NOT NULL DEFAULT '{}',
            resolution_verification_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    incident_report_cols = {row["name"] for row in conn.execute("PRAGMA table_info(incident_reports)").fetchall()}
    for col, ddl in (
        ("previous_mode", "ALTER TABLE incident_reports ADD COLUMN previous_mode TEXT"),
        ("previous_mode_json", "ALTER TABLE incident_reports ADD COLUMN previous_mode_json TEXT NOT NULL DEFAULT '{}'"),
        ("previous_settings_json", "ALTER TABLE incident_reports ADD COLUMN previous_settings_json TEXT NOT NULL DEFAULT '{}'"),
        ("previous_settings_hash", "ALTER TABLE incident_reports ADD COLUMN previous_settings_hash TEXT NOT NULL DEFAULT ''"),
        ("checkpoint_id", "ALTER TABLE incident_reports ADD COLUMN checkpoint_id TEXT"),
        ("snapshot_id", "ALTER TABLE incident_reports ADD COLUMN snapshot_id TEXT"),
        ("entry_state", "ALTER TABLE incident_reports ADD COLUMN entry_state TEXT NOT NULL DEFAULT 'active'"),
        ("entry_error", "ALTER TABLE incident_reports ADD COLUMN entry_error TEXT NOT NULL DEFAULT ''"),
        ("restore_result_json", "ALTER TABLE incident_reports ADD COLUMN restore_result_json TEXT NOT NULL DEFAULT '{}'"),
        ("resolution_verification_json", "ALTER TABLE incident_reports ADD COLUMN resolution_verification_json TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in incident_report_cols:
            conn.execute(ddl)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS security_profiles (
            name            TEXT PRIMARY KEY,
            label           TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            settings_json   TEXT NOT NULL DEFAULT '{}',
            thresholds_json TEXT NOT NULL DEFAULT '{}',
            is_builtin      INTEGER NOT NULL DEFAULT 0,
            created_by      INTEGER,
            updated_by      INTEGER,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )
    now = datetime.now().isoformat()
    for name, profile in BUILTIN_SECURITY_PROFILES.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO security_profiles
            (name, label, description, settings_json, thresholds_json, is_builtin, created_by, updated_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?)
            """,
            (
                name,
                profile["label"],
                profile["description"],
                json.dumps(profile["settings"], ensure_ascii=False, sort_keys=True),
                json.dumps(profile["thresholds"], ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE security_profiles
            SET label=?,
                description=?,
                settings_json=?,
                thresholds_json=?,
                is_builtin=1,
                updated_at=?
            WHERE name=? AND is_builtin=1
            """,
            (
                profile["label"],
                profile["description"],
                json.dumps(profile["settings"], ensure_ascii=False, sort_keys=True),
                json.dumps(profile["thresholds"], ensure_ascii=False, sort_keys=True),
                now,
                name,
            ),
        )
    # Remove the old builtin alias row so admin UIs only list the canonical mode.
    conn.execute("DELETE FROM security_profiles WHERE name='preprod' AND is_builtin=1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_type_status ON snapshots(type, status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_restore_events_snapshot ON snapshot_restore_events(snapshot_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mode_switch_logs_created ON mode_switch_logs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mode_switch_logs_hash ON mode_switch_logs(row_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tester_token_audit_token_time ON tester_token_audit(token_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_security_keys_purpose_status ON security_keys(purpose, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_server_checkpoints_target ON server_checkpoints(target_mode, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_production_reports_type ON production_entry_reports(report_type, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tester_token_request_log_token_time ON tester_token_request_log(token_id, created_at)")


def ensure_control_db_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS server_modes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_mode TEXT NOT NULL,
            previous_mode TEXT,
            active_snapshot_id TEXT,
            checkpoint_id TEXT,
            mode_changed_by INTEGER,
            mode_changed_at TEXT,
            notes TEXT,
            reason TEXT,
            config_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    server_mode_cols = {row["name"] for row in conn.execute("PRAGMA table_info(server_modes)").fetchall()}
    for col, ddl in (
        ("previous_mode", "ALTER TABLE server_modes ADD COLUMN previous_mode TEXT"),
        ("active_snapshot_id", "ALTER TABLE server_modes ADD COLUMN active_snapshot_id TEXT"),
        ("checkpoint_id", "ALTER TABLE server_modes ADD COLUMN checkpoint_id TEXT"),
        ("mode_changed_by", "ALTER TABLE server_modes ADD COLUMN mode_changed_by INTEGER"),
        ("mode_changed_at", "ALTER TABLE server_modes ADD COLUMN mode_changed_at TEXT"),
        ("notes", "ALTER TABLE server_modes ADD COLUMN notes TEXT"),
        ("reason", "ALTER TABLE server_modes ADD COLUMN reason TEXT"),
        ("config_json", "ALTER TABLE server_modes ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in server_mode_cols:
            conn.execute(ddl)
    conn.execute(
        "INSERT OR IGNORE INTO server_modes "
        "(id, current_mode, previous_mode, active_snapshot_id, checkpoint_id, mode_changed_by, mode_changed_at, notes, reason, config_json) "
        "VALUES (1, 'dev_ready', NULL, NULL, NULL, NULL, ?, '', '', '{}')",
        (datetime.now().isoformat(),),
    )
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        """
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
        )
        """
    )
    mode_log_cols = {row["name"] for row in conn.execute("PRAGMA table_info(mode_switch_logs)").fetchall()}
    for col, ddl in (
        ("event_uuid", "ALTER TABLE mode_switch_logs ADD COLUMN event_uuid TEXT"),
        ("actor_id", "ALTER TABLE mode_switch_logs ADD COLUMN actor_id INTEGER"),
        ("actor_role", "ALTER TABLE mode_switch_logs ADD COLUMN actor_role TEXT"),
        ("source_ip", "ALTER TABLE mode_switch_logs ADD COLUMN source_ip TEXT"),
        ("user_agent", "ALTER TABLE mode_switch_logs ADD COLUMN user_agent TEXT"),
        ("request_id", "ALTER TABLE mode_switch_logs ADD COLUMN request_id TEXT"),
        ("prev_hash", "ALTER TABLE mode_switch_logs ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''"),
        ("row_hash", "ALTER TABLE mode_switch_logs ADD COLUMN row_hash TEXT NOT NULL DEFAULT ''"),
        ("server_boot_id", "ALTER TABLE mode_switch_logs ADD COLUMN server_boot_id TEXT"),
        ("hmac_signature", "ALTER TABLE mode_switch_logs ADD COLUMN hmac_signature TEXT"),
        ("key_version", "ALTER TABLE mode_switch_logs ADD COLUMN key_version TEXT"),
    ):
        if col not in mode_log_cols:
            conn.execute(ddl)
    conn.execute("UPDATE mode_switch_logs SET event_uuid=id WHERE event_uuid IS NULL OR event_uuid=''")
    conn.execute("UPDATE mode_switch_logs SET actor_id=actor_user_id WHERE actor_id IS NULL")
    conn.execute("UPDATE mode_switch_logs SET actor_role='' WHERE actor_role IS NULL")
    conn.execute("UPDATE mode_switch_logs SET source_ip='' WHERE source_ip IS NULL")
    conn.execute("UPDATE mode_switch_logs SET user_agent='' WHERE user_agent IS NULL")
    conn.execute("UPDATE mode_switch_logs SET request_id='' WHERE request_id IS NULL")
    conn.execute("UPDATE mode_switch_logs SET server_boot_id='' WHERE server_boot_id IS NULL")
    conn.execute("UPDATE mode_switch_logs SET key_version='' WHERE key_version IS NULL")
    _backfill_mode_switch_log_hashes(conn)
    conn.execute("DROP TRIGGER IF EXISTS trg_mode_switch_logs_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_mode_switch_logs_no_delete")
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_mode_switch_logs_no_update
        BEFORE UPDATE ON mode_switch_logs
        BEGIN
            SELECT RAISE(ABORT, 'mode_switch_logs append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_mode_switch_logs_no_delete
        BEFORE DELETE ON mode_switch_logs
        BEGIN
            SELECT RAISE(ABORT, 'mode_switch_logs append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS security_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL,
            key_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            rotated_at TEXT,
            disabled_at TEXT,
            status TEXT NOT NULL,
            UNIQUE(purpose, key_version)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS production_entry_reports (
            id                       TEXT PRIMARY KEY,
            report_type              TEXT NOT NULL,
            report_hash              TEXT NOT NULL,
            target_commit            TEXT,
            target_branch            TEXT,
            server_mode              TEXT,
            test_result              TEXT,
            pass                     INTEGER NOT NULL DEFAULT 0,
            critical_findings_count  INTEGER NOT NULL DEFAULT 0,
            high_findings_count      INTEGER NOT NULL DEFAULT 0,
            unresolved_findings_json TEXT NOT NULL DEFAULT '[]',
            tester                   TEXT,
            signature                TEXT,
            raw_report_json          TEXT NOT NULL DEFAULT '{}',
            report_source            TEXT NOT NULL DEFAULT 'manual_upload',
            trust_level              TEXT NOT NULL DEFAULT 'unverified',
            key_version              TEXT NOT NULL DEFAULT '',
            verified_at              TEXT NOT NULL DEFAULT '',
            created_at               TEXT NOT NULL
        )
        """
    )
    production_report_cols = {row["name"] for row in conn.execute("PRAGMA table_info(production_entry_reports)").fetchall()}
    for col, ddl in (
        ("raw_report_json", "ALTER TABLE production_entry_reports ADD COLUMN raw_report_json TEXT NOT NULL DEFAULT '{}'"),
        ("report_source", "ALTER TABLE production_entry_reports ADD COLUMN report_source TEXT NOT NULL DEFAULT 'manual_upload'"),
        ("trust_level", "ALTER TABLE production_entry_reports ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'unverified'"),
        ("key_version", "ALTER TABLE production_entry_reports ADD COLUMN key_version TEXT NOT NULL DEFAULT ''"),
        ("verified_at", "ALTER TABLE production_entry_reports ADD COLUMN verified_at TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in production_report_cols:
            conn.execute(ddl)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_reports (
            id                TEXT PRIMARY KEY,
            status            TEXT NOT NULL,
            trigger_type      TEXT NOT NULL,
            reason            TEXT,
            entered_by        INTEGER,
            entered_at        TEXT NOT NULL,
            resolved_by       INTEGER,
            resolved_at       TEXT,
            resolution_notes  TEXT,
            verification_json TEXT NOT NULL DEFAULT '{}',
            previous_mode     TEXT,
            previous_mode_json TEXT NOT NULL DEFAULT '{}',
            previous_settings_json TEXT NOT NULL DEFAULT '{}',
            previous_settings_hash TEXT NOT NULL DEFAULT '',
            checkpoint_id     TEXT,
            snapshot_id       TEXT,
            entry_state       TEXT NOT NULL DEFAULT 'active',
            entry_error       TEXT NOT NULL DEFAULT '',
            restore_result_json TEXT NOT NULL DEFAULT '{}',
            resolution_verification_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    incident_report_cols = {row["name"] for row in conn.execute("PRAGMA table_info(incident_reports)").fetchall()}
    for col, ddl in (
        ("previous_mode", "ALTER TABLE incident_reports ADD COLUMN previous_mode TEXT"),
        ("previous_mode_json", "ALTER TABLE incident_reports ADD COLUMN previous_mode_json TEXT NOT NULL DEFAULT '{}'"),
        ("previous_settings_json", "ALTER TABLE incident_reports ADD COLUMN previous_settings_json TEXT NOT NULL DEFAULT '{}'"),
        ("previous_settings_hash", "ALTER TABLE incident_reports ADD COLUMN previous_settings_hash TEXT NOT NULL DEFAULT ''"),
        ("checkpoint_id", "ALTER TABLE incident_reports ADD COLUMN checkpoint_id TEXT"),
        ("snapshot_id", "ALTER TABLE incident_reports ADD COLUMN snapshot_id TEXT"),
        ("entry_state", "ALTER TABLE incident_reports ADD COLUMN entry_state TEXT NOT NULL DEFAULT 'active'"),
        ("entry_error", "ALTER TABLE incident_reports ADD COLUMN entry_error TEXT NOT NULL DEFAULT ''"),
        ("restore_result_json", "ALTER TABLE incident_reports ADD COLUMN restore_result_json TEXT NOT NULL DEFAULT '{}'"),
        ("resolution_verification_json", "ALTER TABLE incident_reports ADD COLUMN resolution_verification_json TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in incident_report_cols:
            conn.execute(ddl)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS security_profiles (
            name            TEXT PRIMARY KEY,
            label           TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            settings_json   TEXT NOT NULL DEFAULT '{}',
            thresholds_json TEXT NOT NULL DEFAULT '{}',
            is_builtin      INTEGER NOT NULL DEFAULT 0,
            created_by      INTEGER,
            updated_by      INTEGER,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )
    now = datetime.now().isoformat()
    for name, profile in BUILTIN_SECURITY_PROFILES.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO security_profiles
            (name, label, description, settings_json, thresholds_json, is_builtin, created_by, updated_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?)
            """,
            (
                name,
                profile["label"],
                profile["description"],
                json.dumps(profile["settings"], ensure_ascii=False, sort_keys=True),
                json.dumps(profile["thresholds"], ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE security_profiles
            SET label=?,
                description=?,
                settings_json=?,
                thresholds_json=?,
                is_builtin=1,
                updated_at=?
            WHERE name=? AND is_builtin=1
            """,
            (
                profile["label"],
                profile["description"],
                json.dumps(profile["settings"], ensure_ascii=False, sort_keys=True),
                json.dumps(profile["thresholds"], ensure_ascii=False, sort_keys=True),
                now,
                name,
            ),
        )
    conn.execute("DELETE FROM security_profiles WHERE name='preprod' AND is_builtin=1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mode_switch_logs_created ON mode_switch_logs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mode_switch_logs_hash ON mode_switch_logs(row_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_security_keys_purpose_status ON security_keys(purpose, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_server_checkpoints_target ON server_checkpoints(target_mode, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_production_reports_type ON production_entry_reports(report_type, created_at)")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_snapshot_id(snapshot_id):
    return isinstance(snapshot_id, str) and SNAPSHOT_ID_RE.fullmatch(snapshot_id) is not None


def _safe_relative_tarinfo(tarinfo):
    name = tarinfo.name
    if not name or name.startswith("/") or ".." in Path(name).parts:
        return False
    if tarinfo.issym() or tarinfo.islnk() or tarinfo.isdev():
        return False
    return True


def _safe_extract_tar(archive_path, target_dir):
    target = Path(target_dir).resolve()
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not _safe_relative_tarinfo(member):
                raise ValueError(f"unsafe archive member: {member.name}")
            final = (target / member.name).resolve()
            if target not in final.parents and final != target:
                raise ValueError(f"archive member escapes target: {member.name}")
        # Python 3.14 changes the implicit extraction filter.  We already
        # validate every member above; on Python 3.12+ also request the
        # standard data-only filter explicitly so future interpreter upgrades
        # cannot change restore behavior or re-enable special members.
        if sys.version_info >= (3, 12):
            tar.extractall(target, filter="data")
        else:
            tar.extractall(target)


def _parse_daily_snapshot_time(value):
    text = str(value or "03:00").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return 3, 0, "03:00"
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return 3, 0, "03:00"
    return hour, minute, f"{hour:02d}:{minute:02d}"
