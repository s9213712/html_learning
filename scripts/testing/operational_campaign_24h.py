#!/usr/bin/env python3
"""Run the isolated 24-hour operational, recovery, and usability campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests
import urllib3


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LAUNCHER = ROOT / "test_for_develop.sh"
SOAK = ROOT / "scripts" / "testing" / "operational_soak_probe.py"
MIN_FORMAL_SECONDS = 24 * 60 * 60
SENSITIVE_FLAGS = {
    "--password",
    "--root-password",
    "--manager-password",
    "--test-password",
    "--user-password",
    "--member-password",
    "--account-password",
    "--accounts",
}
PROTECTED_FINANCIAL_DATABASES = ("finance.db", "points_chain.db", "trading.db")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "path": str(path)}


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * pct)))
    return round(ordered[index], 3)


def validate_tmp_path(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    tmp = Path("/tmp").resolve()
    if resolved != tmp and tmp not in resolved.parents:
        raise ValueError(f"{label} must remain under /tmp: {resolved}")
    return resolved


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sanitized_command(command: list[str]) -> list[str]:
    result: list[str] = []
    hide_next = False
    for value in command:
        text = str(value)
        if hide_next:
            result.append("[redacted]")
            hide_next = False
            continue
        matched = next((flag for flag in SENSITIVE_FLAGS if text.startswith(f"{flag}=")), "")
        if matched:
            result.append(f"{matched}=[redacted]")
            continue
        result.append(text)
        if text in SENSITIVE_FLAGS:
            hide_next = True
    return result


def git_metadata() -> dict[str, Any]:
    def output(*args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(ROOT), *args],
                text=True,
                capture_output=True,
                timeout=15,
                check=True,
            )
            return completed.stdout.strip()
        except Exception:
            return ""

    status = output("status", "--porcelain", "--untracked-files=all")
    return {
        "target_commit": output("rev-parse", "HEAD"),
        "target_branch": output("rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_dirty": bool(status),
        "worktree_change_count": len(status.splitlines()),
    }


def source_manifest() -> dict[str, str]:
    paths: set[Path] = {Path(__file__).resolve(), LAUNCHER.resolve()}
    for base in (
        ROOT / ".github",
        ROOT / "deploy",
        ROOT / "hooks",
        ROOT / "routes",
        ROOT / "services",
        ROOT / "scripts",
        ROOT / "public",
        ROOT / "tests",
        ROOT / "workflows",
    ):
        if not base.exists():
            continue
        for pattern in ("*.py", "*.sh", "*.js", "*.html", "*.css", "*.json", "*.yml", "*.yaml", "*.sql"):
            paths.update(path.resolve() for path in base.rglob(pattern) if path.is_file())
    for pattern in ("*.py", "*.sh", "*.sql", "requirements*.txt", "pytest.ini"):
        paths.update(path.resolve() for path in ROOT.glob(pattern) if path.is_file())
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
        if path.exists() and path.is_file()
    }


def manifest_digest(manifest: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(manifest.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def manifest_drift(expected: dict[str, str]) -> dict[str, dict[str, str]]:
    current = source_manifest()
    return {
        name: {"expected": expected.get(name, "missing"), "actual": current.get(name, "missing")}
        for name in sorted(set(expected) | set(current))
        if expected.get(name) != current.get(name)
    }


@dataclass(frozen=True)
class Credentials:
    root: str
    manager: str
    test: str
    member: str

    @classmethod
    def load(cls, *, managed_servers: bool) -> "Credentials":
        names = {
            "root": "HACKME_CAMPAIGN_ROOT_PASSWORD",
            "manager": "HACKME_CAMPAIGN_MANAGER_PASSWORD",
            "test": "HACKME_CAMPAIGN_TEST_PASSWORD",
            "member": "HACKME_CAMPAIGN_MEMBER_PASSWORD",
        }
        values = {key: str(os.environ.get(name) or "") for key, name in names.items()}
        if not managed_servers:
            missing = [name for key, name in names.items() if not values[key]]
            if missing:
                raise ValueError("existing targets require credential environment variables: " + ", ".join(missing))
        for key in values:
            values[key] = values[key] or f"Campaign-{key}-{secrets.token_urlsafe(24)}"
        return cls(**values)

    def child_env(self) -> dict[str, str]:
        return {
            "HACKME_PROBE_ROOT_PASSWORD": self.root,
            "HACKME_PROBE_MANAGER_PASSWORD": self.manager,
            "HACKME_PROBE_USER_PASSWORD": self.test,
            "HACKME_ROOT_PASSWORD": self.root,
            "HACKME_MANAGER_PASSWORD": self.manager,
            "HACKME_TEST_PASSWORD": self.test,
            "PLAYWRIGHT_ROOT_PASSWORD": self.root,
            "PLAYWRIGHT_MANAGER_PASSWORD": self.manager,
            "PLAYWRIGHT_TEST_PASSWORD": self.test,
            "PENTEST_ROOT_PASSWORD": self.root,
            "PENTEST_MANAGER_PASSWORD": self.manager,
            "PENTEST_TEST_PASSWORD": self.test,
            "PENTEST_STRESS_USER_PASSWORD": self.member,
            "HACKME_QA_ROOT_PASSWORD": self.root,
            "HACKME_QA_TEST_PASSWORD": self.test,
            "HACKME_TRADING_PROBE_USER_PASSWORD": self.member,
        }


class WebClient:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""

    def refresh_csrf(self) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/api/csrf-token", timeout=self.timeout)
        body = response.json() if response.content else {}
        self.csrf = str(body.get("csrf_token") or self.session.cookies.get("csrf_token") or "")
        return {"ok": response.status_code == 200 and bool(self.csrf), "status": response.status_code}

    def login(self) -> dict[str, Any]:
        self.refresh_csrf()
        return self.request("POST", "/api/login", json_body={"username": self.username, "password": self.password}, retry_login=False)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry_login: bool = True,
    ) -> dict[str, Any]:
        method = method.upper()
        headers: dict[str, str] = {}
        if method not in {"GET", "HEAD", "OPTIONS"}:
            if not self.csrf:
                self.refresh_csrf()
            headers["X-CSRF-Token"] = self.csrf
        started = time.perf_counter()
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            if response.status_code == 401 and retry_login:
                self.login()
                return self.request(method, path, json_body=json_body, params=params, retry_login=False)
            try:
                body: Any = response.json()
            except Exception:
                body = {"raw": response.text[:1000]}
            return {
                "ok": 200 <= response.status_code < 300 and (not isinstance(body, dict) or body.get("ok") is not False),
                "status": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "body": body,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": 0,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
            }


class ServerController:
    def __init__(
        self,
        *,
        name: str,
        run_root: Path,
        port: int,
        credentials: Credentials,
        workers: int,
        threads: int,
        planned_outage: threading.Event,
    ) -> None:
        self.name = name
        self.run_root = validate_tmp_path(run_root, label=f"{name} run root")
        self.runtime_root = self.run_root / "runtime"
        self.port = int(port)
        self.base_url = f"https://127.0.0.1:{self.port}"
        self.credentials = credentials
        self.workers = max(1, int(workers))
        self.threads = max(1, int(threads))
        self.planned_outage = planned_outage
        self.launch_count = 0
        self.events: list[dict[str, Any]] = []

    @property
    def pid_file(self) -> Path:
        return self.runtime_root / "server.pid"

    def pid(self) -> int:
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "ROOT_PASSWORD": self.credentials.root,
            "MANAGER_PASSWORD": self.credentials.manager,
            "TEST_PASSWORD": self.credentials.test,
            "PYTHONPYCACHEPREFIX": str(self.run_root / "pycache"),
            "HACKME_MEDIA_REALTIME_PROXY_ENABLED": "1",
            "HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT": "2",
            "HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE": "host",
            "HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR": str(self.run_root / "locks" / "realtime_proxy"),
            "HACKME_DEV_BACKTEST_PROBE_ON_STARTUP": "0",
            "HACKME_DEV_BTC_TRADE_AUTOSTART": "0",
        })
        return env

    def launcher_command(self) -> list[str]:
        return [
            str(LAUNCHER),
            "--cli",
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--port-conflict", "fail",
            "--feature-mode", "all",
            "--security", "off",
            "--server-mode", "dev_ready",
            "--server-runner", "gunicorn",
            "--gunicorn-workers", str(self.workers),
            "--gunicorn-threads", str(self.threads),
            "--gunicorn-timeout", "900",
            "--gunicorn-max-requests", "10000",
            "--no-capacity-probe",
            "--no-hls-slot-probe",
            "--no-btc-trade-autostart",
            "--trading-background-dev-ready",
            "--max-content-mb", "4096",
            "--run-root", str(self.run_root),
            "--runtime-root", str(self.runtime_root),
            "--in-place",
            "--tmp-runtime",
            "--skip-install",
        ]

    def wait_ready(self, *, timeout: float = 180.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        attempts = 0
        started = time.monotonic()
        last_error = ""
        while time.monotonic() < deadline:
            attempts += 1
            try:
                response = requests.get(f"{self.base_url}/api/version", verify=False, timeout=5)
                if response.status_code == 200:
                    return {
                        "ok": True,
                        "attempts": attempts,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "status": response.status_code,
                    }
                last_error = f"status={response.status_code}"
            except Exception as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
            time.sleep(0.5)
        return {
            "ok": False,
            "attempts": attempts,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": last_error,
        }

    def start(self) -> dict[str, Any]:
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.launch_count += 1
        log = self.run_root / "campaign" / f"launcher_{self.launch_count:03d}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        command = self.launcher_command()
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
            check=False,
        )
        output = completed.stdout or ""
        log.write_text(output, encoding="utf-8")
        leaked = [label for label, value in (
            ("root", self.credentials.root),
            ("manager", self.credentials.manager),
            ("test", self.credentials.test),
            ("member", self.credentials.member),
        ) if value and value in output]
        ready = self.wait_ready() if completed.returncode == 0 and not leaked else {"ok": False, "error": "launcher_failed_or_secret_leak"}
        event = {
            "action": "start",
            "name": self.name,
            "at": utc_now(),
            "returncode": completed.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "pid": self.pid(),
            "ready": ready,
            "secret_leak_labels": leaked,
            "log": str(log),
            "command": sanitized_command(command),
            "ok": completed.returncode == 0 and not leaked and bool(ready.get("ok")) and self.pid() > 0,
        }
        if event["ok"]:
            self.planned_outage.clear()
        self.events.append(event)
        return event

    def _pid_matches_runtime(self, pid: int) -> bool:
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except Exception:
            return False
        expected = f"HACKME_RUNTIME_DIR={self.runtime_root}".encode()
        return expected in environ

    def stop(self, *, reason: str = "campaign") -> dict[str, Any]:
        self.planned_outage.set()
        pid = self.pid()
        started = time.monotonic()
        event: dict[str, Any] = {"action": "stop", "name": self.name, "at": utc_now(), "pid": pid, "reason": reason}
        try:
            if pid > 0 and Path(f"/proc/{pid}").exists():
                if not self._pid_matches_runtime(pid):
                    raise RuntimeError(f"refusing to stop pid {pid}: runtime ownership mismatch")
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
                    time.sleep(0.2)
                if Path(f"/proc/{pid}").exists():
                    os.killpg(pgid, signal.SIGKILL)
                    time.sleep(0.5)
            event["ok"] = not (pid > 0 and Path(f"/proc/{pid}").exists())
        except Exception as exc:
            event.update({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
        event["elapsed_seconds"] = round(time.monotonic() - started, 3)
        self.events.append(event)
        return event

    def restart(self, *, reason: str) -> dict[str, Any]:
        started = time.monotonic()
        stopped = self.stop(reason=reason)
        start = self.start() if stopped.get("ok") else {"ok": False, "error": "stop_failed"}
        self.planned_outage.clear()
        result = {
            "action": "restart",
            "name": self.name,
            "reason": reason,
            "stopped": stopped,
            "started": start,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "ok": bool(stopped.get("ok") and start.get("ok")),
        }
        self.events.append(result)
        return result


def proc_rows() -> dict[int, dict[str, int]]:
    rows: dict[int, dict[str, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status: dict[str, str] = {}
            for line in (entry / "status").read_text(encoding="utf-8", errors="ignore").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
            stat_tail = (entry / "stat").read_text(encoding="utf-8", errors="ignore").rsplit(") ", 1)[1].split()
            rows[int(entry.name)] = {
                "ppid": int(status.get("PPid", "0").split()[0]),
                "rss_kb": int(status.get("VmRSS", "0 kB").split()[0]),
                "threads": int(status.get("Threads", "0").split()[0]),
                "cpu_ticks": int(stat_tail[11]) + int(stat_tail[12]),
            }
        except Exception:
            continue
    return rows


def descendants(rows: dict[int, dict[str, int]], root_pid: int) -> set[int]:
    found = {root_pid} if root_pid in rows else set()
    changed = True
    while changed:
        changed = False
        for pid, row in rows.items():
            if pid not in found and row["ppid"] in found:
                found.add(pid)
                changed = True
    return found


class ResourceMonitor(threading.Thread):
    def __init__(self, controllers: list[ServerController], out: Path, *, interval: float):
        super().__init__(daemon=True)
        self.controllers = controllers
        self.out = out
        self.interval = max(1.0, float(interval))
        self.stop_event = threading.Event()
        self.samples: list[dict[str, Any]] = []

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        session = requests.Session()
        session.verify = False
        with self.out.open("a", encoding="utf-8") as handle:
            while not self.stop_event.is_set():
                processes = proc_rows()
                sample: dict[str, Any] = {"at": utc_now(), "monotonic": round(time.monotonic(), 3), "servers": {}}
                try:
                    load = Path("/proc/loadavg").read_text(encoding="utf-8").split()
                    mem = {}
                    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                        if ":" in line:
                            key, value = line.split(":", 1)
                            mem[key] = int(value.strip().split()[0])
                    sample["host"] = {
                        "load1": float(load[0]),
                        "load5": float(load[1]),
                        "load15": float(load[2]),
                        "mem_available_kb": int(mem.get("MemAvailable", 0)),
                        "mem_total_kb": int(mem.get("MemTotal", 0)),
                    }
                except Exception as exc:
                    sample["host"] = {"error": str(exc)}
                for controller in self.controllers:
                    pid = controller.pid()
                    tree = descendants(processes, pid)
                    started = time.perf_counter()
                    try:
                        response = session.get(f"{controller.base_url}/api/version", timeout=5)
                        health_status = response.status_code
                        health_error = ""
                    except Exception as exc:
                        health_status = 0
                        health_error = f"{exc.__class__.__name__}: {exc}"
                    database_dir = controller.runtime_root / "database"
                    db_sizes = {
                        path.name: path.stat().st_size
                        for path in database_dir.glob("*.db*")
                        if path.is_file()
                    } if database_dir.exists() else {}
                    sample["servers"][controller.name] = {
                        "pid": pid,
                        "process_count": len(tree),
                        "rss_kb": sum(processes[item]["rss_kb"] for item in tree),
                        "threads": sum(processes[item]["threads"] for item in tree),
                        "cpu_ticks": sum(processes[item]["cpu_ticks"] for item in tree),
                        "health_status": health_status,
                        "health_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                        "health_error": health_error,
                        "planned_outage": controller.planned_outage.is_set(),
                        "database_sizes": db_sizes,
                    }
                self.samples.append(sample)
                handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                self.stop_event.wait(self.interval)

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {"samples": len(self.samples), "servers": {}}
        for controller in self.controllers:
            rows = [(sample.get("servers") or {}).get(controller.name) or {} for sample in self.samples]
            latencies = [float(row.get("health_elapsed_ms") or 0) for row in rows if int(row.get("health_status") or 0) == 200]
            unplanned = [row for row in rows if int(row.get("health_status") or 0) != 200 and not row.get("planned_outage")]
            result["servers"][controller.name] = {
                "samples": len(rows),
                "health_200": sum(1 for row in rows if int(row.get("health_status") or 0) == 200),
                "unplanned_health_failures": len(unplanned),
                "unplanned_failure_samples": unplanned[:20],
                "health_latency_ms": {
                    "p50": percentile(latencies, 0.50),
                    "p95": percentile(latencies, 0.95),
                    "p99": percentile(latencies, 0.99),
                    "max": round(max(latencies), 3) if latencies else 0.0,
                },
                "max_rss_mb": round(max((int(row.get("rss_kb") or 0) for row in rows), default=0) / 1024, 3),
                "max_threads": max((int(row.get("threads") or 0) for row in rows), default=0),
                "max_processes": max((int(row.get("process_count") or 0) for row in rows), default=0),
            }
        available = [int((sample.get("host") or {}).get("mem_available_kb") or 0) for sample in self.samples]
        result["host"] = {
            "minimum_mem_available_mb": round(min((value for value in available if value > 0), default=0) / 1024, 3),
            "maximum_load1": max((float((sample.get("host") or {}).get("load1") or 0) for sample in self.samples), default=0.0),
        }
        return result


def terminate_process_group(process: subprocess.Popen[Any], *, grace_seconds: float = 10.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.1, grace_seconds)
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    category: str
    target: str
    fraction: float
    runner: Callable[[], dict[str, Any]]
    mandatory: bool = True


class Campaign:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = validate_tmp_path(Path(args.campaign_root), label="campaign root")
        self.reports = self.root / "reports"
        self.checkpoint_path = self.root / "campaign.checkpoint.json"
        self.final_path = self.reports / "operational_campaign_24h.json"
        self.credentials = Credentials.load(managed_servers=True)
        self.primary_outage = threading.Event()
        self.recovery_outage = threading.Event()
        primary_port = int(args.primary_port or free_port())
        recovery_port = int(args.recovery_port or free_port())
        if primary_port == recovery_port:
            recovery_port = free_port()
        self.primary = ServerController(
            name="primary",
            run_root=self.root / "primary",
            port=primary_port,
            credentials=self.credentials,
            workers=args.workers,
            threads=args.threads,
            planned_outage=self.primary_outage,
        )
        self.recovery = ServerController(
            name="recovery",
            run_root=self.root / "recovery",
            port=recovery_port,
            credentials=self.credentials,
            workers=max(2, args.workers // 2),
            threads=args.threads,
            planned_outage=self.recovery_outage,
        )
        self.lock = threading.RLock()
        self.active_event = threading.Event()
        self.stop_event = threading.Event()
        self.active_started = 0.0
        self.active_started_at = ""
        self.scenario_results: dict[str, dict[str, Any]] = {}
        self.scenario_threads: list[threading.Thread] = []
        self.accounts: list[tuple[str, str]] = []
        self.source_hashes = source_manifest()
        self.source_digest = manifest_digest(self.source_hashes)
        self.source_git = git_metadata()
        self.drift: dict[str, dict[str, str]] = {}
        self.core_process: subprocess.Popen[Any] | None = None
        self.core_stdout_handle: Any = None
        self.core_command: list[str] = []
        self.core_root = self.root / "core_soak"
        self.core_report = self.core_root / "operational_soak.json"
        self.resource_monitor = ResourceMonitor(
            [self.primary, self.recovery],
            self.reports / "resources" / "resource_samples.jsonl",
            interval=args.resource_interval,
        )

    def base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.credentials.child_env())
        env.update({
            "PYTHONPATH": str(ROOT),
            "PYTHONPYCACHEPREFIX": str(self.root / "pycache"),
            "HACKME_TEST_ARTIFACT_ROOT": str(self.root / "test_artifacts"),
        })
        return env

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.active_started) if self.active_started else 0.0

    def required_duration_completed(self) -> bool:
        return self.elapsed() + 1 >= int(self.args.duration_seconds)

    def check_drift(self) -> dict[str, dict[str, str]]:
        drift = manifest_drift(self.source_hashes)
        if drift:
            with self.lock:
                self.drift.update(drift)
        return drift

    def write_checkpoint(self, phase: str) -> None:
        with self.lock:
            payload = {
                "status": "running",
                "phase": phase,
                "updated_at": utc_now(),
                "active_started_at": self.active_started_at,
                "active_test_seconds": round(self.elapsed(), 3),
                "required_active_test_seconds": int(self.args.duration_seconds),
                "primary": {
                    "base_url": self.primary.base_url,
                    "runtime_root": str(self.primary.runtime_root),
                    "pid": self.primary.pid(),
                },
                "recovery": {
                    "base_url": self.recovery.base_url,
                    "runtime_root": str(self.recovery.runtime_root),
                    "pid": self.recovery.pid(),
                    "planned_outage": self.recovery_outage.is_set(),
                },
                "accounts": [username for username, _password in self.accounts],
                "scenario_results": self.scenario_results,
                "source_manifest_digest": self.source_digest,
                "source_git": self.source_git,
                "source_drift": self.drift,
                "core_soak": {
                    "pid": self.core_process.pid if self.core_process else 0,
                    "returncode": self.core_process.poll() if self.core_process else None,
                    "report": str(self.core_report),
                },
                "report": str(self.final_path),
            }
            atomic_write_json(self.checkpoint_path, payload)

    def run_step(
        self,
        scenario_id: str,
        step_id: str,
        command: list[str],
        *,
        timeout: int,
        artifact: Path | None = None,
        env: dict[str, str] | None = None,
        cwd: Path = ROOT,
        payload_ok: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        out_dir = self.reports / "scenarios" / scenario_id
        out_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = out_dir / f"{step_id}.stdout"
        started_at = utc_now()
        started = time.monotonic()
        full_env = self.base_env()
        full_env.update(env or {})
        with stdout_path.open("w", encoding="utf-8") as stdout:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=full_env,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            timed_out = False
            try:
                returncode = process.wait(timeout=max(1, int(timeout)))
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_group(process)
                returncode = 124
                stdout.write(f"\n[TIMEOUT] {step_id} exceeded {timeout}s\n")
        output = stdout_path.read_text(encoding="utf-8", errors="replace")
        leaked = [label for label, value in (
            ("root", self.credentials.root),
            ("manager", self.credentials.manager),
            ("test", self.credentials.test),
            ("member", self.credentials.member),
        ) if value and value in output]
        payload = load_json(artifact) if artifact and artifact.exists() else {}
        artifact_ok = True
        if artifact is not None:
            artifact_ok = artifact.exists() and (payload_ok(payload) if payload_ok else payload.get("ok") is not False)
        return {
            "step_id": step_id,
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "returncode": returncode,
            "timed_out": timed_out,
            "command": sanitized_command(command),
            "stdout": str(stdout_path),
            "artifact": str(artifact) if artifact else "",
            "artifact_summary": {
                "ok": payload.get("ok"),
                "verdict": payload.get("verdict"),
                "error": payload.get("error") or payload.get("msg") or "",
            } if payload else {},
            "secret_leak_labels": leaked,
            "ok": returncode == 0 and not leaked and artifact_ok,
        }

    def run_group(self, scenario_id: str, steps: list[Callable[[], dict[str, Any]]]) -> dict[str, Any]:
        started_at = utc_now()
        started = time.monotonic()
        results: list[dict[str, Any]] = []
        for step in steps:
            if self.stop_event.is_set():
                results.append({"ok": False, "error": "campaign_stopping"})
                break
            results.append(step())
        return {
            "ok": bool(results) and all(item.get("ok") for item in results),
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "steps": results,
        }

    def scenario_media_long(self) -> dict[str, Any]:
        scenario_id = "media_long_hls_share"
        out = self.reports / "scenarios" / scenario_id / "video_hls_quality_stress.json"
        fixture = self.root / "fixtures" / "campaign_long_video.mkv"
        fixture_seconds = 45 if self.args.allow_short_duration else 3900
        minimum_duration = 30 if self.args.allow_short_duration else 3600
        minimum_segments = 3 if self.args.allow_short_duration else 100
        account_rows = [{"username": username, "password": password} for username, password in self.accounts[:3]]
        command = [
            sys.executable,
            str(ROOT / "scripts" / "testing" / "video_hls_quality_stress.py"),
            "--base-url", self.primary.base_url,
            "--video", str(fixture),
            "--db", str(self.primary.runtime_root / "database" / "database.db"),
            "--runtime-marker", str(self.primary.run_root),
            "--out", str(out),
            "--generate-fixture-duration-seconds", str(fixture_seconds),
            "--fixture-timeout-seconds", "1800",
            "--minimum-source-duration-seconds", str(minimum_duration),
            "--visibility", "unlisted",
            "--privacy-mode", "server_encrypted",
            "--upload",
            "--wait",
            "--measure",
            "--verify-share",
            "--browser-seek",
            "--browser-mobile",
            "--expect-audio-tracks", "2",
            "--expect-subtitles",
            "--minimum-segments-per-variant", str(minimum_segments),
            "--segment-concurrency", "8",
            "--max-segments-per-variant", "16",
            "--post-upload-observe-seconds", "5",
            "--upload-timeout-seconds", "1800",
            "--wait-timeout-seconds", "21600",
            "--wait-interval-seconds", "15",
            "--orphan-grace-seconds", "900",
        ]
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "long_video_hls_share",
                command,
                timeout=8 * 60 * 60,
                artifact=out,
                env={
                    "HACKME_HLS_STRESS_ACCOUNTS_JSON": json.dumps(account_rows),
                    "HACKME_HLS_SHARE_PASSWORD": secrets.token_urlsafe(24),
                    "HACKME_PROBE_ROOT_PASSWORD": self.credentials.root,
                },
            )
        ])

    def scenario_ai_agent(self) -> dict[str, Any]:
        scenario_id = "ai_agent_operations"
        scripts = [
            ("frontend_full", "ai_agent_frontend_full_probe.py", []),
            ("drive_share", "ai_agent_drive_share_task_probe.py", []),
            ("server_ops", "ai_agent_server_ops_probe.py", []),
            ("governance", "ai_agent_governance_capability_probe.py", []),
            ("trading", "ai_agent_trading_capability_probe.py", []),
            ("media", "ai_agent_media_downloader_probe.py", ["--fixture", str(self.root / "fixtures" / "ai_agent_media_probe.mp4")]),
            ("capability_boundary", "ai_agent_capability_boundary_probe.py", ["--comfyui-api-url", "http://127.0.0.1:1"]),
        ]
        comfyui_url = str(os.environ.get("HACKME_CAMPAIGN_COMFYUI_API_URL") or "").strip()
        if comfyui_url:
            scripts.append(("real_image_generation", "ai_agent_image_generation_probe.py", ["--comfyui-api-url", comfyui_url]))
        steps: list[Callable[[], dict[str, Any]]] = []
        for step_id, script_name, extra in scripts:
            artifact = self.reports / "scenarios" / scenario_id / f"{step_id}.json"
            command = [
                sys.executable,
                str(ROOT / "scripts" / "testing" / script_name),
                "--base-url", self.primary.base_url,
                "--out", str(artifact),
                *extra,
            ]
            steps.append(lambda step_id=step_id, command=command, artifact=artifact: self.run_step(
                scenario_id,
                step_id,
                command,
                timeout=1800,
                artifact=artifact,
            ))
        return self.run_group(scenario_id, steps)

    def scenario_trading(self) -> dict[str, Any]:
        scenario_id = "trading_background_and_abuse"
        background_dir = self.reports / "scenarios" / scenario_id / "background"
        pentest_dir = self.reports / "scenarios" / scenario_id / "pentest"
        stress_orders = 20 if self.args.allow_short_duration else 150
        users = 3 if self.args.allow_short_duration else 16
        orders = 10 if self.args.allow_short_duration else 180
        background_artifact = background_dir / "trading_background_correctness.json"
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "background_correctness",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "playwright_trading_background_correctness.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-dir", str(self.primary.runtime_root),
                    "--out", str(background_dir),
                    "--trigger-mode", "auto",
                    "--stress-orders", str(stress_orders),
                ],
                timeout=3600,
                artifact=background_artifact,
            ),
            lambda: self.run_step(
                scenario_id,
                "trading_stress_pentest",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "security" / "pentest" / "trading_stress_pentest.py"),
                    "--base-url", self.primary.base_url,
                    "--users", str(users),
                    "--orders-per-user", str(orders),
                    "--concurrency", "16",
                    "--rate", "60",
                    "--out", str(pentest_dir),
                ],
                timeout=3600,
            ),
        ])

    def scenario_points_hft(self) -> dict[str, Any]:
        scenario_id = "pointschain_hft_invariants"
        out_dir = self.reports / "scenarios" / scenario_id
        stress = out_dir / "points_chain_destructive_stress.json"
        post = out_dir / "points_chain_post_stress.json"
        direct_ops = 200 if self.args.allow_short_duration else 12000
        transfer_ops = 20 if self.args.allow_short_duration else 1200
        trading_ops = 10 if self.args.allow_short_duration else 600
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "high_frequency_chain_and_trading",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "points_chain_destructive_stress.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-root", str(self.primary.runtime_root),
                    "--out", str(stress),
                    "--accounts", "24",
                    "--grant-points", "20000",
                    "--transfer-ops", str(transfer_ops),
                    "--direct-transfer-ops", str(direct_ops),
                    "--trading-ops", str(trading_ops),
                    "--concurrency", "32",
                    "--external-transfer-every", "7",
                    "--max-external-transfers", "40",
                    "--server-pids", str(self.primary.pid()),
                ],
                timeout=4 * 60 * 60,
                artifact=stress,
                env={"HACKME_POINTS_STRESS_ROOT_PASSWORD": self.credentials.root},
            ),
            lambda: self.run_step(
                scenario_id,
                "post_stress_frontend",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "points_chain_post_stress_playwright.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(post),
                    "--member-username", "admin",
                ],
                timeout=1200,
                artifact=post,
            ),
        ])

    def scenario_points_incident(self) -> dict[str, Any]:
        scenario_id = "pointschain_incident_governance"
        out_dir = self.reports / "scenarios" / scenario_id
        attacks = out_dir / "real_incident_attacks.json"
        dispute = out_dir / "dispute_api.json"
        frontend = out_dir / "incident_frontend.json"
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "real_incident_attack_regressions",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_real_incident_attack_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(attacks),
                ],
                timeout=5400,
                artifact=attacks,
            ),
            lambda: self.run_step(
                scenario_id,
                "live_dispute_api",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_dispute_api_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-root", str(self.primary.runtime_root),
                    "--out", str(dispute),
                ],
                timeout=1800,
                artifact=dispute,
            ),
            lambda: self.run_step(
                scenario_id,
                "incident_frontend",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_real_incident_frontend_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(frontend),
                ],
                timeout=1800,
                artifact=frontend,
            ),
        ])

    def scenario_media_compatibility(self) -> dict[str, Any]:
        scenario_id = "media_proxy_cross_browser"
        out_dir = self.reports / "scenarios" / scenario_id
        service_out = out_dir / "realtime_proxy_service.json"
        http_root = out_dir / "http_concurrency"
        browser_root = out_dir / "browser_compat"
        browser_artifact = browser_root / "reports" / "qa" / "browser_video_compat.json"
        chat_out = out_dir / "chat_video_share.json"
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "realtime_proxy_service",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "realtime_proxy_stress_probe.py"),
                    "--runtime-root", str(out_dir / "service_runtime"),
                    "--json-out", str(service_out),
                    "--duration", "12" if self.args.allow_short_duration else "90",
                    "--max-concurrent", "2",
                ],
                timeout=1200,
                artifact=service_out,
            ),
            lambda: self.run_step(
                scenario_id,
                "realtime_proxy_http_concurrency",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "realtime_proxy_http_concurrency_probe.py"),
                    "--runtime-root", str(http_root),
                    "--json-out", str(http_root / "result.json"),
                    "--duration", "8" if self.args.allow_short_duration else "60",
                    "--max-concurrent", "2",
                    "--server-runner", "gunicorn",
                    "--gunicorn-workers", "3",
                    "--gunicorn-threads", "2",
                ],
                timeout=1800,
                artifact=http_root / "result.json",
            ),
            lambda: self.run_step(
                scenario_id,
                "cross_browser_video",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "playwright_browser_video_compat.py"),
                    "--runtime-root", str(browser_root),
                    "--browsers", "chromium" if self.args.allow_short_duration else "chromium,firefox,webkit",
                ],
                timeout=3600,
                artifact=browser_artifact,
            ),
            lambda: self.run_step(
                scenario_id,
                "chat_video_share_embed",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "chat_video_share_link_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(chat_out),
                ],
                timeout=1200,
                artifact=chat_out,
            ),
        ])

    def scenario_final_ui_prelaunch(self) -> dict[str, Any]:
        scenario_id = "final_ui_mobile_prelaunch"
        out_dir = self.reports / "scenarios" / scenario_id
        deep_root = out_dir / "deep_site"
        member = out_dir / "member_probe.json"
        gate = out_dir / "production_gate"
        return self.run_group(scenario_id, [
            lambda: self.run_step(
                scenario_id,
                "member_real_behavior",
                [
                    sys.executable,
                    str(ROOT / "docs" / "AGENTS" / "skills" / "hackme-web-qa" / "scripts" / "member_probe.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(member),
                ],
                timeout=1800,
                artifact=member,
            ),
            lambda: self.run_step(
                scenario_id,
                "desktop_mobile_deep_site",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "playwright_deep_site_check.py"),
                    "--base-url", self.primary.base_url,
                    "--runtime-root", str(deep_root),
                    "--max-chess-human-moves", "8",
                ],
                timeout=5400,
            ),
            lambda: self.run_step(
                scenario_id,
                "whole_site_production_gate",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "security" / "gate" / "whole_site_production_gate.py"),
                    "--base-url", self.primary.base_url,
                    "--out", str(gate),
                    "--skip-full-pytest",
                    "--stress-requests", "80" if self.args.allow_short_duration else "400",
                    "--stress-concurrency", "16",
                ],
                timeout=3 * 60 * 60,
            ),
        ])

    def _create_user(self, root: WebClient, username: str, password: str, *, nickname: str = "Campaign User") -> dict[str, Any]:
        result = root.request(
            "POST",
            "/api/admin/users",
            json_body={
                "username": username,
                "password": password,
                "password_confirm": password,
                "nickname": nickname,
                "role": "user",
                "status": "active",
                "member_level": "normal",
            },
        )
        search = root.request("GET", "/api/admin/users", params={"q": username, "page_size": 100})
        users = ((search.get("body") or {}).get("users") or []) if isinstance(search.get("body"), dict) else []
        exact = next((item for item in users if str(item.get("username") or "") == username), None)
        return {
            "ok": int(result.get("status") or 0) in {200, 201, 409} and exact is not None,
            "create_status": result.get("status"),
            "search_status": search.get("status"),
            "user_id": int((exact or {}).get("id") or 0),
            "username": username,
        }

    def _user_exists(self, root: WebClient, username: str) -> bool:
        search = root.request("GET", "/api/admin/users", params={"q": username, "page_size": 100})
        users = ((search.get("body") or {}).get("users") or []) if isinstance(search.get("body"), dict) else []
        return any(str(item.get("username") or "") == username for item in users)

    def _wallet_transfer_between_builtin_users(self, base_url: str, *, reference: str) -> dict[str, Any]:
        sender = WebClient(base_url, "test", self.credentials.test, timeout=60)
        recipient = WebClient(base_url, "admin", self.credentials.manager, timeout=60)
        sender_login = sender.login()
        recipient_login = recipient.login()
        sender_wallet = sender.request("GET", "/api/points/wallet", params={"hydrate": "1"})
        recipient_wallet = recipient.request("GET", "/api/points/wallet", params={"hydrate": "1"})
        sender_row = (sender_wallet.get("body") or {}).get("wallet") or {}
        recipient_row = (recipient_wallet.get("body") or {}).get("wallet") or {}
        source = str(sender_row.get("active_wallet_address") or "")
        destination = str(recipient_row.get("active_wallet_address") or "")
        transfer = sender.request(
            "POST",
            "/api/points/transactions/submit",
            json_body={
                "source_wallet_address": source,
                "destination_wallet_address": destination,
                "amount_points": 1,
                "fee_points": 0,
                "request_uuid": reference,
                "memo": "24h campaign restore-boundary transfer",
                "compact": True,
            },
        ) if source and destination else {"ok": False, "status": 0, "error": "wallet_address_missing"}
        body = transfer.get("body") or {}
        tx_hash = str(body.get("transaction_hash") or body.get("tx_group_hash") or "")
        return {
            "ok": bool(sender_login.get("ok") and recipient_login.get("ok") and transfer.get("ok") and tx_hash),
            "sender_login": sender_login.get("status"),
            "recipient_login": recipient_login.get("status"),
            "sender_balance_before": sender_row.get("points_balance"),
            "source_wallet_present": bool(source),
            "destination_wallet_present": bool(destination),
            "transfer_status": transfer.get("status"),
            "transaction_hash": tx_hash,
        }

    def _snapshot_restore_boundary_cycle(self) -> dict[str, Any]:
        started = time.monotonic()
        root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=180)
        login = root.login()
        if not login.get("ok"):
            return {"ok": False, "error": "root_login_failed", "login": login.get("status")}
        storage_marker = self.recovery.runtime_root / "storage" / "campaign_snapshot_marker.txt"
        storage_marker.parent.mkdir(parents=True, exist_ok=True)
        storage_marker.write_text("snapshot-baseline\n", encoding="utf-8")
        # Hydrate stable built-in wallets before the snapshot so the later
        # append-only transfer never references a user absent from restored core state.
        pre_transfer_wallet = self._wallet_transfer_between_builtin_users(
            self.recovery.base_url,
            reference=f"campaign-pre-snapshot-{int(time.time())}",
        )
        snapshot = root.request(
            "POST",
            "/api/admin/snapshots",
            json_body={"type": "manual", "notes": "24h campaign ordinary-state restore boundary"},
        )
        snapshot_id = str((snapshot.get("body") or {}).get("snapshot_id") or "")
        marker_username = f"restore_dirty_{int(time.time())}"
        marker = self._create_user(root, marker_username, self.credentials.member, nickname="Restore Dirty Marker")
        storage_marker.write_text("snapshot-dirty-after-create\n", encoding="utf-8")
        transfer = self._wallet_transfer_between_builtin_users(
            self.recovery.base_url,
            reference=f"campaign-post-snapshot-{int(time.time())}",
        )
        restore = root.request(
            "POST",
            f"/api/admin/snapshots/{quote(snapshot_id)}/restore",
            json_body={"confirm": "RESTORE", "reason": "24h campaign restore-boundary verification"},
        ) if snapshot_id else {"ok": False, "status": 0, "error": "snapshot_id_missing"}
        root.login()
        marker_absent = not self._user_exists(root, marker_username)
        tx_hash = str(transfer.get("transaction_hash") or "")
        explorer = root.request("GET", f"/api/points/explorer/tx/{quote(tx_hash, safe='')}") if tx_hash else {"ok": False, "status": 0}
        verify = root.request("GET", "/api/root/points/chain/verify")
        restore_body = restore.get("body") or {}
        skipped = {
            str(item.get("label") or ""): str(item.get("reason") or "")
            for item in ((restore_body.get("database_restore") or {}).get("skipped") or [])
        }
        protected_skips = {
            label: skipped.get(label)
            for label in ("finance", "points_chain", "trading")
            if label in skipped
        }
        storage_restored = storage_marker.exists() and storage_marker.read_text(encoding="utf-8") == "snapshot-baseline\n"
        ok = bool(
            pre_transfer_wallet.get("ok")
            and snapshot.get("ok")
            and snapshot_id
            and marker.get("ok")
            and transfer.get("ok")
            and restore.get("ok")
            and marker_absent
            and explorer.get("ok")
            and storage_restored
            and protected_skips.get("finance") == "append_only_financial_restore_disabled"
            and int(verify.get("status") or 0) in {200, 202}
        )
        return {
            "ok": ok,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "snapshot_status": snapshot.get("status"),
            "snapshot_id_present": bool(snapshot_id),
            "dirty_marker_created": marker.get("ok"),
            "dirty_marker_absent_after_restore": marker_absent,
            "append_only_transfer": transfer,
            "transfer_survived_restore": explorer.get("ok"),
            "restore_status": restore.get("status"),
            "protected_database_skips": protected_skips,
            "storage_restored": storage_restored,
            "chain_verify_status": verify.get("status"),
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""

    @staticmethod
    def _sqlite_checks(database_dir: Path) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for path in sorted(database_dir.glob("*.db")):
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
                row = conn.execute("PRAGMA quick_check").fetchone()
                conn.close()
                results[path.name] = {"ok": bool(row and row[0] == "ok"), "result": row[0] if row else "missing"}
            except Exception as exc:
                results[path.name] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
        return results

    def _cli_backup_restore_cycle(self, scenario_id: str) -> dict[str, Any]:
        started = time.monotonic()
        archive = self.root / "backups" / "recovery_runtime.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            archive.unlink()
        stop_before_backup = self.recovery.stop(reason="campaign_cli_backup")
        backup_step = self.run_step(
            scenario_id,
            "cli_runtime_backup",
            [
                str(LAUNCHER),
                "--cli",
                "--run-root", str(self.recovery.run_root),
                "--runtime-root", str(self.recovery.runtime_root),
                "--in-place",
                "--tmp-runtime",
                "--skip-install",
                "--backup", str(archive),
            ],
            timeout=1800,
        ) if stop_before_backup.get("ok") else {"ok": False, "error": "stop_before_backup_failed"}
        backup_size = archive.stat().st_size if archive.exists() else 0
        start_after_backup = self.recovery.start() if backup_step.get("ok") else {"ok": False, "error": "backup_failed"}
        root = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
        root_login = root.login() if start_after_backup.get("ok") else {"ok": False}
        marker_username = f"cli_restore_dirty_{int(time.time())}"
        marker = self._create_user(root, marker_username, self.credentials.member, nickname="CLI Restore Dirty") if root_login.get("ok") else {"ok": False}
        transfer = self._wallet_transfer_between_builtin_users(
            self.recovery.base_url,
            reference=f"campaign-cli-post-backup-{int(time.time())}",
        ) if root_login.get("ok") else {"ok": False}
        storage_marker = self.recovery.runtime_root / "storage" / "campaign_cli_storage_marker.txt"
        storage_marker.parent.mkdir(parents=True, exist_ok=True)
        storage_marker.write_text("live-storage-after-backup\n", encoding="utf-8")
        stop_before_restore = self.recovery.stop(reason="campaign_cli_restore")
        finance = self.recovery.runtime_root / "database" / "finance.db"
        protected_hash_before = self._sha256(finance)
        restore_step = self.run_step(
            scenario_id,
            "cli_runtime_restore",
            [
                str(LAUNCHER),
                "--cli",
                "--run-root", str(self.recovery.run_root),
                "--runtime-root", str(self.recovery.runtime_root),
                "--in-place",
                "--tmp-runtime",
                "--skip-install",
                "--restore", str(archive),
            ],
            timeout=1800,
        ) if stop_before_restore.get("ok") else {"ok": False, "error": "stop_before_restore_failed"}
        protected_hash_after = self._sha256(finance)
        storage_preserved = storage_marker.exists() and storage_marker.read_text(encoding="utf-8") == "live-storage-after-backup\n"
        policy_path = self.recovery.runtime_root / "logs" / "runtime_restore_policy.json"
        policy = load_json(policy_path) if policy_path.exists() else {}
        sqlite_checks = self._sqlite_checks(self.recovery.runtime_root / "database")
        start_after_restore = self.recovery.start() if restore_step.get("ok") else {"ok": False, "error": "restore_failed"}
        root_after = WebClient(self.recovery.base_url, "root", self.credentials.root, timeout=120)
        root_after_login = root_after.login() if start_after_restore.get("ok") else {"ok": False}
        marker_absent = not self._user_exists(root_after, marker_username) if root_after_login.get("ok") else False
        tx_hash = str(transfer.get("transaction_hash") or "")
        explorer = root_after.request("GET", f"/api/points/explorer/tx/{quote(tx_hash, safe='')}") if tx_hash and root_after_login.get("ok") else {"ok": False}
        verify = root_after.request("GET", "/api/root/points/chain/verify") if root_after_login.get("ok") else {"ok": False, "status": 0}
        ok = bool(
            stop_before_backup.get("ok")
            and backup_step.get("ok")
            and backup_size > 0
            and start_after_backup.get("ok")
            and marker.get("ok")
            and transfer.get("ok")
            and stop_before_restore.get("ok")
            and restore_step.get("ok")
            and protected_hash_before
            and protected_hash_before == protected_hash_after
            and storage_preserved
            and policy.get("policy") == "append_only_financial_restore_disabled"
            and all(item.get("ok") for item in sqlite_checks.values())
            and start_after_restore.get("ok")
            and marker_absent
            and explorer.get("ok")
            and int(verify.get("status") or 0) in {200, 202}
        )
        return {
            "ok": ok,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stop_before_backup": stop_before_backup,
            "backup": backup_step,
            "backup_size_bytes": backup_size,
            "start_after_backup": start_after_backup,
            "dirty_marker_created": marker.get("ok"),
            "append_only_transfer": transfer,
            "stop_before_restore": stop_before_restore,
            "restore": restore_step,
            "protected_finance_hash_preserved": protected_hash_before == protected_hash_after and bool(protected_hash_before),
            "storage_preserved": storage_preserved,
            "restore_policy": policy,
            "sqlite_quick_checks": sqlite_checks,
            "start_after_restore": start_after_restore,
            "dirty_marker_absent_after_restore": marker_absent,
            "transfer_survived_restore": explorer.get("ok"),
            "chain_verify_status": verify.get("status"),
        }

    def scenario_recovery_backup(self) -> dict[str, Any]:
        scenario_id = "recovery_backup_restart"
        out_dir = self.reports / "scenarios" / scenario_id
        realistic = out_dir / "realistic_wallet_incident.json"
        branch = out_dir / "governed_recovery_branch.json"
        restore_drill = out_dir / "rc1_restore_drill.json"

        def realistic_step() -> dict[str, Any]:
            stop = self.recovery.stop(reason="realistic_wallet_incident")
            if not stop.get("ok"):
                return {"ok": False, "stop": stop}
            probe = self.run_step(
                scenario_id,
                "realistic_wallet_incident",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_realistic_recovery_drill.py"),
                    "--runtime-root", str(self.recovery.runtime_root),
                    "--out", str(realistic),
                    "--mode", "dev_ready",
                ],
                timeout=3600,
                artifact=realistic,
            )
            start = self.recovery.start() if probe.get("ok") else {"ok": False, "error": "incident_probe_failed"}
            return {"ok": bool(stop.get("ok") and probe.get("ok") and start.get("ok")), "stop": stop, "probe": probe, "start": start}

        def branch_step() -> dict[str, Any]:
            incident = load_json(realistic)
            tx_hash = str((incident.get("incident") or {}).get("theft_tx_hash") or "")
            victim_wallet = str(((incident.get("users") or {}).get("victim") or {}).get("wallet") or "")
            if not tx_hash or not victim_wallet:
                return {"ok": False, "error": "realistic incident evidence missing"}
            return self.run_step(
                scenario_id,
                "governed_recovery_branch",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "testing" / "pointschain_live_branch_drill.py"),
                    "--base-url", self.recovery.base_url,
                    "--incident-tx-hash", tx_hash,
                    "--victim-wallet", victim_wallet,
                    "--claim-amount", "60",
                    "--out", str(branch),
                ],
                timeout=3600,
                artifact=branch,
            )

        return self.run_group(scenario_id, [
            self._snapshot_restore_boundary_cycle,
            realistic_step,
            branch_step,
            lambda: self.run_step(
                scenario_id,
                "isolated_snapshot_boundary_drill",
                [
                    sys.executable,
                    str(ROOT / "scripts" / "ops" / "rc1_restore_drill.py"),
                    "--out", str(restore_drill),
                ],
                timeout=1800,
                artifact=restore_drill,
            ),
            lambda: self._cli_backup_restore_cycle(scenario_id),
            lambda: self.recovery.restart(reason="final_recovery_restart"),
        ])

    def provision_accounts(self) -> list[tuple[str, str]]:
        root = WebClient(self.primary.base_url, "root", self.credentials.root, timeout=60)
        login = root.login()
        if not login.get("ok"):
            raise RuntimeError(f"primary root login failed: status={login.get('status')}")
        prefix = f"campaign{datetime.now(timezone.utc).strftime('%m%d%H%M')}"
        accounts: list[tuple[str, str]] = []
        for index in range(1, max(4, int(self.args.account_count)) + 1):
            username = f"{prefix}{index:02d}"
            created = self._create_user(root, username, self.credentials.member, nickname=f"Campaign {index:02d}")
            if not created.get("ok"):
                raise RuntimeError(f"campaign account provisioning failed: {created}")
            member = WebClient(self.primary.base_url, username, self.credentials.member, timeout=60)
            if not member.login().get("ok"):
                raise RuntimeError(f"campaign account login failed: {username}")
            accounts.append((username, self.credentials.member))
        self.accounts = accounts
        return accounts

    def preflight(self) -> dict[str, Any]:
        commands = {
            "ffmpeg": ["ffmpeg", "-version"],
            "ffprobe": ["ffprobe", "-version"],
            "playwright": [sys.executable, "-c", "from playwright.sync_api import sync_playwright; print('ok')"],
            "gunicorn": [sys.executable, "-c", "import gunicorn; print(gunicorn.__version__)"],
        }
        dependencies: dict[str, Any] = {}
        for name, command in commands.items():
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(ROOT),
                    env=self.base_env(),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                dependencies[name] = {
                    "ok": completed.returncode == 0,
                    "returncode": completed.returncode,
                    "output": (completed.stdout or completed.stderr or "")[:500],
                }
            except Exception as exc:
                dependencies[name] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
        disk = os.statvfs(self.root.parent)
        free_bytes = int(disk.f_bavail * disk.f_frsize)
        runtime_pollution = [
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("*")
            if path.is_dir() and path.name in {"runtime", "__pycache__", ".pytest_cache"}
        ]
        result = {
            "ok": all(item.get("ok") for item in dependencies.values()) and free_bytes >= int(self.args.minimum_free_gb * 1024**3) and not runtime_pollution,
            "dependencies": dependencies,
            "free_bytes": free_bytes,
            "minimum_free_bytes": int(self.args.minimum_free_gb * 1024**3),
            "repo_runtime_pollution": runtime_pollution,
            "source_manifest_files": len(self.source_hashes),
            "source_manifest_digest": self.source_digest,
            "source_git": self.source_git,
        }
        atomic_write_json(self.reports / "preflight.json", result)
        return result

    def start_core_soak(self) -> dict[str, Any]:
        self.core_root.mkdir(parents=True, exist_ok=True)
        stdout_path = self.core_root / "operational_soak.stdout"
        self.core_command = [
            sys.executable,
            str(SOAK),
            "--base-url", self.primary.base_url,
            "--runtime-root", str(self.core_root),
            "--server-runtime-root", str(self.primary.runtime_root),
            "--out", str(self.core_report),
            "--duration-seconds", str(int(self.args.duration_seconds)),
            "--account-count", str(int(self.args.account_count)),
            "--account-prefix", f"soak{datetime.now(timezone.utc).strftime('%m%d%H%M')}",
            "--round-ops", str(int(self.args.round_ops)),
            "--concurrency", str(int(self.args.concurrency)),
            "--session-pool", str(max(int(self.args.account_count), int(self.args.session_pool))),
            "--round-timeout-seconds", "2400",
            "--sentinel-interval-seconds", "5",
            "--browser-interval-seconds", str(int(self.args.browser_interval_seconds)),
            "--max-server-busy-rate", str(float(self.args.max_server_busy_rate)),
            "--max-ordinary-p95-ms", str(float(self.args.max_ordinary_p95_ms)),
            "--max-ordinary-p99-ms", str(float(self.args.max_ordinary_p99_ms)),
            "--max-sentinel-p95-ms", str(float(self.args.max_sentinel_p95_ms)),
            "--server-pids", str(self.primary.pid()),
        ]
        if self.args.allow_short_duration:
            self.core_command.append("--allow-short-duration")
        env = self.base_env()
        env.update({
            "HACKME_SOAK_ROOT_PASSWORD": self.credentials.root,
            "HACKME_SOAK_MANAGER_PASSWORD": self.credentials.manager,
            "HACKME_SOAK_TEST_PASSWORD": self.credentials.test,
            "HACKME_SOAK_ACCOUNT_PASSWORD": self.credentials.member,
            "HACKME_SERVER_PIDS": str(self.primary.pid()),
        })
        self.core_stdout_handle = stdout_path.open("w", encoding="utf-8")
        self.core_process = subprocess.Popen(
            self.core_command,
            cwd=str(ROOT),
            env=env,
            stdout=self.core_stdout_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        time.sleep(2)
        return {
            "ok": self.core_process.poll() is None,
            "pid": self.core_process.pid,
            "stdout": str(stdout_path),
            "report": str(self.core_report),
            "command": sanitized_command(self.core_command),
        }

    def scenario_specs(self) -> list[ScenarioSpec]:
        return [
            ScenarioSpec("media_long_hls_share", "long_video_upload_stream_hls_share", "primary", 0.01, self.scenario_media_long),
            ScenarioSpec("ai_agent_operations", "ai_agent_full_operations", "primary", 0.08, self.scenario_ai_agent),
            ScenarioSpec("trading_background_and_abuse", "trading_and_background_trading", "primary", 0.20, self.scenario_trading),
            ScenarioSpec("pointschain_hft_invariants", "pointschain_high_frequency_mechanisms", "primary", 0.32, self.scenario_points_hft),
            ScenarioSpec("pointschain_incident_governance", "wallet_incident_and_chain_governance", "primary", 0.43, self.scenario_points_incident),
            ScenarioSpec("recovery_backup_restart", "backup_restore_restart_emergency", "recovery", 0.55, self.scenario_recovery_backup),
            ScenarioSpec("media_proxy_cross_browser", "realtime_proxy_and_cross_browser_media", "isolated", 0.70, self.scenario_media_compatibility),
            ScenarioSpec("final_ui_mobile_prelaunch", "desktop_mobile_prelaunch_and_member_ux", "primary", 0.85, self.scenario_final_ui_prelaunch),
        ]

    def scenario_worker(self, spec: ScenarioSpec) -> None:
        self.active_event.wait()
        delay = max(0.0, float(self.args.duration_seconds) * max(0.0, min(1.0, spec.fraction)))
        while not self.stop_event.is_set() and self.elapsed() < delay:
            self.stop_event.wait(min(5.0, max(0.1, delay - self.elapsed())))
        if self.stop_event.is_set():
            result = {"ok": False, "error": "campaign_stopped_before_scenario", "scheduled_fraction": spec.fraction}
        else:
            drift_before = self.check_drift()
            started_at = utc_now()
            started_elapsed = self.elapsed()
            try:
                result = spec.runner() if not drift_before else {"ok": False, "error": "source_drift_before_scenario"}
            except Exception as exc:
                result = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            drift_after = self.check_drift()
            result.update({
                "scenario_id": spec.scenario_id,
                "category": spec.category,
                "target": spec.target,
                "mandatory": spec.mandatory,
                "scheduled_fraction": spec.fraction,
                "started_at": started_at,
                "started_active_seconds": round(started_elapsed, 3),
                "finished_active_seconds": round(self.elapsed(), 3),
                "source_drift_before": drift_before,
                "source_drift_after": drift_after,
                "ok": bool(result.get("ok") and not drift_before and not drift_after),
            })
        with self.lock:
            self.scenario_results[spec.scenario_id] = result
        self.write_checkpoint(f"scenario_{spec.scenario_id}_complete")

    def start_scenarios(self) -> None:
        for spec in self.scenario_specs():
            thread = threading.Thread(target=self.scenario_worker, args=(spec,), daemon=True, name=f"campaign-{spec.scenario_id}")
            self.scenario_threads.append(thread)
            thread.start()

    @staticmethod
    def scan_server_logs(controllers: list[ServerController]) -> dict[str, Any]:
        patterns = {
            "database_locked": re.compile(r"database is locked|database table is locked", re.I),
            "traceback": re.compile(r"Traceback \(most recent call last\):"),
            "uncaught": re.compile(r"uncaught exception|unhandled exception", re.I),
            "oom": re.compile(r"out of memory|oom-kill|killed process", re.I),
        }
        result: dict[str, Any] = {}
        for controller in controllers:
            counts = {name: 0 for name in patterns}
            samples = {name: [] for name in patterns}
            log_paths = sorted((controller.runtime_root / "logs").glob("*.log")) + sorted((controller.runtime_root / "logs").glob("*.out"))
            for path in log_paths:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for name, pattern in patterns.items():
                    matches = list(pattern.finditer(text))
                    counts[name] += len(matches)
                    for match in matches[: max(0, 10 - len(samples[name]))]:
                        start = max(0, match.start() - 120)
                        end = min(len(text), match.end() + 240)
                        samples[name].append({"path": str(path), "text": text[start:end].replace("\n", " ")[:500]})
            result[controller.name] = {"counts": counts, "samples": samples, "paths": [str(path) for path in log_paths]}
        return result

    def final_control_checks(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for controller in (self.primary, self.recovery):
            root = WebClient(controller.base_url, "root", self.credentials.root, timeout=120)
            login = root.login()
            checks = {
                "version": root.request("GET", "/api/version"),
                "readiness": root.request("GET", "/api/admin/health/readiness"),
                "security_center": root.request("GET", "/api/admin/security-center"),
                "log_chain": root.request("GET", "/api/root/server-mode/logs/verify"),
                "points_chain": root.request("GET", "/api/root/points/chain/verify"),
                "financial_invariants": root.request("GET", "/api/root/points/financial-invariants"),
                "ai_agent": root.request("GET", "/api/ai-agent/status"),
            } if login.get("ok") else {}
            result[controller.name] = {
                "login_status": login.get("status"),
                "checks": {name: {"ok": value.get("ok"), "status": value.get("status"), "elapsed_ms": value.get("elapsed_ms")} for name, value in checks.items()},
                "ok": bool(login.get("ok") and checks and all(int(value.get("status") or 0) in {200, 202} and value.get("ok") for value in checks.values())),
            }
        return result

    def secret_scan(self) -> dict[str, Any]:
        hits: list[dict[str, str]] = []
        protected_stores_checked: list[dict[str, Any]] = []
        values = {
            "root": self.credentials.root,
            "manager": self.credentials.manager,
            "test": self.credentials.test,
            "member": self.credentials.member,
        }
        for path in self.root.rglob("*"):
            if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
                continue
            if path.name == "restart_develop_server.env":
                mode = path.stat().st_mode & 0o777
                protected_stores_checked.append({"path": str(path), "mode": oct(mode), "ok": mode & 0o077 == 0})
                if mode & 0o077 == 0:
                    continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for label, value in values.items():
                if value and value in text:
                    hits.append({"label": label, "path": str(path)})
        return {
            "ok": not hits and all(item.get("ok") for item in protected_stores_checked),
            "hits": hits[:100],
            "hit_count": len(hits),
            "protected_secret_stores": protected_stores_checked,
        }

    def write_markdown(self, payload: dict[str, Any]) -> Path:
        path = self.final_path.with_suffix(".md")
        lines = [
            "# 24-Hour Operational Campaign",
            "",
            f"- Verdict: `{payload.get('verdict')}`",
            f"- Production sign-off eligible: `{payload.get('production_signoff_eligible')}`",
            f"- Required active time: `{payload.get('required_active_test_seconds')}s`",
            f"- Actual active time: `{payload.get('active_test_seconds')}s`",
            f"- Primary URL: `{self.primary.base_url}`",
            f"- Recovery URL: `{self.recovery.base_url}`",
            "",
            "## Scenarios",
            "",
        ]
        for spec in self.scenario_specs():
            item = (payload.get("scenarios") or {}).get(spec.scenario_id) or {}
            lines.append(f"- `{'PASS' if item.get('ok') else 'FAIL'}` {spec.category}: `{spec.scenario_id}`")
        lines.extend(["", "## Findings", ""])
        findings = payload.get("findings") or []
        lines.extend(f"- `{item.get('severity', 'unknown')}` {item.get('title')}" for item in findings)
        if not findings:
            lines.append("- none")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def run(self) -> int:
        self.root.mkdir(parents=True, exist_ok=False)
        self.reports.mkdir(parents=True, exist_ok=True)
        preflight = self.preflight()
        if not preflight.get("ok"):
            payload = {"ok": False, "verdict": "FAIL", "phase": "preflight", "preflight": preflight}
            atomic_write_json(self.final_path, payload)
            return 2
        primary_start = self.primary.start()
        recovery_start = self.recovery.start()
        if not primary_start.get("ok") or not recovery_start.get("ok"):
            payload = {
                "ok": False,
                "verdict": "FAIL",
                "phase": "server_start",
                "primary_start": primary_start,
                "recovery_start": recovery_start,
            }
            atomic_write_json(self.final_path, payload)
            self.primary.stop(reason="start_failure_cleanup")
            self.recovery.stop(reason="start_failure_cleanup")
            return 2
        self.provision_accounts()
        core_start = self.start_core_soak()
        if not core_start.get("ok"):
            raise RuntimeError(f"core soak failed to start: {core_start}")

        self.active_started = time.monotonic()
        self.active_started_at = utc_now()
        self.resource_monitor.start()
        self.start_scenarios()
        self.active_event.set()
        self.write_checkpoint("active_campaign_started")
        next_heartbeat = 0.0
        while self.core_process and self.core_process.poll() is None and not self.stop_event.is_set():
            if self.elapsed() >= next_heartbeat:
                self.check_drift()
                self.write_checkpoint("core_soak_running")
                print(json.dumps({
                    "event": "campaign_heartbeat",
                    "active_test_seconds": round(self.elapsed(), 1),
                    "remaining_seconds": round(max(0.0, int(self.args.duration_seconds) - self.elapsed()), 1),
                    "scenarios_completed": len(self.scenario_results),
                    "core_pid": self.core_process.pid,
                }, ensure_ascii=False), flush=True)
                next_heartbeat = self.elapsed() + max(10.0, float(self.args.heartbeat_interval))
            self.stop_event.wait(2)

        core_returncode = self.core_process.poll() if self.core_process else 127
        if not self.required_duration_completed():
            # Wake delayed scenarios immediately when the continuous load driver
            # dies early; otherwise a failed smoke can wait for the full join timeout.
            self.stop_event.set()
        if self.core_process and core_returncode is None:
            terminate_process_group(self.core_process)
            core_returncode = self.core_process.poll()
        if self.core_stdout_handle:
            self.core_stdout_handle.close()
            self.core_stdout_handle = None

        scenario_join_deadline = time.monotonic() + int(self.args.scenario_join_timeout_seconds)
        for thread in self.scenario_threads:
            remaining = max(0.0, scenario_join_deadline - time.monotonic())
            thread.join(timeout=remaining)
        unfinished = [thread.name for thread in self.scenario_threads if thread.is_alive()]
        if unfinished:
            self.stop_event.set()
        self.check_drift()
        self.write_checkpoint("collecting_final_evidence")

        control_checks = self.final_control_checks()
        self.resource_monitor.stop()
        self.resource_monitor.join(timeout=30)
        resources = self.resource_monitor.summary()
        server_logs = self.scan_server_logs([self.primary, self.recovery])
        secret_scan = self.secret_scan()
        active_seconds = self.elapsed()
        core_payload = load_json(self.core_report)
        specs = self.scenario_specs()
        missing_scenarios = [spec.scenario_id for spec in specs if spec.scenario_id not in self.scenario_results]
        failed_scenarios = [spec.scenario_id for spec in specs if not (self.scenario_results.get(spec.scenario_id) or {}).get("ok")]
        findings: list[dict[str, Any]] = []
        if active_seconds + 1 < int(self.args.duration_seconds):
            findings.append({"severity": "critical", "title": "required active campaign duration was not completed", "actual": active_seconds})
        if int(core_returncode or 0) != 0 or not core_payload.get("ok"):
            findings.append({"severity": "critical", "title": "continuous primary operational soak failed", "returncode": core_returncode})
        if missing_scenarios:
            findings.append({"severity": "critical", "title": "mandatory campaign scenarios did not run", "scenarios": missing_scenarios})
        if failed_scenarios:
            findings.append({"severity": "high", "title": "mandatory campaign scenarios failed", "scenarios": failed_scenarios})
        if unfinished:
            findings.append({"severity": "critical", "title": "scenario workers did not finish", "threads": unfinished})
        if self.drift:
            findings.append({"severity": "critical", "title": "source or test harness changed during campaign", "files": self.drift})
        minimum_samples = max(2, int(int(self.args.duration_seconds) / max(1.0, float(self.args.resource_interval)) * 0.8))
        if int(resources.get("samples") or 0) < minimum_samples:
            findings.append({"severity": "high", "title": "resource evidence sample coverage is incomplete", "samples": resources.get("samples"), "required": minimum_samples})
        for name, evidence in (resources.get("servers") or {}).items():
            if int(evidence.get("unplanned_health_failures") or 0) > 0:
                findings.append({"severity": "high", "title": f"{name} sentinel observed unplanned transport failure", "count": evidence.get("unplanned_health_failures")})
            if float((evidence.get("health_latency_ms") or {}).get("p95") or 0) > float(self.args.max_sentinel_p95_ms):
                findings.append({"severity": "high", "title": f"{name} sentinel p95 exceeded SLA", "p95_ms": (evidence.get("health_latency_ms") or {}).get("p95")})
            if float(evidence.get("max_rss_mb") or 0) <= 0:
                findings.append({"severity": "high", "title": f"{name} resource sampler captured no process RSS"})
        for name, evidence in server_logs.items():
            counts = evidence.get("counts") or {}
            if int(counts.get("database_locked") or 0) > 0:
                findings.append({"severity": "high", "title": f"{name} logged SQLite lock failures", "count": counts.get("database_locked")})
            if int(counts.get("traceback") or 0) > 0 or int(counts.get("uncaught") or 0) > 0 or int(counts.get("oom") or 0) > 0:
                findings.append({"severity": "high", "title": f"{name} logged unhandled server failures", "counts": counts})
        if not all(item.get("ok") for item in control_checks.values()):
            findings.append({"severity": "high", "title": "final control-plane verification failed"})
        if not secret_scan.get("ok"):
            findings.append({"severity": "critical", "title": "campaign artifacts contain credential material", "hits": secret_scan.get("hits")})

        formal = not self.args.allow_short_duration and int(self.args.duration_seconds) >= MIN_FORMAL_SECONDS
        ok = not findings
        payload = {
            "ok": ok,
            "verdict": "PASS" if ok else "FAIL",
            "production_signoff_eligible": bool(ok and formal),
            "formal_campaign": formal,
            "started_at": self.active_started_at,
            "finished_at": utc_now(),
            "required_active_test_seconds": int(self.args.duration_seconds),
            "active_test_seconds": round(active_seconds, 3),
            "authorization_wait_seconds_included": 0,
            "preflight": preflight,
            "primary_start": primary_start,
            "recovery_start": recovery_start,
            "core_soak": {
                "returncode": core_returncode,
                "report": str(self.core_report),
                "result": core_payload,
                "command": sanitized_command(self.core_command),
            },
            "scenarios": self.scenario_results,
            "resources": resources,
            "resource_samples": str(self.resource_monitor.out),
            "server_logs": server_logs,
            "control_checks": control_checks,
            "secret_scan": secret_scan,
            "server_events": {"primary": self.primary.events, "recovery": self.recovery.events},
            "source_manifest_digest": self.source_digest,
            "source_git": self.source_git,
            "source_drift": self.drift,
            "findings": findings,
        }
        atomic_write_json(self.final_path, payload)
        markdown = self.write_markdown(payload)
        payload["markdown_report"] = str(markdown)
        atomic_write_json(self.final_path, payload)
        atomic_write_json(self.checkpoint_path, {
            "status": "complete",
            "verdict": payload["verdict"],
            "production_signoff_eligible": payload["production_signoff_eligible"],
            "active_test_seconds": payload["active_test_seconds"],
            "report": str(self.final_path),
            "markdown_report": str(markdown),
        })
        if not self.args.keep_servers:
            self.primary.stop(reason="campaign_complete")
            self.recovery.stop(reason="campaign_complete")
        print(json.dumps({
            "ok": ok,
            "verdict": payload["verdict"],
            "production_signoff_eligible": payload["production_signoff_eligible"],
            "active_test_seconds": payload["active_test_seconds"],
            "report": str(self.final_path),
            "findings": findings,
        }, ensure_ascii=False, indent=2), flush=True)
        return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", required=True, help="New campaign directory below /tmp.")
    parser.add_argument("--duration-seconds", type=int, default=MIN_FORMAL_SECONDS)
    parser.add_argument("--allow-short-duration", action="store_true", help="Development harness validation only; never sign-off evidence.")
    parser.add_argument("--primary-port", type=int, default=0)
    parser.add_argument("--recovery-port", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--account-count", type=int, default=10)
    parser.add_argument("--round-ops", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--session-pool", type=int, default=20)
    parser.add_argument("--browser-interval-seconds", type=int, default=3 * 60 * 60)
    parser.add_argument("--resource-interval", type=float, default=5.0)
    parser.add_argument("--heartbeat-interval", type=float, default=60.0)
    parser.add_argument("--scenario-join-timeout-seconds", type=int, default=8 * 60 * 60)
    parser.add_argument("--minimum-free-gb", type=float, default=12.0)
    parser.add_argument("--max-server-busy-rate", type=float, default=0.05)
    parser.add_argument("--max-ordinary-p95-ms", type=float, default=3000.0)
    parser.add_argument("--max-ordinary-p99-ms", type=float, default=8000.0)
    parser.add_argument("--max-sentinel-p95-ms", type=float, default=3000.0)
    parser.add_argument("--keep-servers", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if int(args.duration_seconds) < MIN_FORMAL_SECONDS and not args.allow_short_duration:
        raise SystemExit(f"formal campaign requires at least {MIN_FORMAL_SECONDS} active seconds")
    root = validate_tmp_path(Path(args.campaign_root), label="campaign root")
    if root.exists():
        raise SystemExit(f"campaign root must not already exist: {root}")
    campaign = Campaign(args)

    def stop_handler(_signum: int, _frame: Any) -> None:
        campaign.stop_event.set()
        if campaign.core_process:
            terminate_process_group(campaign.core_process)

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        return campaign.run()
    except Exception as exc:
        campaign.stop_event.set()
        campaign.resource_monitor.stop()
        if campaign.resource_monitor.is_alive():
            campaign.resource_monitor.join(timeout=10)
        if campaign.core_process:
            terminate_process_group(campaign.core_process)
        if campaign.core_stdout_handle:
            campaign.core_stdout_handle.close()
        if not args.keep_servers:
            campaign.primary.stop(reason="campaign_exception")
            campaign.recovery.stop(reason="campaign_exception")
        payload = {
            "ok": False,
            "verdict": "FAIL",
            "phase": "exception",
            "error": f"{exc.__class__.__name__}: {exc}",
            "at": utc_now(),
        }
        campaign.reports.mkdir(parents=True, exist_ok=True)
        atomic_write_json(campaign.final_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
