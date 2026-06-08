"""Domain database inventory and non-destructive split helpers.

The application still has legacy code paths that expect many tables to be
reachable from the primary connection.  This module provides the canonical
domain table map and a safe export path so operators can split/copy high-risk
domains first, verify table hashes, then migrate runtime routing in smaller
steps.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DOMAIN_DATABASES = {
    "storage": {
        "filename": "storage_catalog.db",
        "description": "Cloud-drive, E2EE file catalog, shares, albums, video/media catalog.",
    },
    "points_chain": {
        "filename": "points_chain.db",
        "description": "PointsChain wallets, append-only ledgers, bridge events, governance, economy snapshots.",
    },
    "trading": {
        "filename": "trading.db",
        "description": "Exchange orders, fills, positions, bots, reserve pool, background trading jobs.",
    },
    "jobs": {
        "filename": "jobs.db",
        "description": "Background job center state and event timeline.",
    },
    "social": {
        "filename": "social.db",
        "description": "Chat, forum/community, notifications, reports, friendships.",
    },
    "governance": {
        "filename": "governance.db",
        "description": "Appeals, sanctions, moderation, member-level policy records.",
    },
    "comfyui": {
        "filename": "comfyui.db",
        "description": "ComfyUI workflow presets, layout versions, generation history.",
    },
    "games": {
        "filename": "games.db",
        "description": "Game scores, multiplayer room state, invitations, rewards.",
    },
    "core": {
        "filename": "core.db",
        "description": "Identity, settings, schema migrations, snapshots, integrity records.",
    },
}


DOMAIN_TABLES = {
    "storage": {
        "uploaded_files",
        "encrypted_file_keys",
        "file_scan_results",
        "file_access_logs",
        "file_type_policies",
        "cloud_drive_security_policies",
        "cloud_file_refs",
        "file_access_grants",
        "cloud_resumable_upload_sessions",
        "user_storage",
        "storage_files",
        "storage_folders",
        "storage_quota_log",
        "storage_quota_overrides",
        "storage_quota_purchases",
        "storage_quota_reduction_notices",
        "storage_share_links",
        "albums",
        "album_files",
        "album_share_links",
        "videos",
        "video_views",
        "video_likes",
        "video_comments",
        "video_danmaku",
        "video_tips",
        "video_share_links",
        "media_stream_assets",
        "media_stream_variants",
        "media_stream_segments",
        "media_stream_jobs",
        "media_e2ee_stream_v2_assets",
        "media_e2ee_stream_v2_variants",
        "announcement_attachment_requests",
    },
    "points_chain": {
        "economy_price_catalog",
        "points_wallets",
        "points_ledger",
        "points_rules",
        "points_pending_rewards",
        "points_chain_blocks",
        "points_chain_block_signatures",
        "points_chain_acceleration_requests",
        "points_chain_transfer_requests",
        "points_chain_deposit_addresses",
        "points_chain_bridge_events",
        "points_service_fee_charges",
        "points_chain_nodes",
        "points_chain_audit_logs",
        "points_disputes",
        "points_chain_recovery_state",
        "points_chain_backup_catalog",
        "points_chain_governance_proposals",
        "points_chain_governance_votes",
        "points_chain_governance_multisig_signatures",
        "points_chain_governance_audit_log",
        "points_chain_address_risk_labels",
        "points_chain_address_freezes",
        "points_chain_address_provisional_freezes",
        "points_chain_governance_branches",
        "points_chain_transaction_disputes",
        "points_economy_policy",
        "points_economy_fund_wallets",
        "points_economy_events",
        "points_economy_derived_balances",
        "points_economy_incidents",
        "points_economy_snapshots",
        "points_economy_daily_stats",
        "points_wallet_identities",
        "points_wallet_onboarding_events",
        "points_wallet_identity_bindings",
        "test_chain_blocks",
        "test_shadow_ledger",
    },
    "trading": {
        "trading_settings",
        "trading_markets",
        "trading_markets_registry",
        "trading_market_provider_mappings",
        "trading_market_registry_audit",
        "trading_orders",
        "trading_fills",
        "trading_spot_realized_pnl",
        "trading_sim_accounts",
        "trading_trial_credits",
        "trading_trial_position_costs",
        "trading_operation_idempotency",
        "trading_spot_positions",
        "trading_futures_positions",
        "trading_margin_positions",
        "trading_pending_profit",
        "trading_reserve_pool",
        "trading_reserve_pool_events",
        "trading_user_volume_stats",
        "trading_audit_events",
        "trading_state",
        "trading_bots",
        "trading_bot_runs",
        "trading_grid_bots",
        "trading_grid_orders",
        "trading_bot_competition_rewards",
        "trading_bot_audit_runs",
        "trading_bot_audit_findings",
        "trading_background_jobs",
        "trading_background_locks",
        "trading_background_job_runs",
        "trading_background_job_queue",
        "trading_root_snapshots",
        "test_shadow_orders",
        "test_shadow_positions",
        "test_shadow_margin_positions",
        "test_shadow_wallets",
        "test_shadow_transactions",
        "test_shadow_roles",
    },
    "jobs": {
        "job_center_jobs",
        "job_center_events",
    },
    "social": {
        "announcements",
        "board_moderators",
        "chat_messages",
        "chat_message_reports",
        "chat_rooms",
        "chat_room_members",
        "chat_room_invites",
        "forum_categories",
        "forum_boards",
        "forum_threads",
        "forum_posts",
        "forum_thread_reactions",
        "forum_thread_views",
        "forum_post_reactions",
        "forum_post_reports",
        "reports",
        "notifications",
        "share_access_events",
        "mail_outbox",
        "user_friends",
        "user_follows",
        "user_profiles",
        "reputation_events",
    },
    "governance": {
        "admin_sanction_appeal_contexts",
        "moderation_actions",
        "moderation_proposals",
        "moderation_votes",
        "member_level_rules",
        "member_level_audit",
        "secure_violations",
        "violation_appeals",
        "violation_fines",
        "violation_fine_appeals",
        "user_feature_restrictions",
        "user_mod_notes",
    },
    "comfyui": {
        "comfyui_generation_history",
        "comfyui_image_favorites",
        "comfyui_image_refs",
        "comfyui_template_preview_tokens",
        "comfyui_workflow_layout_versions",
        "comfyui_workflow_presets",
        "comfyui_workflow_runs",
    },
    "games": {
        "game_daily_challenge_rewards",
        "game_invites",
        "game_leaderboard_rewards",
        "game_matches",
        "game_multiplayer_events",
        "game_multiplayer_invites",
        "game_multiplayer_player_states",
        "game_multiplayer_rooms",
        "game_solo_scores",
    },
    "core": {
        "users",
        "user_passwords",
        "account_recovery_tokens",
        "password_reset_review_requests",
        "system_settings",
        "schema_migrations",
        "snapshots",
        "snapshot_restore_events",
        "integrity_findings",
        "integrity_scan_runs",
        "integrity_manifest_versions",
        "security_events",
        "ip_blocks",
        "login_locations",
        "superweak_dirty_writes",
    },
}


EXTERNALLY_SPLIT_TABLES = {
    "auth": {"csrf_tokens", "captcha_challenges", "login_attempts", "sessions"},
    "audit": {"secure_audit"},
    "control": {
        "server_modes",
        "server_checkpoints",
        "mode_switch_logs",
        "security_keys",
        "production_entry_reports",
        "incident_reports",
        "security_profiles",
        "tester_tokens",
        "tester_token_audit",
        "tester_token_request_log",
    },
}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def table_domain(table: str) -> str:
    for domain, tables in DOMAIN_TABLES.items():
        if table in tables:
            return domain
    for domain, tables in EXTERNALLY_SPLIT_TABLES.items():
        if table in tables:
            return f"already_split:{domain}"
    return "unclassified"


def domain_for_path(label: str, db_dir: str | Path) -> Path:
    info = DOMAIN_DATABASES.get(label)
    if not info:
        raise ValueError(f"unknown domain database: {label}")
    return Path(db_dir) / str(info["filename"])


def connect_db(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    db_path = Path(path)
    if read_only:
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def list_user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row["name"]) for row in rows]


def table_row_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {quote_ident(table)}").fetchone()
    return int(row["c"] if row else 0)


def normalize_sql_value(value):
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    return value


def table_digest(conn: sqlite3.Connection, table: str) -> dict:
    columns = [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()]
    hasher = hashlib.sha256()
    count = 0
    order_by = ", ".join(quote_ident(col) for col in columns) if columns else "rowid"
    for row in conn.execute(f"SELECT * FROM {quote_ident(table)} ORDER BY {order_by}"):
        payload = {col: normalize_sql_value(row[col]) for col in columns}
        hasher.update(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        hasher.update(b"\n")
        count += 1
    return {"rows": count, "sha256": hasher.hexdigest(), "columns": columns}


def analyze_database(source_path: str | Path) -> dict:
    source = Path(source_path)
    conn = connect_db(source, read_only=True)
    try:
        tables = []
        domains: dict[str, dict] = {}
        for table in list_user_tables(conn):
            domain = table_domain(table)
            rows = table_row_count(conn, table)
            tables.append({"table": table, "domain": domain, "rows": rows})
            domains.setdefault(domain, {"tables": 0, "rows": 0})
            domains[domain]["tables"] += 1
            domains[domain]["rows"] += rows
        return {
            "source": str(source),
            "created_at": utc_now_text(),
            "table_count": len(tables),
            "domains": domains,
            "tables": tables,
        }
    finally:
        conn.close()


def _schema_objects_for_tables(conn: sqlite3.Connection, tables: set[str]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in tables)
    return conn.execute(
        f"""
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL
          AND (
            (type='table' AND name IN ({placeholders}))
            OR (type IN ('index','trigger','view') AND tbl_name IN ({placeholders}))
          )
        ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'trigger' THEN 2 ELSE 3 END, name
        """,
        tuple(tables) + tuple(tables),
    ).fetchall()


def _sqlite_sequence_rows(conn: sqlite3.Connection, tables: set[str]) -> list[sqlite3.Row]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence' LIMIT 1"
    ).fetchone()
    if not exists:
        return []
    placeholders = ",".join("?" for _ in tables)
    return conn.execute(
        f"SELECT name, seq FROM sqlite_sequence WHERE name IN ({placeholders})",
        tuple(tables),
    ).fetchall()


def _copy_table_rows(source: sqlite3.Connection, dest: sqlite3.Connection, table: str, *, chunk_size: int = 500) -> None:
    columns = [str(row["name"]) for row in source.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()]
    if not columns:
        return
    col_sql = ", ".join(quote_ident(col) for col in columns)
    placeholders = ", ".join("?" for _ in columns)
    insert_sql = f"INSERT INTO {quote_ident(table)} ({col_sql}) VALUES ({placeholders})"
    cursor = source.execute(f"SELECT {col_sql} FROM {quote_ident(table)}")
    while True:
        rows = cursor.fetchmany(chunk_size)
        if not rows:
            break
        dest.executemany(insert_sql, [tuple(row[col] for col in columns) for row in rows])


def export_domain_tables(
    source_path: str | Path,
    out_dir: str | Path,
    *,
    domains: set[str] | None = None,
    overwrite: bool = False,
) -> dict:
    source = Path(source_path)
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    selected_domains = set(domains or DOMAIN_TABLES)
    source_conn = connect_db(source, read_only=True)
    manifest = {
        "created_at": utc_now_text(),
        "source": str(source),
        "out_dir": str(output),
        "mode": "export",
        "domains": {},
        "skipped": [],
    }
    try:
        existing_tables = set(list_user_tables(source_conn))
        for domain in sorted(selected_domains):
            if domain not in DOMAIN_TABLES:
                raise ValueError(f"unknown domain: {domain}")
            tables = DOMAIN_TABLES[domain] & existing_tables
            if not tables:
                manifest["domains"][domain] = {"path": "", "tables": {}, "row_count": 0, "table_count": 0}
                continue
            target = domain_for_path(domain, output)
            if target.exists():
                if not overwrite:
                    raise FileExistsError(str(target))
                target.unlink()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(target) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            dest = connect_db(target)
            try:
                dest.execute("PRAGMA foreign_keys = OFF")
                schema_objects = _schema_objects_for_tables(source_conn, tables)
                for obj in schema_objects:
                    if obj["type"] != "table":
                        continue
                    sql = str(obj["sql"] or "").strip()
                    if sql:
                        dest.execute(sql)
                for table in sorted(tables):
                    _copy_table_rows(source_conn, dest, table)
                for obj in schema_objects:
                    if obj["type"] == "table":
                        continue
                    sql = str(obj["sql"] or "").strip()
                    if sql:
                        dest.execute(sql)
                for row in _sqlite_sequence_rows(source_conn, tables):
                    dest.execute(
                        "INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                        (row["name"], row["seq"]),
                    )
                dest.commit()
                table_hashes = {table: table_digest(dest, table) for table in sorted(tables)}
                manifest["domains"][domain] = {
                    "path": str(target),
                    "table_count": len(table_hashes),
                    "row_count": sum(int(item["rows"]) for item in table_hashes.values()),
                    "tables": table_hashes,
                    "sha256": _digest_domain_table_hashes(table_hashes),
                }
            except Exception:
                dest.rollback()
                dest.close()
                if target.exists():
                    target.unlink()
                raise
            finally:
                try:
                    dest.close()
                except Exception:
                    pass
        assigned = set().union(*(DOMAIN_TABLES[d] for d in selected_domains if d in DOMAIN_TABLES))
        for table in sorted(existing_tables - assigned):
            manifest["skipped"].append({
                "table": table,
                "domain": table_domain(table),
                "rows": table_row_count(source_conn, table),
            })
        manifest_path = output / "domain_split_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        manifest["manifest_path"] = str(manifest_path)
        return manifest
    except Exception:
        # Leave already completed domain files in place for operator forensics,
        # but remove a partially written manifest if it exists.
        manifest_path = output / "domain_split_manifest.json"
        if manifest_path.exists():
            manifest_path.unlink()
        raise
    finally:
        source_conn.close()


def export_domains_to_database(
    source_path: str | Path,
    target_path: str | Path,
    *,
    domains: set[str],
    overwrite: bool = False,
) -> dict:
    """Copy selected domain tables into one SQLite database.

    This is used for domains that must keep same-connection transaction
    semantics after leaving ``database.db``.  For example, PointsChain and
    Trading are intentionally exported together into one ``finance.db``.
    """

    source = Path(source_path)
    target = Path(target_path)
    selected_domains = set(domains or set())
    unknown = sorted(selected_domains - set(DOMAIN_TABLES))
    if unknown:
        raise ValueError(f"unknown domain(s): {', '.join(unknown)}")
    if target.exists():
        if not overwrite:
            raise FileExistsError(str(target))
        target.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    source_conn = connect_db(source, read_only=True)
    dest = connect_db(target)
    manifest = {
        "created_at": utc_now_text(),
        "source": str(source),
        "target": str(target),
        "mode": "single_database_export",
        "domains": sorted(selected_domains),
        "tables": {},
        "row_count": 0,
        "table_count": 0,
    }
    try:
        existing_tables = set(list_user_tables(source_conn))
        selected_tables = set().union(*(DOMAIN_TABLES[d] for d in selected_domains)) & existing_tables
        if not selected_tables:
            dest.commit()
            manifest["sha256"] = _digest_domain_table_hashes({})
            return manifest
        dest.execute("PRAGMA foreign_keys = OFF")
        schema_objects = _schema_objects_for_tables(source_conn, selected_tables)
        for obj in schema_objects:
            if obj["type"] != "table":
                continue
            sql = str(obj["sql"] or "").strip()
            if sql:
                dest.execute(sql)
        for table in sorted(selected_tables):
            _copy_table_rows(source_conn, dest, table)
        for obj in schema_objects:
            if obj["type"] == "table":
                continue
            sql = str(obj["sql"] or "").strip()
            if sql:
                dest.execute(sql)
        for row in _sqlite_sequence_rows(source_conn, selected_tables):
            dest.execute(
                "INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                (row["name"], row["seq"]),
            )
        dest.commit()
        table_hashes = {table: table_digest(dest, table) for table in sorted(selected_tables)}
        manifest["tables"] = table_hashes
        manifest["row_count"] = sum(int(item["rows"]) for item in table_hashes.values())
        manifest["table_count"] = len(table_hashes)
        manifest["sha256"] = _digest_domain_table_hashes(table_hashes)
        return manifest
    except Exception:
        dest.rollback()
        dest.close()
        if target.exists():
            target.unlink()
        raise
    finally:
        try:
            dest.close()
        finally:
            source_conn.close()


def _digest_domain_table_hashes(table_hashes: dict[str, dict]) -> str:
    payload = {
        table: {"rows": int(data["rows"]), "sha256": str(data["sha256"])}
        for table, data in sorted(table_hashes.items())
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def copy_database_file(source: str | Path, target: str | Path, *, overwrite: bool = False) -> Path:
    source_path = Path(source)
    target_path = Path(target)
    if target_path.exists() and not overwrite:
        raise FileExistsError(str(target_path))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
    return target_path
