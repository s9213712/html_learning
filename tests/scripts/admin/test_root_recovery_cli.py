import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.admin import root_recovery
from services.users.auth import hash_password, verify_password


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "admin" / "root_recovery.py"


def _init_root_db(path: Path):
    old_hash = hash_password("OldRootPassword123!")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            role TEXT NOT NULL DEFAULT 'user',
            failed_login_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            is_default_password INTEGER NOT NULL DEFAULT 0,
            password_strength_score INTEGER NOT NULL DEFAULT 0,
            password_changed_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE user_passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0,
            revoked_at TEXT
        );
        CREATE TABLE csrf_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT NOT NULL,
            user TEXT NOT NULL,
            success INTEGER NOT NULL,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            chain_hash TEXT NOT NULL
        );
        INSERT INTO users (id, username, status, role, failed_login_count, must_change_password, is_default_password)
        VALUES (1, 'root', 'active', 'super_admin', 2, 0, 0);
        INSERT INTO sessions (user_id, token_hash, expires_at, is_revoked)
        VALUES (1, 'session-a', '2999-01-01T00:00:00', 0);
        INSERT INTO sessions (user_id, token_hash, expires_at, is_revoked)
        VALUES (1, 'session-b', '2999-01-01T00:00:00', 0);
        INSERT INTO csrf_tokens (username, token_hash, expires_at)
        VALUES ('root', 'csrf-1', '2999-01-01T00:00:00');
        """
    )
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (1, ?, '2026-01-01T00:00:00')",
        (old_hash,),
    )
    conn.commit()
    conn.close()


def _prepare_runtime(tmp_path: Path):
    runtime = tmp_path / "runtime"
    (runtime / "database").mkdir(parents=True)
    (runtime / "logs").mkdir()
    (runtime / "anchors").mkdir()
    (runtime / ".chain_seed").write_text("seed-value", encoding="utf-8")
    (runtime / ".integrity_key").write_bytes(b"0123456789abcdef0123456789abcdef")
    _init_root_db(runtime / "database" / "database.db")
    return runtime


def test_root_recovery_cli_rotates_password_revokes_sessions_and_audits(tmp_path):
    runtime = _prepare_runtime(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--runtime-dir",
            str(runtime),
            "--json",
            "--reason",
            "pytest recovery",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["username"] == "root"
    assert payload["must_change_password"] is True
    assert payload["sessions_revoked"] == 2
    assert payload["audit_recorded"] is True
    assert len(payload["temporary_password"]) >= 12

    conn = sqlite3.connect(runtime / "database" / "database.db")
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            "SELECT password_hash FROM user_passwords WHERE user_id=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()["password_hash"]
        user = conn.execute(
            "SELECT must_change_password, failed_login_count, is_default_password FROM users WHERE id=1"
        ).fetchone()
        revoked = conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=1 AND is_revoked=1").fetchone()[0]
        csrf_left = conn.execute("SELECT COUNT(*) FROM csrf_tokens WHERE username='root'").fetchone()[0]
        audit_row = conn.execute(
            "SELECT action, detail FROM secure_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert verify_password(latest, payload["temporary_password"]) is True
    assert user["must_change_password"] == 1
    assert user["failed_login_count"] == 0
    assert user["is_default_password"] == 0
    assert revoked == 2
    assert csrf_left == 0
    assert audit_row["action"] == "ROOT_OFFLINE_PASSWORD_RECOVERY"
    assert "pytest recovery" in audit_row["detail"]


def test_root_recovery_cli_rejects_weak_password(tmp_path):
    runtime = _prepare_runtime(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--runtime-dir",
            str(runtime),
            "--password",
            "weak",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "密碼" in payload["msg"]


def test_root_recovery_default_runtime_dir_requires_explicit_runtime(monkeypatch):
    monkeypatch.delenv("HACKME_RUNTIME_DIR", raising=False)

    try:
        root_recovery.default_runtime_dir()
    except ValueError as exc:
        assert "HACKME_RUNTIME_DIR or --runtime-dir is required" in str(exc)
    else:
        raise AssertionError("repo runtime fallback must remain disabled")


def test_root_recovery_cli_can_infer_runtime_from_explicit_db_path(tmp_path):
    runtime = _prepare_runtime(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db-path",
            str(runtime / "database" / "database.db"),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["runtime_dir"] == str(runtime.resolve())
