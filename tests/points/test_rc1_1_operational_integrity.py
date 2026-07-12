import json
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _run(cmd):
    return subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )


def test_rc1_1_restore_drill_cli_proves_snapshot_boundary_invariants(tmp_path):
    out = tmp_path / "restore_drill.json"
    proc = _run([sys.executable, "scripts/ops/rc1_restore_drill.py", "--out", str(out)])

    assert proc.returncode == 0, proc.stdout
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["restore"]["ok"] is True
    assert payload["baseline_verify"]["ok"] is True
    assert payload["restored_verify"]["ok"] is True
    assert all(payload["invariants"].values())
    assert payload["ledger_backup_disabled"]["disabled"] is True
    assert payload["ledger_restore_exercised"] is False
    assert payload["counts"]["dirty"]["ledger"] == payload["counts"]["baseline"]["ledger"]
    assert payload["counts"]["restored"]["ledger"] == payload["counts"]["baseline"]["ledger"]


def test_rc1_1_restore_drill_refuses_existing_workdir(tmp_path):
    sentinel = tmp_path / "do-not-delete.txt"
    sentinel.write_text("keep", encoding="utf-8")
    out = tmp_path / "restore_drill.json"

    proc = _run([
        sys.executable,
        "scripts/ops/rc1_restore_drill.py",
        "--workdir",
        str(tmp_path),
        "--out",
        str(out),
    ])

    assert proc.returncode != 0
    assert "--workdir must not already exist" in proc.stdout
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not out.exists()


def test_export_chain_anchor_cli_signs_checkpoint(tmp_path):
    db_path = tmp_path / "database.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE points_chain_blocks (
            id INTEGER PRIMARY KEY,
            block_number INTEGER NOT NULL,
            previous_block_hash TEXT,
            merkle_root TEXT NOT NULL,
            block_hash TEXT NOT NULL,
            ledger_count INTEGER NOT NULL,
            first_ledger_id INTEGER NOT NULL,
            last_ledger_id INTEGER NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE points_ledger (
            id INTEGER PRIMARY KEY,
            ledger_hash TEXT NOT NULL,
            chain_block_id INTEGER
        );
        CREATE TABLE points_wallets (
            user_id INTEGER PRIMARY KEY,
            soft_balance INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE points_chain_governance_branches (
            id INTEGER PRIMARY KEY,
            branch_uuid TEXT NOT NULL,
            is_canonical INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            activated_at TEXT
        );
        INSERT INTO points_chain_governance_branches
            (id, branch_uuid, is_canonical, created_at, activated_at)
        VALUES (1, 'main', 1, '2026-05-23T00:00:00Z', '2026-05-23T00:00:00Z');
        INSERT INTO points_chain_blocks
            (id, block_number, previous_block_hash, merkle_root, block_hash, ledger_count, first_ledger_id, last_ledger_id, sealed_at)
        VALUES (1, 1, '', 'merkle-a', 'block-a', 1, 1, 1, '2026-05-23T00:00:01Z');
        INSERT INTO points_ledger (id, ledger_hash, chain_block_id) VALUES (1, 'ledger-a', 1);
        INSERT INTO points_wallets (user_id, soft_balance) VALUES (2, 100);
        """
    )
    conn.commit()
    conn.close()
    key_file = tmp_path / ".anchor_signing_key"
    out = tmp_path / "anchor.json"

    proc = _run([
        sys.executable,
        "scripts/ops/export_chain_anchor.py",
        "--db",
        str(db_path),
        "--anchor-key-file",
        str(key_file),
        "--chain-seed-file",
        str(tmp_path / "missing_chain_seed"),
        "--environment",
        "test",
        "--release-version",
        "test-release",
        "--out",
        str(out),
    ])

    assert proc.returncode == 0, proc.stdout
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "pointschain_anchor_v0"
    assert payload["chain"]["latest_block_height"] == 1
    assert payload["chain"]["latest_block_hash"] == "block-a"
    assert payload["chain"]["latest_ledger_hash"] == "ledger-a"
    assert payload["chain"]["wallet_count"] == 1
    assert payload["verification"]["local_chain_verify"]["status"] == "not_run"
    assert payload["signature_algorithm"] == "hmac-sha256"
    assert payload["signature_payload_hash"]
    assert payload["signature"]
    assert key_file.exists()

    sys.path.insert(0, str(ROOT))
    from scripts.ops.export_chain_anchor import verify_anchor_signature

    assert verify_anchor_signature(payload, key_file.read_text(encoding="utf-8").strip()) is True
