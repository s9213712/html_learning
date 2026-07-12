#!/usr/bin/env python3
"""Export a signed PointsChain checkpoint anchor.

This is RC1.1 anchoring v0: it does not publish to an external chain and it
does not replace local chain verification. It creates a portable, signed JSON
checkpoint that can be copied to offline/read-only storage.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_artifacts import test_artifact_path  # noqa: E402


DEFAULT_OUT_DIR = test_artifact_path("anchors")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def table_count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> int:
    if not table_exists(conn, table):
        return 0
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def read_or_create_key(path: Path) -> str:
    path = Path(path)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def latest_row(conn: sqlite3.Connection, table: str, order: str):
    if not table_exists(conn, table):
        return None
    row = conn.execute(f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def canonical_branch(conn: sqlite3.Connection) -> str:
    if not table_exists(conn, "points_chain_governance_branches"):
        return "main"
    row = conn.execute(
        """
        SELECT branch_uuid
        FROM points_chain_governance_branches
        WHERE is_canonical=1
        ORDER BY COALESCE(activated_at, created_at) DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row["branch_uuid"] or "main") if row else "main"


def maybe_verify_chain(db_path: Path, chain_seed_file: Path | None) -> dict:
    if not chain_seed_file or not chain_seed_file.exists():
        return {"ok": None, "status": "not_run", "reason": "chain_seed_file_missing"}
    try:
        sys.path.insert(0, str(ROOT))
        from services.points_chain import PointsLedgerService

        chain_secret = chain_seed_file.read_text(encoding="utf-8").strip()

        def get_db():
            conn = sqlite3.connect(str(db_path), timeout=30)
            conn.row_factory = sqlite3.Row
            return conn

        service = PointsLedgerService(
            get_db=get_db,
            chain_secret=chain_secret,
            audit=lambda *args, **kwargs: None,
            backup_dir=db_path.parent / "points_chain_backups",
            mode_reader=lambda: "maintenance",
        )
        result = service.verify_chain()
        return {"ok": bool(result.get("ok")), "status": "pass" if result.get("ok") else "fail", "result": result}
    except Exception as exc:
        return {"ok": False, "status": "error", "error": str(exc)}


def build_anchor_payload(
    *,
    db_path: Path,
    environment: str,
    release_version: str,
    generated_at: str,
    chain_verify: dict,
) -> dict:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        branch = canonical_branch(conn)
        latest_block = latest_row(conn, "points_chain_blocks", "block_number")
        latest_ledger = latest_row(conn, "points_ledger", "id")
        block_count = table_count(conn, "points_chain_blocks")
        ledger_count = table_count(conn, "points_ledger")
        wallet_count = table_count(conn, "points_wallets")
        unsealed_ledger_count = table_count(conn, "points_ledger", "chain_block_id IS NULL")
        pending_transfer_count = table_count(
            conn,
            "points_chain_transfer_requests",
            "status='pending' AND chain_branch=?",
            (branch,),
        )
        governance_proposal_count = table_count(conn, "points_chain_governance_proposals")
        latest_block_hash = str((latest_block or {}).get("block_hash") or "")
        latest_ledger_hash = str((latest_ledger or {}).get("ledger_hash") or "")
        chain_root = sha256_text(canonical_json({
            "canonical_branch": branch,
            "latest_block_hash": latest_block_hash,
            "latest_ledger_hash": latest_ledger_hash,
            "block_count": block_count,
            "ledger_count": ledger_count,
        }))
        return {
            "schema_version": "pointschain_anchor_v0",
            "generated_at": generated_at,
            "environment": environment,
            "release_version": release_version,
            "source": "local_checkpoint_export",
            "chain": {
                "canonical_branch": branch,
                "latest_block_height": int((latest_block or {}).get("block_number") or 0),
                "latest_block_hash": latest_block_hash,
                "latest_block_merkle_root": str((latest_block or {}).get("merkle_root") or ""),
                "latest_block_sealed_at": str((latest_block or {}).get("sealed_at") or ""),
                "latest_ledger_id": int((latest_ledger or {}).get("id") or 0),
                "latest_ledger_hash": latest_ledger_hash,
                "block_count": block_count,
                "ledger_count": ledger_count,
                "wallet_count": wallet_count,
                "unsealed_ledger_count": unsealed_ledger_count,
                "pending_transfer_count": pending_transfer_count,
                "governance_proposal_count": governance_proposal_count,
                "chain_root": chain_root,
            },
            "verification": {
                "local_chain_verify": chain_verify,
            },
        }
    finally:
        conn.close()


