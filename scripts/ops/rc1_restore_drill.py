#!/usr/bin/env python3
"""Run an isolated RC1.1 snapshot-boundary drill.

The drill builds a synthetic runtime under /tmp (or a caller supplied workdir),
creates PointsChain data, snapshots the non-ledger runtime, dirties ordinary
runtime state, restores the snapshot, and verifies the restored file/runtime
state. PointsChain ledger backup/restore is deliberately checked as disabled:
ledger recovery must use safe mode, forensic bundles, branches, emergency
governance, and append-only corrections rather than backup overwrite.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_artifacts import test_artifact_path  # noqa: E402


DEFAULT_OUT_DIR = test_artifact_path("ops")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    sys.path.insert(0, str(ROOT))
    from services.platform.db_mode_triggers import register_app_mode_function

    register_app_mode_function(conn, mode_reader=lambda: "dev_ready")
    return conn


def init_base_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                member_level TEXT NOT NULL DEFAULT 'normal',
                base_level TEXT NOT NULL DEFAULT 'normal',
                effective_level TEXT NOT NULL DEFAULT 'normal'
            );
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                value_type TEXT,
                updated_at TEXT,
                updated_by TEXT
            );
            INSERT OR IGNORE INTO users (id, username, role, status, member_level, base_level, effective_level)
            VALUES
                (1, 'root', 'super_admin', 'active', 'normal', 'normal', 'normal'),
                (2, 'admin', 'manager', 'active', 'normal', 'normal', 'normal'),
                (3, 'test', 'user', 'active', 'normal', 'normal', 'normal');
            INSERT OR IGNORE INTO posts (id, title) VALUES (1, 'baseline post');
            INSERT OR REPLACE INTO system_settings (key, value, value_type, updated_at, updated_by)
            VALUES ('maintenance_mode', 'false', 'bool', '2026-01-01T00:00:00', 'rc1_1_drill');
            """
        )
        conn.commit()
    finally:
        conn.close()


def write_runtime_secrets(runtime_root: Path) -> list[Path]:
    files = [
        ".chain_seed",
        ".csrfkey",
        ".filekey",
        ".fkey",
        ".integrity_key",
        "integrity_manifest.json",
        "cert.pem",
        "key.pem",
        ".server_mode_log_hmac_key",
    ]
    paths = []
    for name in files:
        path = runtime_root / name
        if name == "integrity_manifest.json":
            path.write_text(json.dumps({"drill": True, "nonce": secrets.token_hex(8)}) + "\n", encoding="utf-8")
        else:
            path.write_text(f"{name}:{secrets.token_hex(16)}\n", encoding="utf-8")
        paths.append(path)
    return paths


def count_rows(db_path: Path, table: str) -> int:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        if not row:
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
    finally:
        conn.close()


