#!/usr/bin/env python3
"""Live HTTP smoke test for Server Mode v2 production sign-off.

This script starts an isolated hackme_web server process on loopback, drives the
real Flask HTTP/session/CSRF stack, kills the server during superweak mode, then
restarts it to verify startup rollback behavior. It does not touch the
developer's live :5000 server.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.security.common_paths import security_reports_root  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def read_json_response(resp):
    raw = resp.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text) if text else {}
    except Exception:
        payload = {"_raw": text[:500]}
    return payload, text


class HttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookies = CookieJar()
        self.csrf_token = ""
        self.context = ssl._create_unverified_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=self.context),
        )

    def request(self, method, path, payload=None, headers=None, expected=None):
        method = method.upper()
        url = self.base_url + path
        body = None
        req_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        if method in {"POST", "PUT", "PATCH", "DELETE"} and self.csrf_token:
            req_headers.setdefault("X-CSRF-Token", self.csrf_token)
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        started = time.perf_counter()
        try:
            resp = self.opener.open(req, timeout=20)
            status = resp.getcode()
            response_headers = dict(resp.headers.items())
            response_payload, text = read_json_response(resp)
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = dict(exc.headers.items())
            response_payload, text = read_json_response(exc)
        duration_ms = int((time.perf_counter() - started) * 1000)
        result = {
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
            "json": response_payload,
            "text_sample": text[:500],
            "headers": response_headers,
        }
        if expected is not None and status not in set(expected):
            raise AssertionError(f"{method} {path} expected {expected}, got {status}: {response_payload}")
        return result

    def refresh_csrf(self):
        result = self.request("GET", "/api/csrf-token", expected={200})
        token = result["json"].get("csrf_token")
        if not token:
            raise AssertionError(f"csrf token missing: {result}")
        self.csrf_token = token
        return result

    def login(self, username, password, extra=None):
        self.refresh_csrf()
        payload = {"username": username, "password": password}
        if extra:
            payload.update(extra)
        result = self.request("POST", "/api/login", payload, expected={200})
        if not result["json"].get("ok"):
            raise AssertionError(f"login failed for {username}: {result}")
        self.refresh_csrf()
        return result


class RawPathClient:
    def __init__(self, base_url):
        parsed = urllib.parse.urlparse(base_url)
        self.scheme = parsed.scheme
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        self.context = ssl._create_unverified_context()

    def get(self, raw_path, headers=None):
        conn_cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": 20}
        if self.scheme == "https":
            kwargs["context"] = self.context
        conn = conn_cls(self.host, self.port, **kwargs)
        try:
            conn.request("GET", raw_path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body) if body else {}
            except Exception:
                payload = {"_raw": body[:500]}
            return {"path": raw_path, "status": resp.status, "json": payload, "text_sample": body[:500]}
        finally:
            conn.close()


class IsolatedServer:
    def __init__(self, runtime_dir: Path, port: int):
        self.runtime_dir = runtime_dir
        self.port = port
        self.scheme = "https"
        self.process: subprocess.Popen | None = None
        self.log_path = runtime_dir / "server.log"
        self.db_dir = runtime_dir / "database"
        self.db_path = self.db_dir / "database.db"
        self.env = os.environ.copy()
        self.env.update({
            "PYTHONPATH": str(REPO_ROOT),
            "HTML_LEARNING_HOST": "0.0.0.0",
            "HTML_LEARNING_DISABLE_TRUSTED_HOSTS": "1",
            "HTML_LEARNING_PORT": str(port),
            "HTML_LEARNING_DB_DIR": str(self.db_dir),
            "HTML_LEARNING_LOG_DIR": str(runtime_dir / "logs"),
            "HTML_LEARNING_CHAT_DIR": str(runtime_dir / "chats"),
            "HTML_LEARNING_ANCHOR_DIR": str(runtime_dir / "anchors"),
            "HTML_LEARNING_STORAGE_DIR": str(runtime_dir / "storage"),
            "HTML_LEARNING_REPORTS_DIR": str(runtime_dir / "reports"),
            "HTML_LEARNING_ROOT_PASSWORD": "root",
            "HTML_LEARNING_MANAGER_PASSWORD": "admin",
            "HTML_LEARNING_TEST_PASSWORD": "test",
            "FORCE_HTTPS": "false",
            "SESSION_COOKIE_SECURE": "false",
            "HTML_LEARNING_ALLOW_LOCAL_SERVER_MODE_KEYS": "1",
            "SERVER_MODE_LOG_HMAC_KEY": "live-http-smoke-log-key",
            "SERVER_MODE_TOKEN_HMAC_KEY": "live-http-smoke-token-key",
            "SERVER_MODE_LOG_HMAC_KEY_VERSION": "live-http-v1",
            "SERVER_MODE_TOKEN_HMAC_KEY_VERSION": "live-http-v1",
            "HTML_LEARNING_BOOTSTRAP_POINTS_CHAIN": "0",
            "HTML_LEARNING_SNAPSHOT_CHECK_INTERVAL_SECONDS": "999999",
            "HTML_LEARNING_STORAGE_MAINTENANCE_CHECK_INTERVAL_SECONDS": "999999",
            "HTML_LEARNING_POINTS_BLOCK_CHECK_INTERVAL_SECONDS": "999999",
            "HTML_LEARNING_TRADING_LIQUIDATION_CHECK_INTERVAL_SECONDS": "999999",
            "HTML_LEARNING_TRADING_BOT_SCAN_INTERVAL_SECONDS": "999999",
        })

    @property
    def base_url(self):
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def start(self):
        if self.process and self.process.poll() is None:
            return
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        for subdir in (
            self.db_dir,
            self.runtime_dir / "logs",
            self.runtime_dir / "chats",
            self.runtime_dir / "anchors",
            self.runtime_dir / "storage",
            self.runtime_dir / "reports",
        ):
            subdir.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "server.py")],
            cwd=str(REPO_ROOT),
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def wait_ready(self, timeout=45):
        deadline = time.time() + timeout
        last_error = ""
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError(f"server exited early with code {self.process.returncode}; log={self.log_tail()}")
            for scheme in (self.scheme, "https", "http"):
                try:
                    client = HttpClient(f"{scheme}://127.0.0.1:{self.port}")
                    result = client.request("GET", "/api/csrf-token")
                    if result["status"] == 200:
                        self.scheme = scheme
                        return True
                except Exception as exc:
                    last_error = str(exc)
            time.sleep(0.5)
        raise TimeoutError(f"server not ready: {last_error}; log={self.log_tail()}")

    def kill9(self):
        if not self.process or self.process.poll() is not None:
            return {"killed": False, "pid": None}
        pid = self.process.pid
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        self.process.wait(timeout=10)
        return {"killed": True, "pid": pid, "returncode": self.process.returncode}

    def stop(self):
        if not self.process or self.process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            self.process.wait(timeout=10)
        except Exception:
            try:
                self.kill9()
            except Exception:
                pass

    def log_tail(self, size=4000):
        if not self.log_path.exists():
            return ""
        data = self.log_path.read_bytes()[-size:]
        return data.decode("utf-8", errors="replace")

    def db_connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


class LiveHttpRunner:
    TRAVERSAL_PAYLOADS = [
        "/api/tester/../admin",
        "/api/tester%2f../admin",
        "/api/tester%2e%2e/admin",
        "/api/tester;/admin",
        "/api/tester//../admin",
    ]

    def __init__(self, server: IsolatedServer):
        self.server = server
        self.results = []
        self.root = HttpClient(server.base_url)
        self.test_user = HttpClient(server.base_url)

    def add(self, name, ok, *, expected="", actual=None, evidence=None, severity="LOW"):
        self.results.append({
            "test_name": name,
            "timestamp": datetime.now().isoformat(),
            "ok": bool(ok),
            "severity": "INFO" if ok else severity,
            "expected_result": expected,
            "actual_result": actual or {},
            "evidence": evidence or {},
        })
        if not ok:
            raise AssertionError(f"{name} failed: {actual}")

    def run(self):
        self.server.start()
        self.server.wait_ready()
        self.check_real_http_login()
        mode, _mode_resp = self.current_mode()
        if mode != "internal_test":
            switched = self.switch_mode("internal_test", "SWITCH_TO_INTERNAL_TEST", expected={200})
            if not switched["json"].get("ok"):
                raise AssertionError(f"enter internal_test failed: {switched}")
        token = self.check_tester_token_traversal()
        self.check_log_chain_verify("before_crash")
        self.check_superweak_kill9_recovery()
        self.check_incident_lockdown_old_session_and_token(token)
        return self.report()

    def current_mode(self):
        result = self.root.request("GET", "/api/root/server-mode", expected={200})
        return (result["json"].get("mode") or {}).get("current_mode"), result

    def switch_mode(self, mode, confirm, expected={200}):
        return self.root.request(
            "POST",
            "/api/root/server-mode/switch",
            {"mode": mode, "confirm": confirm, "reason": f"live http smoke {mode}"},
            expected=expected,
        )

    def test_user_id(self):
        conn = self.server.db_connect()
        try:
            row = conn.execute("SELECT id FROM users WHERE username='test'").fetchone()
            if not row:
                raise AssertionError("test user missing")
            return int(row["id"])
        finally:
            conn.close()

    def clear_default_password_gate(self):
        conn = self.server.db_connect()
        try:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            updates = []
            if "must_change_password" in cols:
                updates.append("must_change_password=0")
            if "is_default_password" in cols:
                updates.append("is_default_password=0")
            if updates:
                conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE username IN ('root','admin','test')")
                conn.commit()
        finally:
            conn.close()

    def check_real_http_login(self):
        login = self.root.login("root", "root")
        me = self.root.request("GET", "/api/me", expected={200})
        me_username = (me["json"].get("user") or {}).get("username") or me["json"].get("username")
        ok = bool(login["json"].get("ok")) and bool(me["json"].get("ok")) and me_username == "root"
        self.add(
            "real HTTP root login with session cookie and CSRF",
            ok,
            expected="root login succeeds through /api/csrf-token + /api/login and /api/me sees root",
            actual={"login": login, "me": me},
            severity="CRITICAL",
        )
        test_login = self.test_user.login("test", "test")
        test_me = self.test_user.request("GET", "/api/me", expected={200})
        self.clear_default_password_gate()
        test_me_username = (test_me["json"].get("user") or {}).get("username") or test_me["json"].get("username")
        self.add(
            "real HTTP non-root session established",
            bool(test_login["json"].get("ok")) and test_me_username == "test",
            expected="test user can establish a normal pre-lockdown HTTP session",
            actual={"login": test_login, "me": test_me},
            severity="HIGH",
        )

    def create_tester_token(self):
        expires = (datetime.now() + timedelta(minutes=30)).isoformat()
        result = self.root.request(
            "POST",
            "/api/root/tester-token/create",
            {
                "tester_user_id": self.test_user_id(),
                "allowed_routes": ["/api/tester"],
                "allowed_features": ["server_mode_live_http_smoke"],
                "expires_at": expires,
                "max_requests_per_minute": 60,
                "can_modify_own_role": True,
                "can_modify_own_points": True,
            },
            expected={200},
        )
        if not result["json"].get("ok"):
            raise AssertionError(f"tester token create failed: {result}")
        return result["json"]

    def check_tester_token_traversal(self):
        token = self.create_tester_token()
        tester_client = HttpClient(self.server.base_url)
        allowed = tester_client.request(
            "GET",
            "/api/tester/shadow-state",
            headers={"X-Tester-Token": token["token"]},
            expected={200},
        )
        raw = RawPathClient(self.server.base_url)
        probes = []
        for payload in self.TRAVERSAL_PAYLOADS:
            probes.append(raw.get(payload, headers={"X-Tester-Token": token["token"]}))
        ok = bool(allowed["json"].get("ok")) and all(item["status"] != 200 or not item["json"].get("ok") for item in probes)
        self.add(
            "tester token traversal actual live API requests",
            ok,
            expected="normal tester route works; traversal payloads do not reach privileged API",
            actual={"allowed": allowed, "probes": probes},
            severity="CRITICAL",
        )
        return token

    def check_log_chain_verify(self, label):
        result = self.root.request("GET", "/api/server-mode/logs/verify", expected={200})
        ok = bool(result["json"].get("ok")) and result["json"].get("result") == "PASS"
        self.add(
            f"log chain verify live result {label}",
            ok,
            expected="live verify endpoint returns PASS",
            actual=result,
            severity="CRITICAL",
        )

    def check_superweak_kill9_recovery(self):
        entered = self.switch_mode("superweak", "ENABLE_SUPERWEAK", expected={200})
        if not entered["json"].get("ok"):
            raise AssertionError(f"enter superweak failed: {entered}")
        conn = self.server.db_connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS server_mode_live_dirty_probe (id INTEGER PRIMARY KEY, note TEXT)")
            conn.execute("INSERT INTO server_mode_live_dirty_probe (id, note) VALUES (1, 'dirty after superweak')")
            conn.commit()
        finally:
            conn.close()
        killed = self.server.kill9()
        self.server.start()
        self.server.wait_ready()
        self.root = HttpClient(self.server.base_url)
        self.root.login("root", "root")
        self.test_user = HttpClient(self.server.base_url)
        self.test_user.login("test", "test")
        mode, mode_result = self.current_mode()
        conn = self.server.db_connect()
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='server_mode_live_dirty_probe'"
            ).fetchone()
        finally:
            conn.close()
        ok = killed.get("killed") and mode != "superweak" and table is None
        self.add(
            "superweak true SIGKILL startup rollback",
            ok,
            expected="server process killed in superweak; restart restores checkpoint and removes dirty DB table",
            actual={"entered": entered, "killed": killed, "mode_after_restart": mode_result, "dirty_table_exists": bool(table)},
            severity="CRITICAL",
        )

    def check_incident_lockdown_old_session_and_token(self, old_token):
        fresh_token = self.create_tester_token()
        token_client = HttpClient(self.server.base_url)
        before_token = token_client.request(
            "GET",
            "/api/tester/shadow-state",
            headers={"X-Tester-Token": fresh_token["token"]},
            expected={200},
        )
        before_test_session = self.test_user.request("GET", "/api/me", expected={200})
        incident = self.root.request(
            "POST",
            "/api/root/incident/enter",
            {
                "confirm": "ENTER_INCIDENT_LOCKDOWN",
                "trigger_type": "live_http_smoke",
                "reason": "live HTTP smoke lockdown test",
                "verification": {"ok": True},
            },
            expected={200},
        )
        after_token = token_client.request(
            "GET",
            "/api/tester/shadow-state",
            headers={"X-Tester-Token": fresh_token["token"]},
            expected={401, 403, 503},
        )
        old_token_client = HttpClient(self.server.base_url)
        old_after_token = old_token_client.request(
            "GET",
            "/api/tester/shadow-state",
            headers={"X-Tester-Token": old_token["token"]},
            expected={401, 403, 503},
        )
        after_test_session = self.test_user.request("GET", "/api/tester/shadow-state", expected={401, 403, 503})
        superweak = self.switch_mode("superweak", "ENABLE_SUPERWEAK", expected={400, 403, 503})
        log_verify_blocked = self.root.request("GET", "/api/server-mode/logs/verify", expected={401, 403, 503})
        ok = (
            bool(before_token["json"].get("ok"))
            and bool(before_test_session["json"].get("ok"))
            and bool(incident["json"].get("ok"))
            and not after_token["json"].get("ok")
            and not old_after_token["json"].get("ok")
            and not after_test_session["json"].get("ok")
            and not superweak["json"].get("ok")
            and not log_verify_blocked["json"].get("ok")
        )
        self.add(
            "incident lockdown invalidates live tester tokens and blocks old non-root session actions",
            ok,
            expected="tester token invalid after lockdown; non-root old session cannot act; superweak switch and sensitive verify API blocked",
            actual={
                "before_token": before_token,
                "before_test_session": before_test_session,
                "incident": incident,
                "after_token": after_token,
                "old_after_token": old_after_token,
                "after_test_session": after_test_session,
                "superweak_switch": superweak,
                "log_verify_blocked": log_verify_blocked,
            },
            severity="CRITICAL",
        )

    def report(self):
        breaches = [item for item in self.results if not item["ok"]]
        site_production_gate_remaining = [
            "stress",
            "permission",
            "functional",
            "pentest",
            "snapshot_restore",
            "points_chain_consistency",
            "cloud_drive_quota_permission",
            "off-host append-only audit backup / immutable log replication",
        ]
        return {
            "ok": not breaches,
            "generated_at": datetime.now().isoformat(),
            "test_type": "server_mode_v2_live_http_smoke",
            "target": self.server.base_url,
            "runtime_dir": str(self.server.runtime_dir),
            "server_log": str(self.server.log_path),
            "results": self.results,
            "LIVE_HTTP_SIGNOFF_SUMMARY": {
                "tests_total": len(self.results),
                "passed_total": sum(1 for item in self.results if item["ok"]),
                "breaches_total": len(breaches),
                "critical_findings": sum(1 for item in breaches if item["severity"] == "CRITICAL"),
                "high_findings": sum(1 for item in breaches if item["severity"] == "HIGH"),
                "covered_gaps": [
                    "real Flask before_request + browser/session cookie stack",
                    "live HTTP deployment requests",
                    "true SIGKILL server process during superweak",
                    "live log chain verify endpoint",
                ],
                "uncovered_risks": [
                    "off-host append-only log replication / filesystem immutable storage is still not verified",
                ],
                "production_readiness": "YES" if not breaches else "NO",
                "note": "Server Mode v2 production_ready does not equal whole-site production_ready.",
                "site_production_gate_remaining": site_production_gate_remaining,
            },
        }


def write_reports(report: dict, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"server_mode_v2_live_http_smoke_{ts}.json"
    md_path = out_dir / f"server_mode_v2_live_http_smoke_{ts}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Server Mode v2 Live HTTP Smoke Report",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Target: `{report['target']}`",
        f"- Result: `{'PASS' if report['ok'] else 'FAIL'}`",
        "",
        "## LIVE_HTTP_SIGNOFF_SUMMARY",
        "",
    ]
    for key, value in report["LIVE_HTTP_SIGNOFF_SUMMARY"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Evidence", ""])
    for item in report.get("results", []):
        lines.extend([
            f"### {item['test_name']}",
            "",
            f"- status: `{'PASS' if item['ok'] else 'FAIL'}`",
            f"- severity: `{item['severity']}`",
            f"- expected: {item['expected_result']}",
            "",
            "```json",
            json.dumps(item["actual_result"], ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main():
    parser = argparse.ArgumentParser(description="Run live HTTP Server Mode v2 smoke test against an isolated server.")
    parser.add_argument("--out", default=str(security_reports_root()))
    parser.add_argument("--keep-runtime", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    runtime_root = Path(tempfile.mkdtemp(prefix="hackme_server_mode_v2_live_"))
    server = IsolatedServer(runtime_root, args.port or free_port())
    report = None
    try:
        report = LiveHttpRunner(server).run()
        json_path, md_path = write_reports(report, Path(args.out))
        print(json.dumps({
            "ok": report["ok"],
            "summary": report["LIVE_HTTP_SIGNOFF_SUMMARY"],
            "json_report": str(json_path),
            "md_report": str(md_path),
            "runtime_dir": str(runtime_root),
        }, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    except Exception as exc:
        report = {
            "ok": False,
            "generated_at": datetime.now().isoformat(),
            "test_type": "server_mode_v2_live_http_smoke",
            "target": server.base_url,
            "runtime_dir": str(runtime_root),
            "server_log": str(server.log_path),
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "server_log_tail": server.log_tail(),
            "LIVE_HTTP_SIGNOFF_SUMMARY": {
                "tests_total": 0,
                "passed_total": 0,
                "breaches_total": 1,
                "critical_findings": 1,
                "high_findings": 0,
                "covered_gaps": [],
                "uncovered_risks": ["live HTTP smoke failed before completing all evidence checks"],
                "production_readiness": "NO",
            },
        }
        json_path, md_path = write_reports(report, Path(args.out))
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "json_report": str(json_path),
            "md_report": str(md_path),
            "runtime_dir": str(runtime_root),
        }, ensure_ascii=False, indent=2))
        return 1
    finally:
        server.stop()
        if not args.keep_runtime:
            shutil.rmtree(runtime_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