def sign_anchor(anchor: dict, signing_key: str) -> dict:
    payload_hash = sha256_text(canonical_json(anchor))
    signature = hmac.new(signing_key.encode("utf-8"), payload_hash.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        **anchor,
        "signature_payload_hash": payload_hash,
        "signature_algorithm": "hmac-sha256",
        "signing_key_id": sha256_text(signing_key)[:16],
        "signature": signature,
    }


def verify_anchor_signature(anchor: dict, signing_key: str) -> bool:
    payload = dict(anchor)
    signature = str(payload.pop("signature", "") or "")
    payload.pop("signature_algorithm", None)
    payload.pop("signing_key_id", None)
    payload_hash = str(payload.pop("signature_payload_hash", "") or "")
    if not signature or not payload_hash:
        return False
    expected_hash = sha256_text(canonical_json(payload))
    expected_sig = hmac.new(signing_key.encode("utf-8"), expected_hash.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(payload_hash, expected_hash) and hmac.compare_digest(signature, expected_sig)


def default_out_path(out_dir: Path, generated_at: str) -> Path:
    stamp = generated_at.replace("-", "").replace(":", "").replace("Z", "")
    return out_dir / f"{stamp}.json"


def configured_runtime_root() -> Path | None:
    value = str(os.environ.get("HACKME_RUNTIME_DIR", "") or "").strip()
    return Path(value).expanduser() if value else None


def runtime_root_for_db(db_path: Path) -> Path:
    if db_path.parent.name == "database":
        return db_path.parent.parent
    return db_path.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a signed PointsChain anchor checkpoint.")
    runtime_root = configured_runtime_root()
    default_db = runtime_root / "database" / "database.db" if runtime_root else ""
    parser.add_argument("--db", default=str(default_db), help="Path to runtime database.db. Required unless HACKME_RUNTIME_DIR is set.")
    parser.add_argument("--out", default="", help="Output JSON path. Defaults under /tmp/hackme_web_test_artifacts/anchors.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory when --out is omitted.")
    parser.add_argument("--anchor-key-file", default="", help="Local HMAC key file. Defaults beside the selected runtime database.")
    parser.add_argument("--chain-seed-file", default="", help="Optional chain seed. Defaults beside the selected runtime database when present.")
    parser.add_argument("--environment", default=os.environ.get("HTML_LEARNING_SERVER_MODE", "unknown"))
    parser.add_argument("--release-version", default=os.environ.get("SERVER_RELEASE_ID", "unknown"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.db:
        raise SystemExit("--db or HACKME_RUNTIME_DIR is required")
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    generated_at = utc_now()
    runtime_root = runtime_root_for_db(db_path)
    anchor_key_file = Path(args.anchor_key_file).expanduser() if args.anchor_key_file else runtime_root / ".anchor_signing_key"
    signing_key = read_or_create_key(anchor_key_file)
    if args.chain_seed_file:
        chain_seed_file = Path(args.chain_seed_file).expanduser()
    else:
        candidate = runtime_root / ".chain_seed"
        chain_seed_file = candidate if candidate.exists() else None
    chain_verify = maybe_verify_chain(db_path, chain_seed_file)
    anchor = build_anchor_payload(
        db_path=db_path,
        environment=str(args.environment or "unknown"),
        release_version=str(args.release_version or "unknown"),
        generated_at=generated_at,
        chain_verify=chain_verify,
    )
    signed = sign_anchor(anchor, signing_key)
    out = Path(args.out) if args.out else default_out_path(Path(args.out_dir), generated_at)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(signed, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    print(json.dumps({
        "ok": True,
        "out": str(out),
        "latest_block_height": signed["chain"]["latest_block_height"],
        "latest_block_hash": signed["chain"]["latest_block_hash"],
        "chain_root": signed["chain"]["chain_root"],
        "chain_verify": signed["verification"]["local_chain_verify"].get("status"),
        "signature_payload_hash": signed["signature_payload_hash"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
