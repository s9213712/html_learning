#!/usr/bin/env python3
"""Pre-Phase 0: drive the two token tutorial scripts on an isolated runtime.

Boots an isolated hackme_web on a free loopback port (modeled on
scripts/security/server_mode/server_mode_v2_live_http_smoke.py), waits ready, runs:
  1. docs/server_mode_v2/01_internal_test_login_token.sh
  2. docs/server_mode_v2/02_tester_token_shadow_api.sh

then tears the server down and prints the per-step results.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = REPO_ROOT / "docs" / "server_mode_v2"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(base_url: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    ctx = ssl._create_unverified_context()
    last = ""
    while time.time() < deadline:
        for scheme in ("https", "http"):
            url = f"{scheme}://{base_url.split('://',1)[-1]}/api/csrf-token"
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, context=ctx, timeout=4) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:
                last = f"{scheme}: {exc}"
        time.sleep(0.5)
    raise TimeoutError(f"server not ready within {timeout}s; last={last}")


def lookup_test_user_id(db_path: Path) -> int:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT id FROM users WHERE username='test'").fetchone()
        if not row:
            raise RuntimeError("user 'test' not found in bootstrapped DB")
        return int(row[0])
    finally:
        conn.close()


def main() -> int:
    runtime_root = Path(tempfile.mkdtemp(prefix="hackme_token_smoke_"))
    runtime_dir = runtime_root / "rt"
    runtime_dir.mkdir(parents=True)
    log_path = runtime_dir / "server.log"
    db_dir = runtime_dir / "database"
    db_path = db_dir / "database.db"
    port = free_port()

    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(REPO_ROOT),
        "HTML_LEARNING_HOST": "0.0.0.0",
        "HTML_LEARNING_DISABLE_TRUSTED_HOSTS": "1",
        "HTML_LEARNING_PORT": str(port),
        "HTML_LEARNING_DB_DIR": str(db_dir),
        "HTML_LEARNING_LOG_DIR": str(runtime_dir / "logs"),
        "HTML_LEARNING_CHAT_DIR": str(runtime_dir / "chats"),
        "HTML_LEARNING_ANCHOR_DIR": str(runtime_dir / "anchors"),
        "HTML_LEARNING_STORAGE_DIR": str(runtime_dir / "storage"),
        "HTML_LEARNING_REPORTS_DIR": str(runtime_dir / "reports"),
        "HACKME_RUNTIME_DIR": str(runtime_dir),
        "HTML_LEARNING_ROOT_PASSWORD": "InitialRootP@ss-Smoke",
        "HTML_LEARNING_MANAGER_PASSWORD": "ManagerP@ss-Smoke",
        "HTML_LEARNING_TEST_PASSWORD": "TestUserP@ss-Smoke",
        "FORCE_HTTPS": "false",
        "SESSION_COOKIE_SECURE": "false",
        "HTML_LEARNING_ALLOW_LOCAL_SERVER_MODE_KEYS": "1",
        "SERVER_MODE_LOG_HMAC_KEY": "token-smoke-log-key",
        "SERVER_MODE_TOKEN_HMAC_KEY": "token-smoke-token-key",
        "SERVER_MODE_LOG_HMAC_KEY_VERSION": "v1",
        "SERVER_MODE_TOKEN_HMAC_KEY_VERSION": "v1",
        "HTML_LEARNING_BOOTSTRAP_POINTS_CHAIN": "0",
        "HTML_LEARNING_SNAPSHOT_CHECK_INTERVAL_SECONDS": "999999",
        "HTML_LEARNING_STORAGE_MAINTENANCE_CHECK_INTERVAL_SECONDS": "999999",
        "HTML_LEARNING_POINTS_BLOCK_CHECK_INTERVAL_SECONDS": "999999",
        "HTML_LEARNING_TRADING_LIQUIDATION_CHECK_INTERVAL_SECONDS": "999999",
        "HTML_LEARNING_TRADING_BOT_SCAN_INTERVAL_SECONDS": "999999",
    })

    print(f"[smoke] runtime={runtime_dir}")
    print(f"[smoke] port={port}")
    print(f"[smoke] log={log_path}")

    log_fp = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "server.py")],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def tail_log(n: int = 80) -> str:
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", errors="replace")
            return "\n".join(tail.splitlines()[-n:])
        except Exception as exc:
            return f"<log unavailable: {exc}>"

    base_url = f"https://127.0.0.1:{port}"
    results: dict[str, dict] = {}
    try:
        try:
            wait_ready(base_url, timeout=60)
            print(f"[smoke] server ready at {base_url}")
        except Exception as exc:
            print(f"[smoke] FATAL: server did not become ready: {exc}")
            print("[smoke] log tail:\n" + tail_log(120))
            return 2

        test_user_id = lookup_test_user_id(db_path)
        print(f"[smoke] test user id = {test_user_id}")

        # Smoke harness: clear must_change_password on test user so tester APIs
        # in script-02 are not blocked by enforce_required_password_change.
        # The tutorial script intentionally does NOT do this — it assumes the
        # tester has already cleared the forced-change flag through normal login.
        import sqlite3
        _c = sqlite3.connect(str(db_path))
        try:
            _c.execute("UPDATE users SET must_change_password=0, is_default_password=0 WHERE id=?", (test_user_id,))
            _c.commit()
        finally:
            _c.close()
        print(f"[smoke] cleared must_change_password on test user (smoke harness only)")

        # ── Script 1 ───────────────────────────────────────────────────
        sh1 = EXAMPLES / "01_internal_test_login_token.sh"
        env1 = dict(env)
        env1.update({
            "BASE_URL": base_url,
            "ROOT_USER": "root",
            "ROOT_INITIAL_PW": "InitialRootP@ss-Smoke",
            "ROOT_NEW_PW": "PostChangeRootP@ss-Smoke",
            "TESTER_USER": "test",
            "TESTER_PW": "TestUserP@ss-Smoke",
        })
        print(f"\n[smoke] === running {sh1.name} ===")
        r1 = subprocess.run(
            ["bash", str(sh1)],
            cwd=str(REPO_ROOT),
            env=env1,
            capture_output=True,
            text=True,
            timeout=180,
        )
        print(f"[smoke] {sh1.name} rc={r1.returncode}")
        print("--- stdout ---\n" + (r1.stdout or "<empty>"))
        print("--- stderr ---\n" + (r1.stderr or "<empty>"))
        results["01"] = {"rc": r1.returncode, "stdout": r1.stdout, "stderr": r1.stderr}

        # ── Script 2 ───────────────────────────────────────────────────
        sh2 = EXAMPLES / "02_tester_token_shadow_api.sh"
        env2 = dict(env)
        env2.update({
            "BASE_URL": base_url,
            "ROOT_USER": "root",
            "ROOT_PW": "PostChangeRootP@ss-Smoke",  # script-01 已改密碼
            "TESTER_USER_ID": str(test_user_id),
        })
        print(f"\n[smoke] === running {sh2.name} ===")
        r2 = subprocess.run(
            ["bash", str(sh2)],
            cwd=str(REPO_ROOT),
            env=env2,
            capture_output=True,
            text=True,
            timeout=180,
        )
        print(f"[smoke] {sh2.name} rc={r2.returncode}")
        print("--- stdout ---\n" + (r2.stdout or "<empty>"))
        print("--- stderr ---\n" + (r2.stderr or "<empty>"))
        results["02"] = {"rc": r2.returncode, "stdout": r2.stdout, "stderr": r2.stderr}

        # ── Verify shadow vs prod isolation post-run ──────────────────
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            shadow_wallets = conn.execute("SELECT COUNT(*) AS c FROM test_shadow_wallets").fetchone()["c"]
            shadow_tx = conn.execute("SELECT COUNT(*) AS c FROM test_shadow_transactions").fetchone()["c"]
            shadow_roles = conn.execute("SELECT COUNT(*) AS c FROM test_shadow_roles").fetchone()["c"]
            prod_chain = conn.execute("SELECT COUNT(*) AS c FROM points_chain_blocks").fetchone()["c"]
            prod_wallets = conn.execute("SELECT COUNT(*) AS c FROM points_wallets").fetchone()["c"]
            prod_ledger = conn.execute("SELECT COUNT(*) AS c FROM points_ledger").fetchone()["c"]
            iso = {
                "test_shadow_wallets": shadow_wallets,
                "test_shadow_transactions": shadow_tx,
                "test_shadow_roles": shadow_roles,
                "points_chain_blocks": prod_chain,
                "points_wallets": prod_wallets,
                "points_ledger": prod_ledger,
            }
        finally:
            conn.close()
        print(f"\n[smoke] post-run table counts: {json.dumps(iso, indent=2)}")
        results["isolation_check"] = iso

    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log_fp.close()
        # keep runtime + log for inspection
        print(f"\n[smoke] runtime preserved at: {runtime_dir}")
        print(f"[smoke] server log preserved at: {log_path}")

    overall = (results.get("01", {}).get("rc") == 0) and (results.get("02", {}).get("rc") == 0)
    print(f"\n[smoke] overall_pass={overall}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
