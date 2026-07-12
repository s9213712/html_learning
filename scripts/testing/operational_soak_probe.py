#!/usr/bin/env python3
"""Run a multi-account, full-feature operational soak against an isolated server."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.operation_coverage import (  # noqa: E402
    ACCOUNT_SUCCESS_REQUIRED_OPERATIONS,
    GLOBAL_SUCCESS_REQUIRED_OPERATIONS,
)

SYSTEM_STRESS = ROOT / "scripts" / "testing" / "system_stress_probe.py"
POINTS_STRESS = ROOT / "scripts" / "testing" / "points_chain_destructive_stress.py"
PLAYWRIGHT_DEEP = ROOT / "scripts" / "testing" / "playwright_deep_site_check.py"
DB_STRESS = ROOT / "scripts" / "testing" / "db_stress_probe.py"
OPERATION_COVERAGE = ROOT / "scripts" / "testing" / "operation_coverage.py"
MIN_SIGNOFF_SECONDS = 8 * 60 * 60
HARNESS_FILES = (
    Path(__file__).resolve(),
    SYSTEM_STRESS,
    POINTS_STRESS,
    PLAYWRIGHT_DEEP,
    DB_STRESS,
    OPERATION_COVERAGE,
)
SENSITIVE_COMMAND_FLAGS = {
    "--root-password",
    "--manager-password",
    "--test-password",
    "--account-password",
    "--accounts",
}


def validate_run_policy(base_url: str, runtime_root: Path, *, owns_target: bool) -> None:
    parsed = urlparse(str(base_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base URL must be an absolute http(s) URL")
    hostname = parsed.hostname.lower()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if not loopback and not owns_target:
        raise ValueError("non-loopback destructive soak target requires --i-own-this-target")
    tmp_root = Path("/tmp").resolve()
    resolved_runtime = runtime_root.resolve()
    if resolved_runtime != tmp_root and tmp_root not in resolved_runtime.parents:
        raise ValueError("operational soak runtime and reports must remain under /tmp")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * pct)))
    return round(ordered[index], 3)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "path": str(path)}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def harness_hashes() -> dict[str, str]:
    result = {}
    for path in HARNESS_FILES:
        resolved = Path(path).resolve()
        result[str(resolved.relative_to(ROOT))] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return result


def harness_drift(expected: dict[str, str]) -> dict[str, dict[str, str]]:
    current = harness_hashes()
    return {
        name: {"expected": digest, "actual": current.get(name, "missing")}
        for name, digest in expected.items()
        if current.get(name) != digest
    }


class ApiClient:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = float(timeout)
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""
        # A 401 recovery calls login() while request() still owns this lock.
        self.lock = threading.RLock()

    def refresh_csrf(self) -> bool:
        response = self.session.get(f"{self.base_url}/api/csrf-token", timeout=self.timeout)
        if response.status_code != 200:
            return False
        try:
            self.csrf = str(response.json().get("csrf_token") or self.session.cookies.get("csrf_token") or "")
        except Exception:
            self.csrf = str(self.session.cookies.get("csrf_token") or "")
        return bool(self.csrf)

    def login(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self.refresh_csrf()
            response = self.session.post(
                f"{self.base_url}/api/login",
                json={"username": self.username, "password": self.password},
                headers={"X-CSRF-Token": self.csrf},
                timeout=self.timeout,
            )
            self.refresh_csrf()
            return self.capture(response, started)
        except Exception as exc:
            return {
                "ok": False,
                "status": 0,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
            }

    def capture(self, response: requests.Response, started: float) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:500]}
        if not isinstance(payload, dict):
            payload = {"body": payload}
        return {
            "ok": 200 <= response.status_code < 300 and payload.get("ok") is not False,
            "status": int(response.status_code),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "body": payload,
            "backpressure_rejected": response.headers.get("X-Hackme-Backpressure-Rejected") == "1",
        }

    def request(self, method: str, path: str, *, json_body: dict | None = None) -> dict[str, Any]:
        method = method.upper()
        with self.lock:
            started = time.perf_counter()
            try:
                headers: dict[str, str] = {}
                if method not in {"GET", "HEAD", "OPTIONS"}:
                    if not self.csrf:
                        self.refresh_csrf()
                    headers["X-CSRF-Token"] = self.csrf
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 401:
                    self.login()
                    if method not in {"GET", "HEAD", "OPTIONS"}:
                        headers["X-CSRF-Token"] = self.csrf
                    started = time.perf_counter()
                    response = self.session.request(
                        method,
                        f"{self.base_url}{path}",
                        json=json_body,
                        headers=headers,
                        timeout=self.timeout,
                    )
                return self.capture(response, started)
            except Exception as exc:
                return {
                    "ok": False,
                    "status": 0,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error": f"{exc.__class__.__name__}: {str(exc)[:400]}",
                    "body": {},
                }


class SentinelStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts: Counter = Counter()
        self.statuses: dict[str, Counter] = defaultdict(Counter)
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self.body_not_ready: Counter = Counter()
        self.server_busy: Counter = Counter()
        self.errors: list[dict[str, Any]] = []

    def record(self, role: str, path: str, result: dict[str, Any]) -> None:
        key = f"{role}:{path}"
        status = int(result.get("status") or 0)
        http_only = path == "/api/root/server-mode/requirements"
        body = result.get("body") if isinstance(result.get("body"), dict) else {}
        controlled_busy = (
            status == 503
            and bool(result.get("backpressure_rejected"))
            and str(body.get("error") or "").strip().lower() == "server_busy"
        )
        with self.lock:
            self.counts[key] += 1
            self.statuses[key][str(status)] += 1
            self.latencies[key].append(float(result.get("elapsed_ms") or 0.0))
            if status == 200 and not result.get("ok") and http_only:
                self.body_not_ready[key] += 1
            if controlled_busy:
                self.server_busy[key] += 1
            if (status != 200 and not controlled_busy) or (not result.get("ok") and not http_only and not controlled_busy):
                if len(self.errors) < 100:
                    self.errors.append({
                        "at": utc_now(),
                        "role": role,
                        "path": path,
                        "status": status,
                        "error": str(result.get("error") or (result.get("body") or {}).get("msg") or "")[:400],
                    })

    def summary(self) -> dict[str, Any]:
        with self.lock:
            checks = {}
            for key, count in sorted(self.counts.items()):
                values = list(self.latencies.get(key) or [])
                checks[key] = {
                    "count": int(count),
                    "statuses": dict(sorted(self.statuses.get(key, Counter()).items())),
                    "p95_ms": percentile(values, 0.95),
                    "max_ms": round(max(values), 3) if values else 0.0,
                    "body_not_ready": int(self.body_not_ready.get(key, 0)),
                    "server_busy": int(self.server_busy.get(key, 0)),
                }
            total_checks = int(sum(self.counts.values()))
            server_busy = int(sum(self.server_busy.values()))
            all_latencies = [value for values in self.latencies.values() for value in values]
            return {
                "checks": checks,
                "errors": list(self.errors),
                "total_checks": total_checks,
                "server_busy": server_busy,
                "server_busy_rate": round(server_busy / total_checks, 6) if total_checks else 0.0,
                "p95_ms": percentile(all_latencies, 0.95),
                "max_ms": round(max(all_latencies), 3) if all_latencies else 0.0,
            }


ROOT_SENTINELS = (
    "/api/admin/health/readiness",
    "/api/admin/security-center",
    "/api/root/server-mode/requirements",
    "/api/root/server-mode/logs/verify",
    "/api/ai-agent/readonly?scope=resources&limit=10",
)
MANAGER_SENTINELS = (
    "/api/admin/users?page_size=10",
    "/api/admin/reports?status=pending&page=1",
    "/api/community/boards",
    "/api/notifications/unread-count",
)


def sentinel_loop(
    stop: threading.Event,
    start: threading.Event,
    stats: SentinelStats,
    root: ApiClient,
    manager: ApiClient,
    interval_seconds: float,
) -> None:
    start.wait()
    while not stop.is_set():
        cycle_started = time.monotonic()
        for role, client, paths in (("root", root, ROOT_SENTINELS), ("manager", manager, MANAGER_SENTINELS)):
            for path in paths:
                if stop.is_set():
                    return
                stats.record(role, path, client.request("GET", path))
        elapsed = time.monotonic() - cycle_started
        stop.wait(max(0.2, float(interval_seconds) - elapsed))


def provision_accounts(root: ApiClient, *, prefix: str, count: int, password: str) -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    for index in range(1, max(1, int(count)) + 1):
        username = f"{prefix}{index:02d}"
        search = root.request("GET", f"/api/admin/users?q={username}&page_size=100")
        users = (search.get("body") or {}).get("users") or []
        if not any(str(item.get("username") or "") == username for item in users):
            created = root.request(
                "POST",
                "/api/admin/users",
                json_body={
                    "username": username,
                    "password": password,
                    "password_confirm": password,
                    "nickname": f"Operational Sim {index:02d}",
                    "role": "user",
                    "status": "active",
                    "member_level": "normal",
                },
            )
            if int(created.get("status") or 0) not in {200, 201, 409}:
                raise RuntimeError(f"failed to provision {username}: {created}")
        probe = ApiClient(root.base_url, username, password)
        login = probe.login()
        if not login.get("ok"):
            raise RuntimeError(f"provisioned account cannot login: {username}: {login}")
        accounts.append((username, password))
    return accounts


def sanitized_command(command: list[str]) -> list[str]:
    redacted = []
    hide_next = False
    for value in command:
        if hide_next:
            redacted.append("[redacted]")
            hide_next = False
            continue
        matched_flag = next((flag for flag in SENSITIVE_COMMAND_FLAGS if value.startswith(f"{flag}=")), "")
        if matched_flag:
            redacted.append(f"{matched_flag}=[redacted]")
            continue
        redacted.append(value)
        if value in SENSITIVE_COMMAND_FLAGS:
            hide_next = True
    return redacted


def run_command(command: list[str], *, stdout_path: Path, timeout: int, env: dict[str, str] | None = None) -> dict[str, Any]:
    state = start_command(command, stdout_path=stdout_path, env=env)
    return finish_command(state, timeout=timeout)


def start_command(command: list[str], *, stdout_path: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT) + (os.pathsep + merged_env["PYTHONPATH"] if merged_env.get("PYTHONPATH") else "")
    if env:
        merged_env.update(env)
    handle = stdout_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=merged_env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return {
        "process": process,
        "handle": handle,
        "stdout": str(stdout_path),
        "command": sanitized_command(command),
        "started_monotonic": time.monotonic(),
    }


def finish_command(state: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    process: subprocess.Popen = state["process"]
    timed_out = False
    try:
        returncode = int(process.wait(timeout=max(1, int(timeout))))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            returncode = int(process.wait(timeout=10))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        with open(state["stdout"], "a", encoding="utf-8") as handle:
            handle.write(f"\n[TIMEOUT] exceeded {timeout}s; process group terminated\n")
        returncode = 124
    finally:
        state["handle"].close()
    return {
        "returncode": returncode,
        "elapsed_seconds": round(time.monotonic() - float(state["started_monotonic"]), 3),
        "stdout": state["stdout"],
        "command": state["command"],
        "timed_out": timed_out,
    }


def aggregate_rounds(round_payloads: list[dict[str, Any]], configured_accounts: list[str]) -> dict[str, Any]:
    total_ops = 0
    hard_failures = 0
    server_busy = 0
    observed_operations: set[str] = set()
    registered_operations: set[str] = set()
    account_ops: Counter = Counter()
    operation_successes: Counter = Counter()
    account_successes: dict[str, Counter] = defaultdict(Counter)
    round_failures = []
    for index, payload in enumerate(round_payloads, start=1):
        summary = payload.get("summary") or {}
        total_ops += int(summary.get("total_ops") or 0)
        hard_failures += int(summary.get("hard_failures_excluding_controlled_503", summary.get("hard_failures_excluding_503")) or 0)
        server_busy += int(summary.get("server_busy_503") or 0)
        observed_operations.update((summary.get("ops") or {}).keys())
        registered_operations.update(payload.get("registered_operations") or [])
        for operation, evidence in (summary.get("ops") or {}).items():
            operation_successes[str(operation)] += int((evidence or {}).get("successful_2xx") or 0)
        for account, count in (payload.get("account_operation_counts") or {}).items():
            account_ops[str(account)] += int(count or 0)
        for account, evidence in (summary.get("accounts") or {}).items():
            for operation, count in ((evidence or {}).get("successful_operations") or {}).items():
                account_successes[str(account)][str(operation)] += int(count or 0)
        if payload.get("ok") is False:
            round_failures.append({"round": index, "degraded_reasons": payload.get("degraded_reasons") or [], "error": payload.get("error") or ""})
    account_success_gaps = {
        account: sorted(
            operation
            for operation in ACCOUNT_SUCCESS_REQUIRED_OPERATIONS
            if int(account_successes.get(account, Counter()).get(operation, 0)) <= 0
        )
        for account in configured_accounts
    }
    account_success_gaps = {account: gaps for account, gaps in account_success_gaps.items() if gaps}
    return {
        "rounds": len(round_payloads),
        "round_failures": round_failures,
        "total_ops": total_ops,
        "hard_failures": hard_failures,
        "hard_failure_rate": round(hard_failures / total_ops, 6) if total_ops else 0.0,
        "server_busy": server_busy,
        "server_busy_rate": round(server_busy / total_ops, 6) if total_ops else 0.0,
        "registered_operations": sorted(registered_operations),
        "observed_operations": sorted(observed_operations),
        "missing_operations": sorted(registered_operations - observed_operations),
        "account_operation_counts": {account: int(account_ops.get(account, 0)) for account in configured_accounts},
        "accounts_without_operations": [account for account in configured_accounts if int(account_ops.get(account, 0)) <= 0],
        "successful_operation_counts": {
            operation: int(operation_successes.get(operation, 0))
            for operation in sorted(registered_operations)
        },
        "operations_without_success": sorted(
            operation
            for operation in GLOBAL_SUCCESS_REQUIRED_OPERATIONS
            if int(operation_successes.get(operation, 0)) <= 0
        ),
        "account_success_counts": {
            account: dict(sorted(account_successes.get(account, Counter()).items()))
            for account in configured_accounts
        },
        "account_success_gaps": account_success_gaps,
    }


def aggregate_resource_evidence(round_payloads: list[dict[str, Any]], server_pids: str) -> dict[str, Any]:
    summaries = [
        payload.get("resource_monitor")
        for payload in round_payloads
        if isinstance(payload.get("resource_monitor"), dict)
    ]
    summaries = [summary for summary in summaries if summary]
    monitored_pids_seen = sorted({
        int(pid)
        for summary in summaries
        for pid in (summary.get("monitored_pids_seen") or [])
    })
    first_sample = next((summary.get("first_sample") for summary in summaries if summary.get("first_sample")), {})
    last_sample = next((summary.get("last_sample") for summary in reversed(summaries) if summary.get("last_sample")), {})
    db_peak: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        for label, evidence in (summary.get("db_peak") or {}).items():
            target = db_peak.setdefault(str(label), {})
            for key in ("max_db_mb", "max_wal_mb", "max_shm_mb", "max_page_count", "max_freelist_count"):
                target[key] = max(float(target.get(key) or 0), float((evidence or {}).get(key) or 0))
            if (evidence or {}).get("last"):
                target["last"] = evidence["last"]
    return {
        "server_pids": [part for part in str(server_pids or "").replace(",", " ").split() if part],
        "rounds_with_resource_evidence": len(summaries),
        "sample_count": sum(int(summary.get("sample_count") or 0) for summary in summaries),
        "monitored_rss_first_mb": float((first_sample or {}).get("monitored_rss_mb") or 0),
        "monitored_rss_last_mb": float((last_sample or {}).get("monitored_rss_mb") or 0),
        "monitored_rss_max_mb": max((float(summary.get("monitored_rss_max_mb") or 0) for summary in summaries), default=0.0),
        "monitored_pid_count_max": max((int(summary.get("monitored_pid_count_max") or 0) for summary in summaries), default=0),
        "monitored_pids_seen": monitored_pids_seen,
        "mem_available_min_mb": min((float(summary.get("mem_available_min_mb")) for summary in summaries if summary.get("mem_available_min_mb") is not None), default=None),
        "runtime_disk_free_min_mb": min((float(summary.get("runtime_disk_free_min_mb")) for summary in summaries if summary.get("runtime_disk_free_min_mb") is not None), default=None),
        "db_peak": db_peak,
        "first_sample": first_sample or {},
        "last_sample": last_sample or {},
    }


def final_control_request(client: ApiClient, path: str, *, attempts: int = 3) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "status": 0, "body": {}}
    for attempt in range(1, max(1, int(attempts)) + 1):
        result = client.request("GET", path)
        result["attempts"] = attempt
        body = result.get("body") if isinstance(result.get("body"), dict) else {}
        controlled_busy = (
            int(result.get("status") or 0) == 503
            and bool(result.get("backpressure_rejected"))
            and body.get("error") == "server_busy"
        )
        if not controlled_busy or attempt >= attempts:
            return result
        retry_after = float(body.get("retry_after_seconds") or 0.25)
        time.sleep(max(0.25, min(3.0, retry_after)))
    return result


def latest_playwright_report(runtime_root: Path) -> Path | None:
    reports = sorted(runtime_root.glob("reports/qa/playwright_deep_site_check_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def write_markdown(path: Path, payload: dict[str, Any]) -> Path:
    markdown = path.with_suffix(".md")
    aggregate = payload.get("aggregate") or {}
    lines = [
        "# Operational Soak Probe",
        "",
        f"- Verdict: `{payload.get('verdict')}`",
        f"- Requested duration: `{payload.get('requested_duration_seconds')}s`",
        f"- Actual duration: `{payload.get('actual_duration_seconds')}s`",
        f"- Accounts: `{len(payload.get('accounts') or [])}`",
        f"- Concurrent operations: `{payload.get('concurrency')}`",
        f"- Rounds: `{aggregate.get('rounds', 0)}`",
        f"- Total operations: `{aggregate.get('total_ops', 0)}`",
        f"- Hard failure rate: `{aggregate.get('hard_failure_rate', 0)}`",
        f"- Server busy rate: `{aggregate.get('server_busy_rate', 0)}`",
        f"- Positive-path operations without success: `{len(aggregate.get('operations_without_success') or [])}`",
        f"- Accounts with positive-path gaps: `{len(aggregate.get('account_success_gaps') or {})}`",
        "",
        "## Findings",
        "",
    ]
    findings = payload.get("findings") or []
    lines.extend(f"- `{item.get('severity', 'unknown')}` {item.get('title', item)}" for item in findings)
    if not findings:
        lines.append("- none")
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return markdown


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a configured-duration multi-account synchronous operational simulation.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--server-runtime-root", default="", help="Actual isolated server runtime containing database/, logs/, and secrets/")
    parser.add_argument("--out", default="")
    parser.add_argument("--duration-seconds", type=int, default=MIN_SIGNOFF_SECONDS)
    parser.add_argument("--allow-short-duration", action="store_true", help="Development smoke only; short runs are never production sign-off evidence")
    parser.add_argument("--account-count", type=int, default=8)
    parser.add_argument("--account-prefix", default="opsim")
    parser.add_argument("--account-password", default=os.environ.get("HACKME_SOAK_ACCOUNT_PASSWORD", ""))
    parser.add_argument("--root-username", default="root")
    parser.add_argument("--root-password", default=os.environ.get("HACKME_SOAK_ROOT_PASSWORD", ""))
    parser.add_argument("--manager-username", default="admin")
    parser.add_argument("--manager-password", default=os.environ.get("HACKME_SOAK_MANAGER_PASSWORD", ""))
    parser.add_argument("--test-password", default=os.environ.get("HACKME_SOAK_TEST_PASSWORD", os.environ.get("PLAYWRIGHT_TEST_PASSWORD", "")))
    parser.add_argument("--server-pids", default=os.environ.get("HACKME_SERVER_PIDS", ""), help="Comma/space separated server master and worker PIDs for RSS evidence")
    parser.add_argument("--round-ops", type=int, default=800)
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--session-pool", type=int, default=16)
    parser.add_argument("--round-timeout-seconds", type=int, default=1800)
    parser.add_argument("--sentinel-interval-seconds", type=float, default=10.0)
    parser.add_argument("--max-server-busy-rate", type=float, default=0.05)
    parser.add_argument("--max-ordinary-p95-ms", type=float, default=3000.0)
    parser.add_argument("--max-ordinary-p99-ms", type=float, default=8000.0)
    parser.add_argument("--max-sentinel-p95-ms", type=float, default=3000.0)
    parser.add_argument("--browser-interval-seconds", type=int, default=4 * 60 * 60)
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--skip-points-stress", action="store_true")
    parser.add_argument("--i-own-this-target", action="store_true", help="Required for destructive testing against a non-loopback target")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requests.packages.urllib3.disable_warnings()
    if args.duration_seconds < MIN_SIGNOFF_SECONDS and not args.allow_short_duration:
        raise SystemExit(f"production operational soak requires at least {MIN_SIGNOFF_SECONDS} seconds; use --allow-short-duration only for development smoke")
    if args.account_count < 2:
        raise SystemExit("operational soak requires at least two member accounts")
    missing_secrets = [
        name
        for name, value in (
            ("HACKME_SOAK_ROOT_PASSWORD/--root-password", args.root_password),
            ("HACKME_SOAK_MANAGER_PASSWORD/--manager-password", args.manager_password),
            ("HACKME_SOAK_ACCOUNT_PASSWORD/--account-password", args.account_password),
            ("HACKME_SOAK_TEST_PASSWORD/--test-password", args.test_password),
        )
        if not str(value or "")
    ]
    if missing_secrets:
        raise SystemExit("missing required credentials: " + ", ".join(missing_secrets))

    runtime_root = Path(args.runtime_root).resolve()
    try:
        validate_run_policy(args.base_url, runtime_root, owns_target=bool(args.i_own_this_target))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not args.server_runtime_root and not args.skip_points_stress:
        raise SystemExit("--server-runtime-root is required when PointsChain stress is enabled")
    server_runtime_root = Path(args.server_runtime_root or args.runtime_root).resolve()
    try:
        validate_run_policy(args.base_url, server_runtime_root, owns_target=bool(args.i_own_this_target))
    except ValueError as exc:
        raise SystemExit(f"invalid --server-runtime-root: {exc}") from exc
    report_dir = runtime_root / "reports" / "operational_soak"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out).resolve() if args.out else report_dir / f"operational_soak_{int(time.time())}.json"
    if out_path != runtime_root and runtime_root not in out_path.parents:
        raise SystemExit("--out must remain under the selected /tmp runtime root")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = report_dir / "operational_soak.checkpoint.json"
    source_harness_hashes = harness_hashes()

    root = ApiClient(args.base_url, args.root_username, args.root_password)
    manager = ApiClient(args.base_url, args.manager_username, args.manager_password)
    root_login = root.login()
    manager_login = manager.login()
    if not root_login.get("ok") or not manager_login.get("ok"):
        payload = {"ok": False, "verdict": "FAIL", "error": "privileged login failed", "root_login": root_login, "manager_login": manager_login}
        atomic_write_json(out_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    accounts = provision_accounts(
        root,
        prefix=str(args.account_prefix),
        count=max(2, int(args.account_count)),
        password=str(args.account_password),
    )
    account_names = [username for username, _password in accounts]
    account_spec = ",".join(f"{username}:{password}" for username, password in accounts)

    stop_sentinel = threading.Event()
    start_sentinel = threading.Event()
    sentinel_stats = SentinelStats()
    sentinel_thread = threading.Thread(
        target=sentinel_loop,
        args=(stop_sentinel, start_sentinel, sentinel_stats, root, manager, args.sentinel_interval_seconds),
        daemon=True,
    )
    sentinel_thread.start()

    started_at = utc_now()
    started = time.monotonic()
    deadline = started + max(1, int(args.duration_seconds))
    start_sentinel.set()
    round_payloads: list[dict[str, Any]] = []
    round_runs: list[dict[str, Any]] = []
    browser_runs: list[dict[str, Any]] = []
    browser_state: dict[str, Any] | None = None
    next_browser_at = started
    points_state: dict[str, Any] | None = None
    points_run: dict[str, Any] | None = None
    points_payload: dict[str, Any] | None = None
    points_report = report_dir / "points_stress.json"
    detected_harness_drift: dict[str, dict[str, str]] = {}

    def write_checkpoint(phase: str) -> None:
        elapsed = time.monotonic() - started
        atomic_write_json(checkpoint_path, {
            "status": "running",
            "phase": phase,
            "production_signoff_eligible": False,
            "started_at": started_at,
            "updated_at": utc_now(),
            "elapsed_seconds": round(elapsed, 3),
            "remaining_seconds": round(max(0.0, deadline - time.monotonic()), 3),
            "accounts": account_names,
            "rounds_completed": len(round_payloads),
            "aggregate": aggregate_rounds(round_payloads, account_names),
            "sentinel": sentinel_stats.summary(),
            "points_stress": {
                "started": points_state is not None or points_run is not None,
                "completed": points_run is not None,
                "returncode": (points_run or {}).get("returncode"),
            },
            "browser_runs_completed": len(browser_runs),
            "browser_running": browser_state is not None,
            "source_harness_hashes": source_harness_hashes,
            "harness_drift": detected_harness_drift,
            "report": str(out_path),
        })

    write_checkpoint("initial_setup")

    if not args.skip_points_stress:
        points_command = [
            sys.executable,
            str(POINTS_STRESS),
            "--base-url", args.base_url,
            "--runtime-root", str(server_runtime_root),
            "--out", str(points_report),
            "--accounts", str(max(4, min(12, args.account_count))),
            "--transfer-ops", "36",
            "--direct-transfer-ops", "120",
            "--trading-ops", "24",
            "--concurrency", str(max(4, min(12, args.concurrency))),
        ]
        if args.server_pids:
            points_command.extend(["--server-pids", str(args.server_pids)])
        points_state = start_command(
            points_command,
            stdout_path=report_dir / "points_stress.stdout",
            env={"HACKME_POINTS_STRESS_ROOT_PASSWORD": args.root_password},
        )

    round_index = 0
    try:
        while time.monotonic() < deadline:
            detected_harness_drift = harness_drift(source_harness_hashes)
            if detected_harness_drift:
                write_checkpoint("harness_drift_detected")
                break
            now = time.monotonic()
            if not args.skip_browser and browser_state is None and now >= next_browser_at:
                browser_root = report_dir / f"browser_{len(browser_runs) + 1:03d}"
                browser_state = start_command(
                    [
                        sys.executable,
                        str(PLAYWRIGHT_DEEP),
                        "--base-url", args.base_url,
                        "--runtime-root", str(browser_root),
                        "--max-chess-human-moves", "6",
                    ],
                    stdout_path=browser_root / "playwright.stdout",
                    env={
                        "PLAYWRIGHT_ROOT_PASSWORD": args.root_password,
                        "PLAYWRIGHT_MANAGER_PASSWORD": args.manager_password,
                        "PLAYWRIGHT_TEST_PASSWORD": args.test_password,
                    },
                )
                next_browser_at = now + max(300, int(args.browser_interval_seconds))

            round_index += 1
            round_path = report_dir / f"system_round_{round_index:05d}.json"
            command = [
                sys.executable,
                str(SYSTEM_STRESS),
                "--base-url", args.base_url,
                "--runtime-root", str(server_runtime_root),
                "--out", str(round_path),
                "--session-mode", "clone",
                "--session-pool", str(max(args.account_count, int(args.session_pool))),
                "--logical-users", str(max(args.round_ops, args.account_count)),
                "--ops", str(max(args.round_ops, args.account_count)),
                "--concurrency", str(max(2, int(args.concurrency))),
                "--operation-mode", "rotation",
                "--require-all-accounts",
                "--require-operation-coverage",
                "--require-operation-success",
                "--require-account-success",
                "--allow-server-busy",
                "--max-server-busy-rate", str(max(0.0, min(1.0, args.max_server_busy_rate))),
                "--max-ordinary-p95-ms", str(max(1.0, float(args.max_ordinary_p95_ms))),
                "--max-ordinary-p99-ms", str(max(1.0, float(args.max_ordinary_p99_ms))),
                "--max-hf-generates", "0",
            ]
            if args.server_pids:
                command.extend(["--server-pids", str(args.server_pids)])
            run = run_command(
                command,
                stdout_path=report_dir / f"system_round_{round_index:05d}.stdout",
                timeout=max(60, int(args.round_timeout_seconds)),
                env={
                    "HACKME_STRESS_ACCOUNTS": account_spec,
                    "HACKME_STRESS_TEST_PASSWORD": args.account_password,
                },
            )
            round_runs.append(run)
            payload = load_json(round_path)
            payload["_artifact_path"] = str(round_path)
            payload["_returncode"] = run["returncode"]
            round_payloads.append(payload)

            if points_state is not None and points_state["process"].poll() is not None:
                points_run = finish_command(points_state, timeout=5)
                points_payload = load_json(points_report)
                points_state = None

            if browser_state is not None and browser_state["process"].poll() is not None:
                state = finish_command(browser_state, timeout=5)
                browser_root = Path(state["stdout"]).parent
                report_path = latest_playwright_report(browser_root)
                state["report"] = str(report_path) if report_path else ""
                state["result"] = load_json(report_path) if report_path else {"ok": False, "error": "playwright report missing"}
                browser_runs.append(state)
                browser_state = None

            write_checkpoint(f"round_{round_index:05d}_completed")

            elapsed = time.monotonic() - started
            print(
                json.dumps(
                    {
                        "event": "operational_soak_heartbeat",
                        "elapsed_seconds": round(elapsed, 1),
                        "remaining_seconds": round(max(0.0, deadline - time.monotonic()), 1),
                        "round": round_index,
                        "round_ok": payload.get("ok"),
                        "round_ops": (payload.get("summary") or {}).get("total_ops"),
                        "browser_runs_completed": len(browser_runs),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        stop_sentinel.set()
        sentinel_thread.join(timeout=30)

    if points_state is not None:
        points_run = finish_command(points_state, timeout=max(60, int(args.round_timeout_seconds)))
        points_payload = load_json(points_report)
        points_state = None
    elif points_run is None:
        points_run = {"returncode": 0, "skipped": True}
        points_payload = {"ok": True, "skipped": True}

    if browser_state is not None:
        state = finish_command(browser_state, timeout=max(300, int(args.round_timeout_seconds)))
        browser_root = Path(state["stdout"]).parent
        report_path = latest_playwright_report(browser_root)
        state["report"] = str(report_path) if report_path else ""
        state["result"] = load_json(report_path) if report_path else {"ok": False, "error": "playwright report missing"}
        browser_runs.append(state)

    if not args.skip_browser and not detected_harness_drift:
        detected_harness_drift = harness_drift(source_harness_hashes)
    if not args.skip_browser and not detected_harness_drift:
        browser_root = report_dir / f"browser_{len(browser_runs) + 1:03d}_final"
        state = run_command(
            [
                sys.executable,
                str(PLAYWRIGHT_DEEP),
                "--base-url", args.base_url,
                "--runtime-root", str(browser_root),
                "--max-chess-human-moves", "6",
            ],
            stdout_path=browser_root / "playwright.stdout",
            timeout=max(600, int(args.round_timeout_seconds)),
            env={
                "PLAYWRIGHT_ROOT_PASSWORD": args.root_password,
                "PLAYWRIGHT_MANAGER_PASSWORD": args.manager_password,
                "PLAYWRIGHT_TEST_PASSWORD": args.test_password,
            },
        )
        report_path = latest_playwright_report(browser_root)
        state["report"] = str(report_path) if report_path else ""
        state["result"] = load_json(report_path) if report_path else {"ok": False, "error": "playwright report missing"}
        browser_runs.append(state)

    actual_duration = time.monotonic() - started
    aggregate = aggregate_rounds(round_payloads, account_names)
    resource_evidence = aggregate_resource_evidence(round_payloads, args.server_pids)
    sentinel = sentinel_stats.summary()
    final_checks = {
        "health_readiness": final_control_request(root, "/api/admin/health/readiness"),
        "security_center": final_control_request(root, "/api/admin/security-center"),
        "log_chain": final_control_request(root, "/api/root/server-mode/logs/verify"),
        "points_wallet": final_control_request(root, "/api/points/wallet"),
        "ai_agent_status": final_control_request(root, "/api/ai-agent/status"),
    }

    findings = []
    if actual_duration + 1 < int(args.duration_seconds):
        findings.append({"severity": "critical", "title": "requested soak duration was not completed"})
    if detected_harness_drift:
        findings.append({"severity": "critical", "title": "test harness source changed during the run", "files": detected_harness_drift})
    if aggregate["round_failures"]:
        findings.append({"severity": "high", "title": "one or more synchronized system rounds failed", "count": len(aggregate["round_failures"])})
    if aggregate["hard_failures"]:
        findings.append({"severity": "high", "title": "transport or HTTP 5xx failures occurred", "count": aggregate["hard_failures"]})
    if aggregate["server_busy_rate"] > max(0.0, min(1.0, args.max_server_busy_rate)):
        findings.append({"severity": "high", "title": "server_busy rate exceeded configured SLA", "rate": aggregate["server_busy_rate"]})
    if aggregate["missing_operations"]:
        findings.append({"severity": "high", "title": "full-function operation rotation incomplete", "missing": aggregate["missing_operations"]})
    if aggregate["accounts_without_operations"]:
        findings.append({"severity": "high", "title": "configured accounts received no operations", "accounts": aggregate["accounts_without_operations"]})
    if aggregate["operations_without_success"]:
        findings.append({"severity": "high", "title": "required positive-path operations never returned 2xx", "operations": aggregate["operations_without_success"]})
    if aggregate["account_success_gaps"]:
        findings.append({"severity": "high", "title": "one or more accounts missed required positive-path success", "accounts": aggregate["account_success_gaps"]})
    if sentinel["errors"]:
        findings.append({"severity": "high", "title": "root/manager sentinel observed failures", "count": len(sentinel["errors"])})
    if float(sentinel.get("server_busy_rate") or 0.0) > max(0.0, min(1.0, args.max_server_busy_rate)):
        findings.append({"severity": "high", "title": "root/manager sentinel server-busy rate exceeded SLA", "rate": sentinel.get("server_busy_rate")})
    slow_sentinels = {
        name: evidence.get("p95_ms")
        for name, evidence in (sentinel.get("checks") or {}).items()
        if float(evidence.get("p95_ms") or 0.0) > max(1.0, float(args.max_sentinel_p95_ms))
    }
    if slow_sentinels:
        findings.append({"severity": "high", "title": "root/manager sentinel p95 exceeded SLA", "checks": slow_sentinels})
    if not args.allow_short_duration and not resource_evidence["server_pids"]:
        findings.append({"severity": "high", "title": "server PID RSS evidence was not configured"})
    if resource_evidence["server_pids"] and resource_evidence["monitored_rss_max_mb"] <= 0:
        findings.append({"severity": "high", "title": "configured server PIDs produced no RSS evidence"})
    if int(points_run.get("returncode") or 0) != 0 or points_payload.get("ok") is False:
        findings.append({"severity": "high", "title": "concurrent PointsChain/economy stress failed"})
    failed_browser_runs = [item for item in browser_runs if int(item.get("returncode") or 0) != 0 or not (item.get("result") or {}).get("ok")]
    if failed_browser_runs:
        findings.append({"severity": "high", "title": "browser full-feature rotation failed", "count": len(failed_browser_runs)})
    for name, result in final_checks.items():
        if int(result.get("status") or 0) != 200 or not result.get("ok"):
            findings.append({"severity": "high", "title": f"final control-plane check failed: {name}", "status": result.get("status")})

    payload = {
        "ok": not findings,
        "verdict": "PASS" if not findings else "FAIL",
        "production_signoff_eligible": not args.allow_short_duration and int(args.duration_seconds) >= MIN_SIGNOFF_SECONDS and not findings,
        "base_url": args.base_url,
        "runtime_root": str(runtime_root),
        "server_runtime_root": str(server_runtime_root),
        "started_at": started_at,
        "finished_at": utc_now(),
        "requested_duration_seconds": int(args.duration_seconds),
        "actual_duration_seconds": round(actual_duration, 3),
        "allow_short_duration": bool(args.allow_short_duration),
        "concurrency": int(args.concurrency),
        "accounts": account_names,
        "aggregate": aggregate,
        "resource_evidence": resource_evidence,
        "sentinel": sentinel,
        "points_stress": {"run": points_run, "result": points_payload, "report": str(points_report)},
        "browser_runs": browser_runs,
        "final_checks": final_checks,
        "round_runs": round_runs,
        "source_harness_hashes": source_harness_hashes,
        "harness_drift": detected_harness_drift,
        "checkpoint": str(checkpoint_path),
        "findings": findings,
    }
    atomic_write_json(out_path, payload)
    markdown = write_markdown(out_path, payload)
    payload["markdown_report"] = str(markdown)
    atomic_write_json(out_path, payload)
    atomic_write_json(checkpoint_path, {
        "status": "complete",
        "phase": "complete",
        "production_signoff_eligible": payload["production_signoff_eligible"],
        "verdict": payload["verdict"],
        "started_at": started_at,
        "finished_at": payload["finished_at"],
        "actual_duration_seconds": payload["actual_duration_seconds"],
        "aggregate": aggregate,
        "sentinel": sentinel,
        "resource_evidence": resource_evidence,
        "source_harness_hashes": source_harness_hashes,
        "harness_drift": detected_harness_drift,
        "report": str(out_path),
        "markdown_report": str(markdown),
    })
    print(json.dumps({"ok": payload["ok"], "verdict": payload["verdict"], "report": str(out_path), "findings": findings}, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