def table_value(db_path: Path, sql: str, params: tuple = ()):
    conn = connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def run_drill(workdir: Path, *, keep_workdir: bool = False) -> dict:
    sys.path.insert(0, str(ROOT))
    from services.points_chain import PointsLedgerService
    from services.snapshots import SnapshotService, ensure_snapshot_schema

    started_at = utc_now()
    base = workdir / "app"
    runtime = base / "runtime"
    db_path = runtime / "database" / "database.db"
    storage = runtime / "storage"
    chats = runtime / "chats"
    points_backups = runtime / "database" / "points_chain_backups"
    points_forensics = points_backups / "forensics"
    for path in (storage, chats, points_forensics):
        path.mkdir(parents=True, exist_ok=True)
    init_base_db(db_path)
    runtime_secret_files = write_runtime_secrets(runtime)
    (storage / "baseline_asset.txt").write_text("asset-v1", encoding="utf-8")
    (chats / "baseline_room.txt").write_text("chat-v1", encoding="utf-8")

    audit_events = []

    def get_db():
        return connect(db_path)

    points_service = PointsLedgerService(
        get_db=get_db,
        chain_secret=(runtime / ".chain_seed").read_text(encoding="utf-8").strip(),
        audit=lambda *args, **kwargs: audit_events.append({"args": args, "kwargs": kwargs}),
        backup_dir=points_backups,
        mode_reader=lambda: "dev_ready",
    )
    snapshot_service = SnapshotService(
        get_db=get_db,
        db_path=db_path,
        base_dir=base,
        runtime_base_dir=runtime,
        storage_root=storage,
        audit=lambda *args, **kwargs: audit_events.append({"args": args, "kwargs": kwargs}),
        file_roots=[storage, chats, points_forensics],
        config_files=[],
        runtime_secret_files=runtime_secret_files,
    )
    snapshot_service.set_post_restore_validators([
        ("points_chain", lambda: points_service.verify_chain()),
    ])

    conn = get_db()
    try:
        ensure_snapshot_schema(conn)
        points_service.ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()

    actor = {"id": 1, "username": "root", "role": "super_admin"}
    genesis = points_service.bootstrap_admin_initial_grants(actor=actor, seal_genesis=True, require_wallet=False)
    ledger_backup = points_service.create_ledger_backup(reason="rc1_1_snapshot_boundary_drill", kind="restore_drill")
    try:
        points_service.restore_from_backup(actor=actor, backup_id="legacy", confirm="RESTORE POINTSCHAIN")
        backup_restore_rejected = False
        backup_restore_error = ""
    except PermissionError as exc:
        backup_restore_rejected = "backup restore is disabled" in str(exc)
        backup_restore_error = str(exc)
    baseline_verify = points_service.verify_chain()
    baseline_counts = {
        "posts": count_rows(db_path, "posts"),
        "ledger": count_rows(db_path, "points_ledger"),
        "blocks": count_rows(db_path, "points_chain_blocks"),
        "wallets": count_rows(db_path, "points_wallets"),
    }
    snapshot = snapshot_service.create_snapshot(
        snapshot_type="manual",
        actor=actor,
        notes="RC1.1 snapshot-boundary drill baseline",
    )

    conn = get_db()
    try:
        conn.execute("INSERT INTO posts (id, title) VALUES (2, 'dirty post after snapshot')")
        conn.commit()
    finally:
        conn.close()
    (storage / "dirty_asset.txt").write_text("dirty", encoding="utf-8")
    (chats / "dirty_room.txt").write_text("dirty", encoding="utf-8")
    dirty_counts = {
        "posts": count_rows(db_path, "posts"),
        "ledger": count_rows(db_path, "points_ledger"),
        "blocks": count_rows(db_path, "points_chain_blocks"),
        "wallets": count_rows(db_path, "points_wallets"),
    }

    restore = snapshot_service.restore_snapshot(
        snapshot_id=snapshot.snapshot_id,
        actor=actor,
        reason="RC1.1 isolated snapshot-boundary drill",
    )
    restored_verify = points_service.verify_chain()
    restored_counts = {
        "posts": count_rows(db_path, "posts"),
        "ledger": count_rows(db_path, "points_ledger"),
        "blocks": count_rows(db_path, "points_chain_blocks"),
        "wallets": count_rows(db_path, "points_wallets"),
    }
    restored_post_title = table_value(db_path, "SELECT title FROM posts WHERE id=2")
    invariants = {
        "snapshot_created": bool(snapshot.ok),
        "points_chain_backup_disabled": bool(ledger_backup.get("disabled")) and not bool(ledger_backup.get("created")),
        "points_chain_backup_restore_rejected": backup_restore_rejected,
        "baseline_chain_verify": bool(baseline_verify.get("ok")),
        "restore_ok": bool(restore.get("ok")),
        "restored_chain_verify": bool(restored_verify.get("ok")),
        "dirty_post_removed": restored_post_title is None,
        "dirty_storage_removed": not (storage / "dirty_asset.txt").exists(),
        "dirty_chat_removed": not (chats / "dirty_room.txt").exists(),
        "baseline_storage_restored": (storage / "baseline_asset.txt").read_text(encoding="utf-8") == "asset-v1",
        "baseline_chat_restored": (chats / "baseline_room.txt").read_text(encoding="utf-8") == "chat-v1",
        "ledger_not_mutated_during_drill": dirty_counts["ledger"] == baseline_counts["ledger"] == restored_counts["ledger"],
        "block_not_mutated_during_drill": dirty_counts["blocks"] == baseline_counts["blocks"] == restored_counts["blocks"],
    }
    ok = all(invariants.values())
    return {
        "ok": ok,
        "drill": "rc1_1_restore_drill",
        "started_at": started_at,
        "finished_at": utc_now(),
        "workdir": str(workdir) if keep_workdir else "",
        "snapshot_id": snapshot.snapshot_id if snapshot else "",
        "ledger_backup_disabled": ledger_backup,
        "backup_restore_error": backup_restore_error,
        "genesis": {
            "created_count": genesis.get("created_count"),
            "deferred_count": genesis.get("deferred_count"),
            "sealed": bool(genesis.get("sealed")),
        },
        "ledger_restore_exercised": False,
        "counts": {
            "baseline": baseline_counts,
            "dirty": dirty_counts,
            "restored": restored_counts,
        },
        "invariants": invariants,
        "baseline_verify": baseline_verify,
        "restore": restore,
        "restored_verify": restored_verify,
        "audit_event_count": len(audit_events),
    }


def default_out_path(out_dir: Path) -> Path:
    stamp = utc_now().replace("-", "").replace(":", "").replace("Z", "")
    return out_dir / f"restore_drill_{stamp}.json"


def prepare_explicit_workdir(value: str) -> Path:
    workdir = Path(value).expanduser().resolve()
    tmp_root = Path(tempfile.gettempdir()).resolve()
    if workdir == tmp_root or tmp_root not in workdir.parents:
        raise SystemExit("--workdir must resolve below the system temporary directory")
    if workdir.exists() or workdir.is_symlink():
        raise SystemExit(f"--workdir must not already exist: {workdir}")
    workdir.mkdir(parents=True)
    return workdir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an isolated RC1.1 snapshot-boundary drill.")
    parser.add_argument("--out", default="", help="Output JSON path. Defaults under /tmp/hackme_web_test_artifacts/ops.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--workdir", default="", help="Optional new directory below /tmp. Existing paths are refused.")
    parser.add_argument("--keep-workdir", action="store_true", help="Keep and report the workdir for debugging.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = Path(args.out) if args.out else default_out_path(Path(args.out_dir))
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.workdir:
        workdir = prepare_explicit_workdir(args.workdir)
        try:
            payload = run_drill(workdir, keep_workdir=args.keep_workdir)
        finally:
            if not args.keep_workdir:
                shutil.rmtree(workdir, ignore_errors=True)
    else:
        with tempfile.TemporaryDirectory(prefix="pointschain_rc1_1_restore_drill_") as tmp:
            payload = run_drill(Path(tmp), keep_workdir=args.keep_workdir)
            if args.keep_workdir:
                # TemporaryDirectory will be removed, so keep-workdir only has
                # durable meaning with an explicit new --workdir.
                payload["workdir"] = ""
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": payload["ok"],
        "out": str(out),
        "snapshot_id": payload.get("snapshot_id"),
        "ledger_backup_disabled": bool((payload.get("ledger_backup_disabled") or {}).get("disabled")),
        "ledger_restore_exercised": bool(payload.get("ledger_restore_exercised")),
        "invariants": payload.get("invariants"),
    }, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
