#!/usr/bin/env python3
"""Server Mode v2 — full SMv2 smoke harness.

Boots an isolated hackme_web on a free loopback port, then runs the
full set of SMv2 .sh tutorials end-to-end in sequence:

    01_internal_test_login_token.sh   (token tutorial #1)
    02_tester_token_shadow_api.sh     (token tutorial #2)
    04_pentest_smv2.sh                (SMv2-focused pentest probes)
    05_stress_smv2.sh                 (rate-limit / chain / shadow stress)
    06_full_feature_smv2.sh           (all SMv2 admin surfaces)
    07_privilege_escalation_smv2.sh   (privilege-escalation negatives)

Prints the per-script rc and a final overall pass/fail. Asserts shadow
vs production table isolation at the end (production tables MUST stay
at 0 rows). Exit 0 if every script returns rc=0 AND isolation holds.

This is the wider companion to scripts/security/server_mode/server_mode_v2_token_smoke.py
(which only drives 01 + 02). Use this one for "full SMv2 surface
green-light" gates; use the smaller harness for fast iteration during
token-tutorial work.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = REPO_ROOT / "docs" / "server_mode_v2"


def parse_args(argv: list[str]) -> list[str]:
    if any(arg in {"-h", "--help"} for arg in argv[1:]):
        print(__doc__.strip())
        raise SystemExit(0)
    if len(argv) > 1:
        raise SystemExit(f"unknown arguments: {' '.join(argv[1:])}")
    return argv


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_ready(base_url: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    ctx = ssl._create_unverified_context()
    last = ""
    while time.time() < deadline:
        for scheme in ("https", "http"):
            try:
                req = urllib.request.Request(f"{scheme}://{base_url.split('://',1)[-1]}/api/csrf-token")
                with urllib.request.urlopen(req, context=ctx, timeout=4) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:
                last = f"{scheme}: {exc}"
        time.sleep(0.5)
    raise TimeoutError(f"server not ready: {last}")


def lookup_test_user_id(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT id FROM users WHERE username='test'").fetchone()
        if not row:
            raise RuntimeError("user 'test' not bootstrapped")
        return int(row[0])
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    argv = parse_args(list(argv or sys.argv))
    runtime = Path(tempfile.mkdtemp(prefix="hackme_full_smoke_"))
    db_dir = runtime / "database"
    log_path = runtime / "server.log"
    port = free_port()

    initial_pw = "InitialRootP@ss-FullSmoke"
    new_pw = "PostChangeRootP@ss-FullSmoke"
    tester_pw = "TestUserP@ss-FullSmoke"

    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(REPO_ROOT),
        "HTML_LEARNING_HOST": "0.0.0.0",
        "HTML_LEARNING_DISABLE_TRUSTED_HOSTS": "1",
        "HTML_LEARNING_PORT": str(port),
        "HTML_LEARNING_DB_DIR": str(db_dir),
        "HTML_LEARNING_LOG_DIR": str(runtime / "logs"),
        "HTML_LEARNING_CHAT_DIR": str(runtime / "chats"),
        "HTML_LEARNING_ANCHOR_DIR": str(runtime / "anchors"),
        "HTML_LEARNING_STORAGE_DIR": str(runtime / "storage"),
        "HTML_LEARNING_REPORTS_DIR": str(runtime / "reports"),
        "HACKME_RUNTIME_DIR": str(runtime),
        "HTML_LEARNING_ROOT_PASSWORD": initial_pw,
        "HTML_LEARNING_TEST_PASSWORD": tester_pw,
        "FORCE_HTTPS": "false",
        "SESSION_COOKIE_SECURE": "false",
        "HTML_LEARNING_ALLOW_LOCAL_SERVER_MODE_KEYS": "1",
        "SERVER_MODE_LOG_HMAC_KEY": "full-smoke-log-key",
        "SERVER_MODE_TOKEN_HMAC_KEY": "full-smoke-token-key",
        "SERVER_MODE_LOG_HMAC_KEY_VERSION": "v1",
        "SERVER_MODE_TOKEN_HMAC_KEY_VERSION": "v1",
        "HTML_LEARNING_BOOTSTRAP_POINTS_CHAIN": "0",
        "HTML_LEARNING_SNAPSHOT_CHECK_INTERVAL_SECONDS": "999999",
        "HTML_LEARNING_STORAGE_MAINTENANCE_CHECK_INTERVAL_SECONDS": "999999",
        "HTML_LEARNING_POINTS_BLOCK_CHECK_INTERVAL_SECONDS": "999999",
        "HTML_LEARNING_TRADING_LIQUIDATION_CHECK_INTERVAL_SECONDS": "999999",
        "HTML_LEARNING_TRADING_BOT_SCAN_INTERVAL_SECONDS": "999999",
    })

    print(f"[full] runtime={runtime} port={port}")
    log_fp = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "server.py")],
        cwd=str(runtime), env=env, stdout=log_fp, stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    base_url = f"https://127.0.0.1:{port}"
    results: dict = {}
    try:
        try:
            wait_ready(base_url, timeout=60)
            print(f"[full] server ready at {base_url}")
        except Exception as exc:
            print(f"[full] FATAL: server did not become ready: {exc}")
            return 2

        db_path = db_dir / "database.db"
        test_user_id = lookup_test_user_id(db_path)
        # Smoke harness clears the test user's must_change_password flag
        # so /api/tester/* is reachable without an extra password-change
        # dance — the tutorial assumes this has already been handled.
        c = sqlite3.connect(str(db_path))
        try:
            c.execute("UPDATE users SET must_change_password=0, is_default_password=0 WHERE id=?", (test_user_id,))
            c.commit()
        finally:
            c.close()

        scripts = [
            ("01_internal_test_login_token.sh", {
                "ROOT_USER": "root", "ROOT_INITIAL_PW": initial_pw, "ROOT_NEW_PW": new_pw,
                "TESTER_USER": "test", "TESTER_PW": tester_pw,
            }),
            ("02_tester_token_shadow_api.sh", {
                "ROOT_USER": "root", "ROOT_PW": new_pw,
                "TESTER_USER_ID": str(test_user_id),
            }),
            ("04_pentest_smv2.sh", {
                "ROOT_USER": "root", "ROOT_PW": new_pw,
                "TESTER_USER_ID": str(test_user_id),
            }),
            ("05_stress_smv2.sh", {
                "ROOT_USER": "root", "ROOT_PW": new_pw,
                "TESTER_USER_ID": str(test_user_id),
                "BURST_SIZE": "40", "PARALLELISM": "8",
            }),
            ("06_full_feature_smv2.sh", {
                "ROOT_USER": "root",
                # Already past forced change — script 06's "if must_change"
                # branch is a no-op here, so the password stays the same.
                "ROOT_INITIAL_PW": new_pw,
                "ROOT_NEW_PW": new_pw,
                "TESTER_USER": "test",
                "TESTER_USER_ID": str(test_user_id),
            }),
            ("07_privilege_escalation_smv2.sh", {
                "ROOT_USER": "root", "ROOT_PW": new_pw,
                "TESTER_USER_ID": str(test_user_id),
            }),
        ]

        for idx, (name, extra_env) in enumerate(scripts):
            sh = EXAMPLES / name
            print(f"\n[full] === running {name} ===")
            run_env = dict(env, BASE_URL=base_url, **extra_env)
            r = subprocess.run(
                ["bash", str(sh)], cwd=str(REPO_ROOT), env=run_env,
                capture_output=True, text=True, timeout=300,
            )
            print(f"[full] {name} rc={r.returncode}")
            if r.returncode != 0:
                print("--- stdout ---\n" + (r.stdout or "<empty>"))
                print("--- stderr ---\n" + (r.stderr or "<empty>"))
            else:
                tail = "\n".join((r.stderr or "").splitlines()[-6:])
                print(tail)
            results[name] = {"rc": r.returncode}
            # Sleep between scripts so any tester-token rate-limit
            # / login-violation window from the previous script can
            # tick down before the next script issues its own probes.
            # 04 and 05 in particular accumulate many request-log
            # rows; without this sleep, 06/07 sometimes saw their
            # initial root login throttled.
            if idx + 1 < len(scripts):
                time.sleep(5)

        # Final isolation check
        c = sqlite3.connect(str(db_path)); c.row_factory = sqlite3.Row
        try:
            iso = {
                "test_shadow_wallets": c.execute("SELECT COUNT(*) AS n FROM test_shadow_wallets").fetchone()["n"],
                "test_shadow_transactions": c.execute("SELECT COUNT(*) AS n FROM test_shadow_transactions").fetchone()["n"],
                "test_shadow_roles": c.execute("SELECT COUNT(*) AS n FROM test_shadow_roles").fetchone()["n"],
                "points_chain_blocks": c.execute("SELECT COUNT(*) AS n FROM points_chain_blocks").fetchone()["n"],
                "points_wallets": c.execute("SELECT COUNT(*) AS n FROM points_wallets").fetchone()["n"],
                "points_ledger": c.execute("SELECT COUNT(*) AS n FROM points_ledger").fetchone()["n"],
            }
        finally:
            c.close()
        print(f"\n[full] post-run table counts: {json.dumps(iso, indent=2)}")
        prod_clean = (iso["points_chain_blocks"] == 0 and iso["points_wallets"] == 0 and iso["points_ledger"] == 0)
        rc_all_zero = all(r["rc"] == 0 for r in results.values())
        ok = rc_all_zero and prod_clean
        print(f"\n[full] all_scripts_passed={rc_all_zero}  prod_clean={prod_clean}  overall={ok}")
        return 0 if ok else 1
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM); proc.wait(timeout=10)
        except Exception:
            try: proc.kill()
            except Exception: pass
        log_fp.close()
        print(f"[full] runtime preserved: {runtime}")
        print(f"[full] server log: {log_path}")


if __name__ == "__main__":
    sys.exit(main())
