#!/usr/bin/env python3
"""Pre-deploy capacity probe for hackme_web.

This script starts isolated temporary gunicorn instances through
test_for_develop.sh, runs mixed frontend/API account activity, records latency
and worker CPU samples, then removes the temporary runtime by default.

It is designed for machine sizing before deployment. It does not use the repo
runtime database or uploads directory.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import shutil
import signal
import socket
import string
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


REPO_ROOT = Path(__file__).resolve().parents[2]
if not (REPO_ROOT / "test_for_develop.sh").exists():
    raise RuntimeError(
        f"Could not locate repo root from {__file__}; expected test_for_develop.sh at {REPO_ROOT}"
    )
DEFAULT_CAPACITY_DEFAULTS_FILE = REPO_ROOT / ".hackme_capacity_defaults.env"
DEFAULT_ROOT_PASSWORD = "RootCapacity123!"
DEFAULT_MANAGER_PASSWORD = "ManagerCapacity123!"
DEFAULT_TEST_PASSWORD = "TestCapacity123!"
USER_PASSWORD = "CapacityUser123!"
requests.packages.urllib3.disable_warnings()

BASE_FLOW_LABELS = [
    "login",
]

LIGHT_FLOW_LABELS = [
    "me",
    "jobs",
    "shares",
    "trading dashboard",
]

BASIC_FLOW_LABELS = [
    "me",
    "jobs",
    "shares",
    "trading dashboard",
    "trading markets",
    "games catalog",
    "storage upload text",
    "drive preview",
]

NORMAL_FLOW_LABELS = [
    "points wallet",
    "points wallet onboarding",
    "points wallet create official hot",
    "points ledger",
    "points explorer missing tx",
    "points transfer normal",
    "points transfer insufficient rejected",
    "points explorer tx",
    "points governance proposals",
    "points governance public proposal",
    "points governance vote",
    "points transaction disputes",
    "points transaction dispute create",
    "appeals list",
    "appeals submit none rejected",
    "me",
    "jobs",
    "shares",
    "trading dashboard",
    "trading markets",
    "trading asset overview",
    "trading live price",
    "trading bot competition",
    "trading spot market buy",
    "trading spot limit buy",
    "trading bots",
    "trading dca bot create",
    "trading workflow bot create",
    "trading bots scan",
    "grid bots",
    "trading grid preview",
    "trading grid bot create",
    "trading grid bots scan",
    "trading margin long open",
    "trading margin short open",
    "trading margin close",
    "videos",
    "games catalog",
    "chat rooms",
    "chat normal message",
    "community boards",
    "community create thread",
    "storage upload text",
    "drive preview",
    "drive share link",
    "album create private",
    "album add file",
    "album enable share",
    "game tetris score",
]

MALICIOUS_FLOW_LABELS = [
    "chat malicious text rejected_or_sanitized",
    "community xss title",
    "game invalid score rejected",
    "trading invalid market rejected",
    "trading malformed bot rejected",
    "trading invalid margin rejected",
    "points governance bad vote rejected",
    "points dispute malformed rejected",
    "appeal oversized rejected",
    "drive forbidden other user rejected",
    "drive missing download rejected",
    "csrf wrong token rejected",
]

HEAVY_FLOW_LABELS = [
    "drive repeated preview",
    "drive repeated download",
    "drive text update heavy",
    "resumable upload start",
    "resumable upload chunk",
    "resumable upload complete",
    "trading backtest dca",
    "trading backtest workflow",
    "trading history export",
    "storage albums smart organize",
]

LOAD_PROFILE_KINDS = {
    "light": {"light"},
    "basic": {"basic"},
    "normal": {"normal"},
    "malicious": {"normal", "malicious"},
    "heavy": {"normal", "heavy"},
    "full": {"normal", "malicious", "heavy"},
}

CAPACITY_TIER_PRESETS = {
    "sbc": {
        "profiles": "1x1",
        "account_counts": "1",
        "load_profile": "light",
        "max_rounds": 1,
        "max_accounts": 1,
        "ux_confirm_rounds": 0,
        "max_duration_seconds": 60,
        "request_timeout": 10.0,
        "description": "single-board computer / tiny VM; smoke-size read-only probe",
    },
    "legacy": {
        "profiles": "1x2",
        "account_counts": "1,2",
        "load_profile": "light",
        "max_rounds": 2,
        "max_accounts": 2,
        "ux_confirm_rounds": 0,
        "max_duration_seconds": 120,
        "request_timeout": 12.0,
        "description": "old desktop or low-power NAS; low-impact read-only probe",
    },
    "laptop": {
        "profiles": "1x2,2x2",
        "account_counts": "1,2",
        "load_profile": "basic",
        "max_rounds": 2,
        "max_accounts": 2,
        "ux_confirm_rounds": 1,
        "max_duration_seconds": 180,
        "request_timeout": 15.0,
        "description": "ordinary laptop; small bounded member workflow probe",
    },
    "midrange": {
        "profiles": "1x4,2x4,3x4",
        "account_counts": "auto",
        "load_profile": "normal",
        "start_accounts": 4,
        "max_rounds": 5,
        "max_accounts": 64,
        "ux_confirm_rounds": 2,
        "description": "mid-range server; bounded normal capacity search",
    },
    "highend": {
        "profiles": "1x6,2x6,3x6,4x6",
        "account_counts": "auto",
        "load_profile": "normal",
        "start_accounts": 6,
        "max_rounds": 0,
        "max_accounts": 0,
        "ux_confirm_rounds": 3,
        "description": "high-end server; unbounded search until UX/server stop targets are reached",
    },
}


def active_load_kinds(args: argparse.Namespace) -> set[str]:
    raw = str(getattr(args, "load_kinds", "") or "").strip().lower()
    if raw:
        values = {item.strip() for item in raw.split(",") if item.strip()}
    else:
        values = set(LOAD_PROFILE_KINDS.get(str(getattr(args, "load_profile", "normal")), {"normal"}))
    allowed = {"light", "basic", "normal", "malicious", "heavy"}
    unknown = sorted(values - allowed)
    if unknown:
        raise ValueError(f"unknown load kind(s): {', '.join(unknown)}")
    return values or {"normal"}


def load_includes(args: argparse.Namespace, kind: str) -> bool:
    return kind in active_load_kinds(args)


def feature_flow_labels(args: argparse.Namespace) -> list[str]:
    labels = list(BASE_FLOW_LABELS)
    kinds = active_load_kinds(args)
    if "light" in kinds:
        labels.extend(LIGHT_FLOW_LABELS)
    if "basic" in kinds:
        labels.extend(BASIC_FLOW_LABELS)
    if "normal" in kinds:
        labels.extend(NORMAL_FLOW_LABELS)
    if "malicious" in kinds:
        labels.extend(MALICIOUS_FLOW_LABELS)
    if "heavy" in kinds:
        labels.extend(HEAVY_FLOW_LABELS)
    return labels


def expected_flow_sample_count(args: argparse.Namespace) -> int:
    labels = feature_flow_labels(args)
    total = len(labels)
    if load_includes(args, "heavy"):
        repeat = max(1, min(int(getattr(args, "heavy_repeat", 1) or 1), 20))
        upload_size = max(4096, min(int(getattr(args, "heavy_upload_bytes", 0) or 0), 256 * 1024))
        chunk_size = max(2048, upload_size // 2)
        chunk_count = max(1, (upload_size + chunk_size - 1) // chunk_size)
        total += max(0, repeat - 1)       # repeated preview
        total += max(0, (repeat * 2) - 1) # cloud-drive and storage downloads share one label
        total += max(0, chunk_count - 1)  # resumable chunks share one label
    return total


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def percentile(values: list[int], pct: int) -> int:
    if not values:
        return 0
    ordered = sorted(int(v) for v in values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * pct / 100))
    return int(ordered[index])


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def parse_csv_ints(raw: str) -> list[int]:
    values: list[int] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        value = int(text)
        if value <= 0:
            raise ValueError(f"expected positive integer, got {text!r}")
        if value not in values:
            values.append(value)
    return values


def format_account_sample(usernames: list[str], *, limit: int = 18) -> str:
    if len(usernames) <= limit:
        return ", ".join(usernames)
    head_count = max(1, limit - 4)
    return ", ".join([*usernames[:head_count], "...", *usernames[-3:]])


class AccountLadder:
    def __init__(self, args: argparse.Namespace):
        raw = str(args.account_counts or "").strip().lower()
        self.auto = raw in {"", "auto", "adaptive", "probe"}
        self.index = 0
        self.values = [] if self.auto else parse_csv_ints(args.account_counts)
        self.current = max(1, int(args.start_accounts or 1))
        self.growth_factor = max(1.05, float(args.growth_factor or 1.5))
        self.fine_growth_factor = max(1.02, float(args.fine_growth_factor or 1.18))
        self.max_rounds = max(0, int(args.max_rounds or 0))
        self.max_accounts = max(0, int(args.max_accounts or 0))
        self.seen: set[int] = set()
        self._fine = False

    def label(self) -> str:
        if not self.auto:
            return ", ".join(str(value) for value in self.values)
        ceiling = f", max_accounts={self.max_accounts}" if self.max_accounts else ", no account ceiling"
        return (
            f"auto start={self.current}, growth={self.growth_factor:g}, "
            f"fine_growth={self.fine_growth_factor:g}, max_rounds={self.max_rounds or 'unlimited'}{ceiling}"
        )

    def next(self) -> int | None:
        if not self.auto:
            if self.index >= len(self.values):
                return None
            value = self.values[self.index]
            self.index += 1
            return value
        if self.max_rounds and self.index >= self.max_rounds:
            return None
        value = self.current
        if self.max_accounts and value > self.max_accounts:
            return None
        self.seen.add(value)
        self.index += 1
        factor = self.fine_growth_factor if self._fine else self.growth_factor
        next_value = max(value + 1, int(round(value * factor)))
        if self.max_accounts:
            next_value = min(next_value, self.max_accounts)
            if next_value in self.seen:
                next_value = min(self.max_accounts, value + 1)
        self.current = next_value
        return value

    def refine(self) -> None:
        self._fine = True

    def ensure_usernames(self, root: "Client", usernames: list[str], prefix: str, count: int) -> None:
        start_len = len(usernames)
        if start_len < int(count):
            print(
                f"[capacity] preparing accounts: {start_len}/{count} ready, ensuring {int(count) - start_len} more",
                flush=True,
            )
        while len(usernames) < int(count):
            username = f"{prefix}_{len(usernames)}"
            create_or_get_user(root, username)
            usernames.append(username)
            if len(usernames) == int(count) or len(usernames) % 10 == 0:
                print(f"[capacity] preparing accounts: {len(usernames)}/{count} ready", flush=True)


@dataclass(frozen=True)
class Profile:
    workers: int
    threads: int

    @property
    def label(self) -> str:
        return f"{self.workers}x{self.threads}"


def parse_profiles(raw: str) -> list[Profile]:
    profiles: list[Profile] = []
    for part in str(raw or "").split(","):
        text = part.strip().lower()
        if not text:
            continue
        match = re.fullmatch(r"(\d+)\s*x\s*(\d+)", text)
        if not match:
            raise ValueError(f"profile must look like 3x6, got {part!r}")
        profile = Profile(int(match.group(1)), int(match.group(2)))
        if profile.workers <= 0 or profile.threads <= 0:
            raise ValueError(f"profile must be positive, got {part!r}")
        if profile not in profiles:
            profiles.append(profile)
    return profiles


def default_profiles() -> list[Profile]:
    cpu = max(1, int(os.cpu_count() or 1))
    candidates = [Profile(1, 6)]
    if cpu >= 2:
        candidates.append(Profile(2, 6))
    if cpu >= 4:
        candidates.append(Profile(3, 6))
    if cpu >= 8:
        candidates.append(Profile(4, 6))
    return candidates


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Start isolated temporary hackme_web gunicorn instances and find a "
            "safe pre-deploy workers/threads/backpressure envelope."
        )
    )
    parser.add_argument(
        "--profiles",
        default="",
        help="Comma-separated workers x threads profiles, e.g. 1x6,2x6,3x6,4x6. Default is CPU-aware.",
    )
    parser.add_argument(
        "--light",
        action="store_true",
        help=(
            "Alias for --capacity-tier legacy."
        ),
    )
    parser.add_argument(
        "--capacity-tier",
        choices=["auto", *sorted(CAPACITY_TIER_PRESETS)],
        default="auto",
        help=(
            "Hardware-sized probe preset. auto keeps legacy behavior; sbc, legacy, laptop, "
            "midrange, and highend bound profiles/account counts for that class of machine."
        ),
    )
    parser.add_argument(
        "--account-counts",
        default="auto",
        help="Comma-separated concurrent account counts per profile, or auto for adaptive probing without a built-in account ceiling.",
    )
    parser.add_argument("--start-accounts", type=int, default=6, help="First account count for --account-counts auto.")
    parser.add_argument("--growth-factor", type=float, default=1.5, help="Auto ladder growth factor before thresholds are approached.")
    parser.add_argument("--fine-growth-factor", type=float, default=1.18, help="Auto ladder growth factor after UX degradation is observed.")
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=8,
        help="Safety stop for auto ladders. Use 0 for no round limit when intentionally probing crash boundaries.",
    )
    parser.add_argument(
        "--max-accounts",
        type=int,
        default=256,
        help="Safety account ceiling for auto ladders. Use 0 for no ceiling when intentionally probing crash boundaries.",
    )
    parser.add_argument("--target-p95-ms", type=int, default=1500, help="Preferred p95 latency ceiling for recommended defaults.")
    parser.add_argument("--ux-p95-ms", type=int, default=2000, help="p95 threshold where user experience starts to feel poor.")
    parser.add_argument("--ux-p99-ms", type=int, default=4000, help="p99 threshold where tail latency starts to feel poor.")
    parser.add_argument(
        "--ux-confirm-rounds",
        type=int,
        default=3,
        help="When a round crosses UX latency thresholds, rerun the same account count this many times; only if every confirmation also crosses the threshold is it reported as the UX degradation point.",
    )
    parser.add_argument(
        "--hard-p95-ms",
        type=int,
        default=8000,
        help="Server-instability p95 ceiling. This is intentionally higher than the UX threshold.",
    )
    parser.add_argument("--hard-max-ms", type=int, default=20000, help="Server-instability max latency ceiling.")
    parser.add_argument(
        "--close-connections",
        action="store_true",
        help=(
            "Send Connection: close from the probe client. Keep this off for capacity "
            "sizing; enable it only for explicit close-path compatibility testing."
        ),
    )
    parser.add_argument(
        "--gunicorn-max-requests",
        type=int,
        default=10000,
        help="Gunicorn max-requests for probe servers. Default avoids worker recycling during short probes; 0 disables it.",
    )
    parser.add_argument("--gunicorn-max-requests-jitter", type=int, default=1000)
    parser.add_argument("--hard-failure-stop", action="store_true", default=True, help=argparse.SUPPRESS)
    parser.add_argument(
        "--continue-after-failure",
        action="store_false",
        dest="hard_failure_stop",
        help="Continue probing higher account counts even after failures.",
    )
    parser.add_argument(
        "--continue-after-app-limit",
        action="store_true",
        help="Continue probing after 429 application/rate limits. Results above that point may understate real load.",
    )
    parser.add_argument(
        "--keep-app-limits",
        action="store_true",
        help="Keep login/user/upload abuse limits enabled. Default disables them inside isolated probe servers.",
    )
    parser.add_argument(
        "--disable-backpressure",
        action="store_true",
        help="Disable server backpressure too. Use only when probing bare-process crash behavior.",
    )
    parser.add_argument("--port", type=int, default=0, help="Base port. Default picks free ports automatically.")
    parser.add_argument("--root-password", default=DEFAULT_ROOT_PASSWORD)
    parser.add_argument("--manager-password", default=DEFAULT_MANAGER_PASSWORD)
    parser.add_argument("--test-password", default=DEFAULT_TEST_PASSWORD)
    parser.add_argument(
        "--tmp-parent",
        default=tempfile.gettempdir(),
        help="Parent directory for isolated test roots. Default: system temp dir.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="JSON report path. Default writes under the top-level temp probe directory.",
    )
    parser.add_argument("--keep-run-root", action="store_true", help="Keep temp server roots for debugging.")
    parser.add_argument("--install", action="store_true", help="Allow test_for_develop.sh to install dependencies.")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    parser.add_argument(
        "--max-duration-seconds",
        type=float,
        default=0.0,
        help="Stop starting new capacity profiles/rounds after this many seconds. 0 means no total time ceiling.",
    )
    parser.add_argument("--cpu-sample-interval", type=float, default=0.35)
    parser.add_argument("--progress-interval", type=float, default=5.0, help="Seconds between live progress dashboard updates.")
    parser.add_argument("--progress-active-limit", type=int, default=0, help="Maximum active accounts shown per progress update. Default 0 shows all active accounts.")
    parser.add_argument("--no-progress", action="store_true", help="Disable live per-round progress dashboard.")
    parser.add_argument(
        "--load-profile",
        choices=sorted(LOAD_PROFILE_KINDS),
        default="normal",
        help=(
            "Capacity traffic shape. normal runs regular member activity; malicious adds attack/exception probes; "
                "basic adds a small bounded member workflow; heavy adds repeated upload/download/backtest work; full enables all."
        ),
    )
    parser.add_argument(
        "--load-kinds",
        default="",
        help="Advanced override for --load-profile: comma-separated normal,malicious,heavy.",
    )
    parser.add_argument(
        "--heavy-repeat",
        type=int,
        default=2,
        help="Repeat count for heavy upload/download/read operations when heavy load is enabled.",
    )
    parser.add_argument(
        "--heavy-upload-bytes",
        type=int,
        default=32768,
        help="Approximate per-account payload size for heavy file operations.",
    )
    parser.add_argument(
        "--round-cooldown-seconds",
        type=float,
        default=0.0,
        help=(
            "Sleep between account-count rounds. Use 60+ seconds when you want to "
            "avoid cumulative login/rate-limit contamination while searching for the server crash threshold."
        ),
    )
    parser.add_argument(
        "--capacity-defaults-file",
        default=str(DEFAULT_CAPACITY_DEFAULTS_FILE),
        help=(
            "Local env file updated with the recommended defaults after a successful capacity run. "
            "Default: repo .hackme_capacity_defaults.env"
        ),
    )
    parser.add_argument(
        "--no-sync-defaults",
        action="store_true",
        help="Do not update the local capacity defaults env file after the probe.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


class Client:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 30.0,
        *,
        close_connections: bool = False,
        progress: "RoundProgress | None" = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.progress = progress
        self.session = requests.Session()
        self.session.verify = False
        if close_connections:
            self.session.headers.update({"Connection": "close"})

    def refresh_csrf(self) -> str:
        response = self.session.get(f"{self.base_url}/api/csrf-token", timeout=self.timeout)
        response.raise_for_status()
        return str(response.json()["csrf_token"])

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected: set[int] | None = None,
        acceptable_business_errors: dict[int, tuple[str, ...]] | None = None,
        label: str = "",
    ) -> dict[str, Any]:
        request_label = label or f"{method} {path}"
        req_headers = dict(headers or {})
        if self.progress is not None:
            self.progress.start_step(self.username, request_label)
        started = time.perf_counter()
        try:
            if method.upper() not in {"GET", "HEAD", "OPTIONS"} and "X-CSRF-Token" not in req_headers:
                req_headers["X-CSRF-Token"] = self.refresh_csrf()
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                files=files,
                data=data,
                headers=req_headers,
                timeout=self.timeout,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            try:
                body: Any = response.json()
            except Exception:
                body = {"text": response.text[:500]}
            status = int(response.status_code)
            ok = status in (expected or {200})
            accepted_business_error = ""
            if not ok and acceptable_business_errors:
                message = str((body or {}).get("msg") or (body or {}).get("error") or "")
                for fragment in acceptable_business_errors.get(status, ()):
                    if fragment and fragment in message:
                        ok = True
                        accepted_business_error = message
                        break
            result = {
                "label": request_label,
                "user": self.username,
                "status": status,
                "ok": ok,
                "elapsed_ms": elapsed_ms,
                "body": body,
            }
            if accepted_business_error:
                result["accepted_business_error"] = accepted_business_error
        except Exception as exc:
            result = {
                "label": request_label,
                "user": self.username,
                "status": 0,
                "ok": False,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "error": repr(exc),
            }
        if self.progress is not None:
            self.progress.finish_step(self.username, result)
        return result

    def login(self) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/login",
            json_body={"username": self.username, "password": self.password},
            expected={200},
            label="login",
        )


def wait_for_server(base_url: str, timeout_seconds: float) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url}/api/version", timeout=3, verify=False)
            if response.status_code == 200 and response.json().get("ok"):
                return
        except Exception as exc:
            last_error = repr(exc)
        time.sleep(0.5)
    raise RuntimeError(f"server did not become ready at {base_url}: {last_error}")


def descendants_of(pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            raw = stat_path.read_text(encoding="utf-8", errors="ignore")
            right = raw.rsplit(")", 1)[1].strip().split()
            child_pid = int(stat_path.parent.name)
            parent_pid = int(right[1])
        except Exception:
            continue
        children.setdefault(parent_pid, []).append(child_pid)
    result: list[int] = []
    stack = list(children.get(pid, []))
    while stack:
        child = stack.pop()
        if child in result:
            continue
        result.append(child)
        stack.extend(children.get(child, []))
    return sorted(result)


@dataclass
class CpuSample:
    ts: float
    processes: list[dict[str, Any]]


class CpuSampler:
    def __init__(self, master_pid: int, interval: float = 0.35):
        self.master_pid = int(master_pid)
        self.interval = max(0.1, float(interval))
        self.samples: list[CpuSample] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cpu-sampler", daemon=True)

    def __enter__(self) -> "CpuSampler":
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            pids = [self.master_pid] + descendants_of(self.master_pid)
            processes: list[dict[str, Any]] = []
            if pids:
                proc = subprocess.run(
                    ["ps", "-o", "pid=,ppid=,pcpu=,pmem=,rss=,comm=", "-p", ",".join(str(pid) for pid in pids)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=str(REPO_ROOT),
                )
                for line in proc.stdout.splitlines():
                    parts = line.split(None, 5)
                    if len(parts) < 6:
                        continue
                    try:
                        processes.append({
                            "pid": int(parts[0]),
                            "ppid": int(parts[1]),
                            "pcpu": float(parts[2]),
                            "pmem": float(parts[3]),
                            "rss_mb": round(float(parts[4]) / 1024.0, 2),
                            "comm": parts[5],
                        })
                    except ValueError:
                        continue
            self.samples.append(CpuSample(ts=time.time(), processes=processes))
            self._stop.wait(self.interval)

    def summary(self) -> dict[str, Any]:
        total_cpu_peak = 0.0
        total_cpu_avg_values: list[float] = []
        total_mem_peak = 0.0
        total_rss_peak = 0.0
        total_rss_latest = 0.0
        active_worker_peak = 0
        workers_seen: set[int] = set()
        for sample in self.samples:
            worker_rows = [row for row in sample.processes if int(row.get("pid") or 0) != self.master_pid]
            for row in worker_rows:
                workers_seen.add(int(row["pid"]))
            total = sum(float(row.get("pcpu") or 0.0) for row in worker_rows)
            total_cpu_peak = max(total_cpu_peak, total)
            total_cpu_avg_values.append(total)
            active_worker_peak = max(active_worker_peak, sum(1 for row in worker_rows if float(row.get("pcpu") or 0.0) >= 1.0))
            total_mem_peak = max(total_mem_peak, sum(float(row.get("pmem") or 0.0) for row in sample.processes))
            rss_total = sum(float(row.get("rss_mb") or 0.0) for row in sample.processes)
            total_rss_peak = max(total_rss_peak, rss_total)
            total_rss_latest = rss_total
        avg = sum(total_cpu_avg_values) / len(total_cpu_avg_values) if total_cpu_avg_values else 0.0
        return {
            "sample_count": len(self.samples),
            "workers_seen": sorted(workers_seen),
            "active_worker_peak": active_worker_peak,
            "total_worker_cpu_peak_percent": round(total_cpu_peak, 2),
            "total_worker_cpu_avg_percent": round(avg, 2),
            "total_server_mem_peak_percent": round(total_mem_peak, 2),
            "total_server_rss_peak_mb": round(total_rss_peak, 2),
            "total_server_rss_latest_mb": round(total_rss_latest, 2),
            "multi_worker_cpu_observed": active_worker_peak >= 2,
            "multi_core_cpu_observed": active_worker_peak >= 2 or total_cpu_peak >= 120.0,
        }


class RoundProgress:
    def __init__(self, *, profile_label: str, account_count: int, sampler: CpuSampler, args: argparse.Namespace):
        self.profile_label = profile_label
        self.account_count = int(account_count)
        self.sampler = sampler
        self.interval = max(0.5, float(args.progress_interval or 0.0))
        self.active_limit = max(0, int(args.progress_active_limit or 0))
        self.disabled = bool(args.no_progress) or float(args.progress_interval or 0.0) <= 0
        self.feature_labels = feature_flow_labels(args)
        self.expected_samples = self.account_count * expected_flow_sample_count(args)
        self.started_at = time.time()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.users: dict[str, dict[str, Any]] = {}
        self.label_latencies: dict[str, list[int]] = {}
        self.label_status_counts: dict[str, dict[str, int]] = {}
        self.completed_accounts = 0
        self.completed_samples = 0
        self.thread = threading.Thread(target=self._run, name="capacity-progress", daemon=True)

    def start(self) -> None:
        print(
            f"[capacity] per-account flow ({len(self.feature_labels)} groups, ~{self.expected_samples // max(1, self.account_count)} samples/account): "
            + " -> ".join(self.feature_labels),
            flush=True,
        )
        if not self.disabled:
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if not self.disabled:
            self.thread.join(timeout=2.0)
        self.print_snapshot(final=True)

    def start_user(self, username: str, index: int) -> None:
        with self.lock:
            self.users[username] = {
                "index": int(index),
                "current": "starting",
                "completed": 0,
                "last_status": "",
                "last_ms": 0,
                "done": False,
            }

    def start_step(self, username: str, label: str) -> None:
        with self.lock:
            row = self.users.setdefault(username, {"index": 0, "completed": 0, "done": False})
            row["current"] = label

    def finish_step(self, username: str, sample: dict[str, Any]) -> None:
        label = str(sample.get("label") or "unknown")
        elapsed_ms = int(sample.get("elapsed_ms") or 0)
        status = str(sample.get("status") or 0)
        with self.lock:
            row = self.users.setdefault(username, {"index": 0, "completed": 0, "done": False})
            row["completed"] = int(row.get("completed") or 0) + 1
            row["last_status"] = status
            row["last_ms"] = elapsed_ms
            row["current"] = f"finished {label}"
            self.completed_samples += 1
            self.label_latencies.setdefault(label, []).append(elapsed_ms)
            counts = self.label_status_counts.setdefault(label, {})
            counts[status] = counts.get(status, 0) + 1

    def finish_user(self, username: str) -> None:
        with self.lock:
            row = self.users.setdefault(username, {"index": 0, "completed": 0})
            if not row.get("done"):
                row["done"] = True
                row["current"] = "done"
                self.completed_accounts += 1

    def _bar(self, value: int, total: int, width: int = 24) -> str:
        total = max(1, int(total))
        filled = max(0, min(width, int(width * value / total)))
        return "[" + "#" * filled + "." * (width - filled) + "]"

    def _slow_labels(self) -> list[str]:
        rows: list[tuple[int, str, int, int]] = []
        for label, values in self.label_latencies.items():
            if not values:
                continue
            p95 = percentile(values, 95)
            rows.append((p95, label, max(values), len(values)))
        rows.sort(reverse=True)
        return [f"{label}:p95={p95}ms max={max_ms}ms n={count}" for p95, label, max_ms, count in rows[:6]]

    def print_snapshot(self, *, final: bool = False) -> None:
        with self.lock:
            users = dict(self.users)
            completed_accounts = self.completed_accounts
            completed_samples = self.completed_samples
            active_rows = [
                (int(row.get("index") or 0), username, dict(row))
                for username, row in users.items()
                if not row.get("done")
            ]
            active_rows.sort(key=lambda item: item[0])
            slow_labels = self._slow_labels()
        elapsed = max(0.001, time.time() - self.started_at)
        cpu = self.sampler.summary()
        prefix = "[capacity][progress-final]" if final else "[capacity][progress]"
        print(
            f"{prefix} {self.profile_label} accounts={self.account_count} "
            f"{self._bar(completed_accounts, self.account_count)} "
            f"users={completed_accounts}/{self.account_count} "
            f"samples={completed_samples}/~{self.expected_samples} "
            f"elapsed={elapsed:.1f}s "
            f"cpu_peak={cpu.get('total_worker_cpu_peak_percent')}% "
            f"cpu_avg={cpu.get('total_worker_cpu_avg_percent')}% "
            f"rss_now={cpu.get('total_server_rss_latest_mb')}MB "
            f"rss_peak={cpu.get('total_server_rss_peak_mb')}MB",
            flush=True,
        )
        if active_rows:
            rendered = []
            rows_to_render = active_rows if self.active_limit == 0 else active_rows[: self.active_limit]
            for _idx, username, row in rows_to_render:
                rendered.append(
                    f"{username}:{row.get('current')} "
                    f"({row.get('completed', 0)}/~{self.expected_samples // max(1, self.account_count)}, "
                    f"last={row.get('last_status', '')}/{row.get('last_ms', 0)}ms)"
                )
            more = len(active_rows) - len(rendered)
            suffix = f"; +{more} more active" if more > 0 else ""
            print(f"{prefix} active: " + "; ".join(rendered) + suffix, flush=True)
        if slow_labels:
            print(f"{prefix} slow labels: " + " | ".join(slow_labels), flush=True)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            self.print_snapshot()


@dataclass
class ServerInstance:
    profile: Profile
    port: int
    run_root: Path
    base_url: str
    master_pid: int
    log_path: Path


def start_isolated_server(args: argparse.Namespace, profile: Profile, run_root: Path, port: int) -> ServerInstance:
    command = [
        str(REPO_ROOT / "test_for_develop.sh"),
        "--cli",
        "--run-root",
        str(run_root),
        "--port",
        str(port),
        "--port-conflict",
        "fail",
        "--server-runner",
        "gunicorn",
        "--gunicorn-workers",
        str(profile.workers),
        "--gunicorn-threads",
        str(profile.threads),
        "--gunicorn-max-requests",
        str(max(0, int(args.gunicorn_max_requests))),
        "--gunicorn-max-requests-jitter",
        str(max(0, int(args.gunicorn_max_requests_jitter))),
        "--root-password",
        args.root_password,
        "--manager-password",
        args.manager_password,
        "--test-password",
        args.test_password,
    ]
    if not args.install:
        command.append("--skip-install")
    env = dict(os.environ)
    env["HTML_LEARNING_HOST"] = "127.0.0.1"
    env["HTML_LEARNING_PORT"] = str(port)
    if not args.keep_app_limits:
        env["HACKME_CAPACITY_PROBE_UNLIMITED"] = "1"
    if args.disable_backpressure:
        env["HTML_LEARNING_BACKPRESSURE_ENABLED"] = "0"
    proc = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"test_for_develop failed for {profile.label}:\n{proc.stdout[-4000:]}")
    output = proc.stdout or ""
    pid_match = re.search(r"^\[dev-tmp\]\s+pid:\s+(\d+)\s*$", output, flags=re.MULTILINE)
    log_match = re.search(r"^\[dev-tmp\]\s+log:\s+(.+?)\s*$", output, flags=re.MULTILINE)
    if not pid_match:
        raise RuntimeError(f"could not parse gunicorn pid from test_for_develop output:\n{output[-4000:]}")
    instance = ServerInstance(
        profile=profile,
        port=port,
        run_root=run_root,
        base_url=f"https://127.0.0.1:{port}",
        master_pid=int(pid_match.group(1)),
        log_path=Path(log_match.group(1).strip()) if log_match else run_root / "hackme_web" / "runtime" / "logs" / "server_direct.out",
    )
    wait_for_server(instance.base_url, args.startup_timeout)
    return instance


def stop_server(instance: ServerInstance) -> None:
    try:
        os.kill(instance.master_pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            os.kill(instance.master_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    try:
        os.kill(instance.master_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def root_client(base_url: str, args: argparse.Namespace) -> Client:
    client = Client(
        base_url,
        "root",
        args.root_password,
        timeout=args.request_timeout,
        close_connections=bool(args.close_connections),
    )
    login = client.login()
    if not login.get("ok"):
        raise RuntimeError(f"root login failed: {login}")
    return client


def create_or_get_user(root: Client, username: str) -> int:
    found = root.request("GET", f"/api/admin/users?q={username}&page_size=100", expected={200}, label="admin search user")
    for item in (found.get("body") or {}).get("users") or []:
        if item.get("username") == username:
            return int(item["id"])
    payload = {
        "username": username,
        "password": USER_PASSWORD,
        "password_confirm": USER_PASSWORD,
        "nickname": username,
        "role": "user",
        "status": "active",
    }
    created = root.request("POST", "/api/admin/users", json_body=payload, expected={200, 409}, label="admin create user")
    if created.get("status") not in {200, 409}:
        raise RuntimeError(f"create user failed: {created}")
    found = root.request("GET", f"/api/admin/users?q={username}&page_size=100", expected={200}, label="admin search after create")
    for item in (found.get("body") or {}).get("users") or []:
        if item.get("username") == username:
            return int(item["id"])
    raise RuntimeError(f"user not found after create: {username}")


def collect_official_hot_wallet_addresses(
    base_url: str,
    args: argparse.Namespace,
    usernames: list[str],
    *,
    ensure_missing: bool = False,
) -> dict[str, str]:
    """Fetch member pc0 hot-wallet addresses for real member-to-member transfer load."""
    if not usernames:
        return {}
    root = root_client(base_url, args)
    addresses: dict[str, str] = {}
    for username in usernames:
        if ensure_missing:
            create_or_get_user(root, username)
        found = root.request(
            "GET",
            f"/api/admin/users?q={quote(username)}&page_size=100",
            expected={200},
            label="capacity lookup hot wallet",
        )
        for item in (found.get("body") or {}).get("users") or []:
            if item.get("username") != username:
                continue
            address = str(item.get("official_hot_wallet_address") or "").strip()
            if address:
                addresses[username] = address
            break
    return addresses


def peer_hot_wallet_targets(usernames: list[str], addresses: dict[str, str]) -> dict[str, str]:
    targets: dict[str, str] = {}
    ordered = [(username, addresses.get(username, "")) for username in usernames if addresses.get(username, "")]
    if len(ordered) < 2:
        return targets
    for index, (username, _address) in enumerate(ordered):
        targets[username] = ordered[(index + 1) % len(ordered)][1]
    return targets


def capacity_transfer_sink_username(run_id: str) -> str:
    digest = hashlib.sha256(str(run_id or "capacity").encode("utf-8")).hexdigest()
    return f"cap_sink_{digest[:22]}"


def write_fixture(run_root: Path, username: str, run_id: str) -> Path:
    fixture_dir = run_root / "fixtures" / run_id
    fixture_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", username)
    path = fixture_dir / f"{safe_name}.txt"
    path.write_text(f"capacity probe {username} {run_id}\n<script>alert(1)</script>\n", encoding="utf-8")
    return path


def first_chat_room(client: Client, samples: list[dict[str, Any]]) -> int:
    result = client.request("GET", "/api/chat/rooms", expected={200}, label="chat rooms")
    samples.append(result)
    rooms = (result.get("body") or {}).get("rooms") or []
    return int(rooms[0]["id"]) if rooms else 0


def first_public_board(client: Client, samples: list[dict[str, Any]]) -> int:
    result = client.request("GET", "/api/community/boards", expected={200}, label="community boards")
    samples.append(result)
    for board in (result.get("body") or {}).get("boards") or []:
        if board.get("status") == "approved" and board.get("visibility") == "public" and board.get("is_active", True):
            return int(board["id"])
    return 0


def upload_text_file(client: Client, run_root: Path, index: int, run_id: str, samples: list[dict[str, Any]]) -> tuple[str, str]:
    path = write_fixture(run_root, client.username, run_id)
    with path.open("rb") as handle:
        result = client.request(
            "POST",
            "/api/storage/files",
            files={"file": (path.name, handle, "text/plain")},
            data={
                "privacy_mode": "standard_plain",
                "virtual_path": f"/capacity/{run_id}/{index}/note.txt",
                "display_name": f"{client.username}-note.txt",
            },
            expected={200},
            label="storage upload text",
        )
    samples.append(result)
    storage_file = ((result.get("body") or {}).get("storage_file") or {})
    file_obj = ((result.get("body") or {}).get("file") or {})
    return str(storage_file.get("id") or ""), str(file_obj.get("file_id") or "")


def _append_request(samples: list[dict[str, Any]], client: Client, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    result = client.request(method, path, **kwargs)
    samples.append(result)
    return result


def _first_ledger_ref(samples: list[dict[str, Any]]) -> str:
    for sample in reversed(samples):
        body = sample.get("body") or {}
        candidates = [
            body.get("ledger_uuid"),
            body.get("transaction_hash"),
            ((body.get("ledger") or {}) if isinstance(body.get("ledger"), dict) else {}).get("ledger_uuid"),
            ((body.get("result") or {}) if isinstance(body.get("result"), dict) else {}).get("ledger_uuid"),
            ((body.get("result") or {}) if isinstance(body.get("result"), dict) else {}).get("transaction_hash"),
        ]
        for value in candidates:
            text = str(value or "").strip()
            if text:
                return text
        ledger_rows = body.get("ledger") if isinstance(body.get("ledger"), list) else []
        for row in ledger_rows:
            if not isinstance(row, dict):
                continue
            text = str(row.get("ledger_uuid") or row.get("transaction_hash") or row.get("id") or "").strip()
            if text:
                return text
    return ""


def _first_proposal_uuid(sample: dict[str, Any]) -> str:
    body = sample.get("body") or {}
    candidates = [
        ((body.get("proposal") or {}) if isinstance(body.get("proposal"), dict) else {}).get("proposal_uuid"),
        body.get("proposal_uuid"),
    ]
    for row in body.get("proposals") or []:
        if isinstance(row, dict):
            candidates.append(row.get("proposal_uuid"))
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_position_uuid(sample: dict[str, Any]) -> str:
    body = sample.get("body") or {}
    position = body.get("position") if isinstance(body.get("position"), dict) else {}
    return str(position.get("position_uuid") or body.get("position_uuid") or "").strip()


def _first_file_text(body: dict[str, Any], *path: str) -> str:
    value: Any = body
    for key in path:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return str(value or "").strip()


def _synthetic_candles(count: int = 96, *, base_price: float = 3.0) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    start = int(time.time()) - count * 60
    for idx in range(count):
        drift = (idx % 12) * 0.015
        wave = 0.04 if idx % 7 in {0, 1, 2} else -0.025
        close = max(0.1, base_price + drift + wave)
        open_price = max(0.1, close - 0.02)
        high = close + 0.06
        low = max(0.1, close - 0.07)
        candles.append({
            "time": (start + idx * 60) * 1000,
            "open": round(open_price, 6),
            "high": round(high, 6),
            "low": round(low, 6),
            "close": round(close, 6),
            "volume": 100 + idx,
        })
    return candles


def _write_heavy_fixture(run_root: Path, username: str, run_id: str, size_bytes: int) -> Path:
    fixture_dir = run_root / "fixtures" / run_id
    fixture_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", username)
    path = fixture_dir / f"{safe_name}.heavy.txt"
    size = max(1024, min(int(size_bytes or 0), 4 * 1024 * 1024))
    pattern = (f"capacity-heavy {username} {run_id}\n" * 64).encode("utf-8")
    with path.open("wb") as handle:
        remaining = size
        while remaining > 0:
            chunk = pattern[:remaining] if remaining < len(pattern) else pattern
            handle.write(chunk)
            remaining -= len(chunk)
    return path


def _points_chain_flow(
    client: Client,
    samples: list[dict[str, Any]],
    index: int,
    run_id: str,
    *,
    peer_hot_wallet_address: str = "",
) -> None:
    _append_request(samples, client, "GET", "/api/points/wallet", expected={200}, label="points wallet")
    onboarding = _append_request(samples, client, "GET", "/api/points/wallet/onboarding", expected={200}, label="points wallet onboarding")
    onboarding_body = (onboarding.get("body") or {}).get("onboarding") or {}
    wallet = onboarding_body.get("wallet") or {}
    if not wallet:
        created = _append_request(
            samples,
            client,
            "POST",
            "/api/points/wallet/onboarding",
            json_body={"mode": "official_hot"},
            expected={200, 400, 403, 409, 422},
            label="points wallet create official hot",
        )
        onboarding_body = (created.get("body") or {}).get("onboarding") or onboarding_body
        wallet = onboarding_body.get("wallet") or (created.get("body") or {}).get("wallet_identity") or {}
    source_address = str(wallet.get("address") or "").strip()
    system_wallets = onboarding_body.get("system_wallets") or []
    burn_address = ""
    for row in system_wallets:
        if isinstance(row, dict) and row.get("wallet_type") == "burn":
            burn_address = str(row.get("address") or "").strip()
            break
    _append_request(samples, client, "GET", "/api/points/ledger?limit=10", expected={200}, label="points ledger")
    _append_request(
        samples,
        client,
        "GET",
        "/api/points/explorer/search?q=missing-capacity-probe-tx&limit=5",
        expected={200, 400, 404},
        label="points explorer missing tx",
    )
    transfer_destination = str(peer_hot_wallet_address or burn_address).strip()
    if source_address and transfer_destination:
        _append_request(
            samples,
            client,
            "POST",
            "/api/points/transactions/submit",
            json_body={
                "source_wallet_address": source_address,
                "destination_wallet_address": transfer_destination,
                "amount_points": 1,
                "fee_points": 1,
                "request_uuid": f"{run_id}-{client.username}-transfer",
                "memo": f"capacity transfer {run_id} {index}",
            },
            expected={200, 202},
            acceptable_business_errors={
                403: ("temporarily frozen pending governance review",),
            },
            label="points transfer normal",
        )
        _append_request(
            samples,
            client,
            "POST",
            "/api/points/transactions/submit",
            json_body={
                "source_wallet_address": source_address,
                "destination_wallet_address": transfer_destination,
                "amount_points": 999999999,
                "fee_points": 0,
                "request_uuid": f"{run_id}-{client.username}-insufficient",
                "memo": "capacity insufficient balance",
            },
            expected={400, 402, 403, 409, 422},
            label="points transfer insufficient rejected",
        )
    ref = _first_ledger_ref(samples)
    if ref:
        _append_request(
            samples,
            client,
            "GET",
            f"/api/points/explorer/tx/{ref}",
            expected={200, 404},
            label="points explorer tx",
        )
    proposals = _append_request(
        samples,
        client,
        "GET",
        "/api/points/governance/proposals?limit=10",
        expected={200},
        label="points governance proposals",
    )
    proposal_uuid = _first_proposal_uuid(proposals)
    created_proposal = _append_request(
        samples,
        client,
        "POST",
        "/api/points/governance/public-proposal",
        json_body={
            "action_type": "AUTO_BURN_POLICY",
            "title": f"Capacity governance {run_id} {index}",
            "reason": "capacity probe normal governance load",
            "description": "capacity probe exercises public proposal creation",
            "impact_scope": "capacity_probe",
            "risk_summary": "temporary isolated predeploy load",
            "evidence": [f"capacity:{run_id}:{client.username}"],
            "payload": {"probe": True, "run_id": run_id, "index": index},
        },
        expected={200, 400, 403, 409, 422},
        label="points governance public proposal",
    )
    proposal_uuid = _first_proposal_uuid(created_proposal) or proposal_uuid
    if proposal_uuid:
        _append_request(
            samples,
            client,
            "POST",
            f"/api/points/governance/proposals/{proposal_uuid}/vote",
            json_body={"vote": "yes", "reason": "capacity probe normal vote"},
            expected={200, 400, 403, 409, 422},
            label="points governance vote",
        )
    _append_request(
        samples,
        client,
        "GET",
        "/api/points/transactions/disputes?limit=10",
        expected={200},
        label="points transaction disputes",
    )
    if ref and source_address and transfer_destination:
        _append_request(
            samples,
            client,
            "POST",
            "/api/points/transactions/disputes",
            json_body={
                "tx_hash": ref,
                "from_wallet_address": source_address,
                "to_wallet_address": transfer_destination,
                "claimed_amount_points": 1,
                "loss_cause": "capacity_probe",
                "statement": "capacity probe transaction dispute statement",
                "evidence": [f"capacity:{run_id}:{index}"],
                "account_bound_proof": True,
            },
            expected={200, 400, 403, 409, 422},
            label="points transaction dispute create",
        )


def _trading_member_flow(client: Client, samples: list[dict[str, Any]], index: int, run_id: str) -> None:
    for path, label in (
        ("/api/trading/markets", "trading markets"),
        ("/api/trading/asset-overview", "trading asset overview"),
        ("/api/trading/live-price?market=XRP/USDT", "trading live price"),
        ("/api/trading/bot-competition", "trading bot competition"),
    ):
        samples.append(client.request("GET", path, expected={200}, label=label))
    samples.append(client.request(
        "POST",
        "/api/trading/orders",
        json_body={
            "market_symbol": "XRP/USDT",
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
            "stop_loss_percent": 20,
            "take_profit_percent": 25,
        },
        expected={200, 400, 403, 409, 422},
        label="trading spot market buy",
    ))
    samples.append(client.request(
        "POST",
        "/api/trading/orders",
        json_body={
            "market_symbol": "XRP/USDT",
            "side": "buy",
            "order_type": "limit",
            "quantity": "1",
            "limit_price_points": "1",
        },
        expected={200, 400, 403, 409, 422},
        label="trading spot limit buy",
    ))
    samples.append(client.request("GET", "/api/trading/bots", expected={200}, label="trading bots"))
    samples.append(client.request(
        "POST",
        "/api/trading/bots",
        json_body={
            "bot_type": "dca",
            "name": f"Capacity DCA {run_id} {index}",
            "market_symbol": "XRP/USDT",
            "budget_points": 10,
            "interval_hours": 1,
            "enabled": True,
            "max_runs": 2,
            "stop_loss_percent": 20,
            "take_profit_percent": 25,
        },
        expected={200, 400, 403, 409, 422},
        label="trading dca bot create",
    ))
    samples.append(client.request(
        "POST",
        "/api/trading/bots",
        json_body={
            "bot_type": "conditional",
            "name": f"Capacity Workflow {run_id} {index}",
            "market_symbol": "XRP/USDT",
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
            "trigger_type": "price_below",
            "trigger_price_points": "999999",
            "enabled": True,
            "max_runs": 1,
            "cooldown_seconds": 0,
        },
        expected={200, 400, 403, 409, 422},
        label="trading workflow bot create",
    ))
    samples.append(client.request(
        "POST",
        "/api/trading/bots/scan",
        json_body={"limit": 20},
        expected={200, 400, 403, 409, 422},
        label="trading bots scan",
    ))
    samples.append(client.request(
        "POST",
        "/api/trading/grid/preview",
        json_body={
            "market_symbol": "XRP/USDT",
            "lower_price_points": 1,
            "upper_price_points": 2,
            "grid_count": 2,
            "order_amount_points": 3,
            "order_mode": "maker",
        },
        expected={200},
        label="trading grid preview",
    ))
    samples.append(client.request(
        "POST",
        "/api/trading/grid-bots",
        json_body={
            "name": f"Capacity Grid {run_id} {index}",
            "market_symbol": "XRP/USDT",
            "lower_price_points": 1,
            "upper_price_points": 2,
            "grid_count": 2,
            "order_amount_points": 3,
            "confirm_thin_profit": True,
            "share_parameters": True,
            "stop_loss_percent": 50,
            "take_profit_percent": 80,
        },
        expected={200, 400, 403, 409, 422},
        label="trading grid bot create",
    ))
    samples.append(client.request(
        "POST",
        "/api/trading/grid-bots/scan",
        json_body={},
        expected={200, 400, 403, 409, 422},
        label="trading grid bots scan",
    ))
    margin_positions: list[str] = []
    for position_type, label in (("margin_long", "trading margin long open"), ("short", "trading margin short open")):
        result = client.request(
            "POST",
            "/api/trading/margin/open",
            json_body={
                "market_symbol": "XRP/USDT",
                "position_type": position_type,
                "quantity": "10",
                "collateral_points": 20,
                "stop_loss_percent": 30,
                "take_profit_percent": 40,
                "idempotency_key": f"{run_id}-{client.username}-{position_type}",
            },
            expected={200, 400, 403, 409, 422},
            label=label,
        )
        samples.append(result)
        position_uuid = _first_position_uuid(result)
        if result.get("status") == 200 and position_uuid:
            margin_positions.append(position_uuid)
    for position_uuid in margin_positions[:2]:
        samples.append(client.request(
            "POST",
            f"/api/trading/margin/{position_uuid}/close",
            json_body={},
            expected={200, 400, 403, 409, 422},
            label="trading margin close",
        ))


def _appeals_flow(client: Client, samples: list[dict[str, Any]]) -> None:
    samples.append(client.request("GET", "/api/appeals", expected={200}, label="appeals list"))
    samples.append(client.request(
        "POST",
        "/api/appeals",
        json_body={"reason": "capacity probe regular appeal check"},
        expected={200, 400, 403, 409, 422},
        label="appeals submit none rejected",
    ))


def _malicious_flow(client: Client, samples: list[dict[str, Any]]) -> None:
    samples.append(client.request(
        "POST",
        "/api/trading/orders",
        json_body={"market_symbol": "BAD/POINTS", "side": "buy", "order_type": "limit", "quantity": "1", "limit_price_points": "1"},
        expected={400, 422},
        label="trading invalid market rejected",
    ))
    samples.append(client.request(
        "POST",
        "/api/trading/bots",
        json_body={"bot_type": "evil", "market_symbol": "XRP/USDT", "side": "hack", "order_type": "boom"},
        expected={400, 403, 422},
        label="trading malformed bot rejected",
    ))
    samples.append(client.request(
        "POST",
        "/api/trading/margin/open",
        json_body={
            "market_symbol": "XRP/USDT",
            "position_type": "short",
            "quantity": "-999",
            "collateral_points": -1,
            "idempotency_key": "bad",
        },
        expected={400, 403, 409, 422},
        label="trading invalid margin rejected",
    ))
    samples.append(client.request(
        "POST",
        "/api/points/governance/proposals/not-a-real-proposal/vote",
        json_body={"vote": "deface", "reason": "<script>alert(1)</script>"},
        expected={400, 403, 404, 409, 422},
        label="points governance bad vote rejected",
    ))
    samples.append(client.request(
        "POST",
        "/api/points/transactions/disputes",
        json_body={"tx_hash": "", "statement": "short"},
        expected={400, 403, 409, 422},
        label="points dispute malformed rejected",
    ))
    samples.append(client.request(
        "POST",
        "/api/appeals",
        json_body={"reason": "x" * 500},
        expected={400, 403, 409, 422},
        label="appeal oversized rejected",
    ))
    samples.append(client.request(
        "GET",
        "/api/cloud-drive/files?user_id=999999999",
        expected={400, 403},
        label="drive forbidden other user rejected",
    ))
    samples.append(client.request(
        "GET",
        "/api/cloud-drive/files/not-a-real-file/download",
        expected={400, 403, 404},
        label="drive missing download rejected",
    ))
    samples.append(client.request(
        "POST",
        "/api/storage/albums",
        json_body={"title": "bad csrf should fail"},
        headers={"X-CSRF-Token": "wrong-token"},
        expected={400, 403},
        label="csrf wrong token rejected",
    ))


def _heavy_flow(
    client: Client,
    run_root: Path,
    index: int,
    run_id: str,
    storage_id: str,
    file_id: str,
    samples: list[dict[str, Any]],
    args: argparse.Namespace,
) -> None:
    repeat = max(1, min(int(args.heavy_repeat or 1), 20))
    if file_id:
        for _ in range(repeat):
            samples.append(client.request(
                "GET",
                f"/api/cloud-drive/files/{file_id}/preview",
                expected={200, 400, 403, 404},
                label="drive repeated preview",
            ))
            samples.append(client.request(
                "GET",
                f"/api/cloud-drive/files/{file_id}/download?confirm_high_risk=1",
                expected={200, 400, 403, 404, 409},
                label="drive repeated download",
            ))
        samples.append(client.request(
            "PUT",
            f"/api/cloud-drive/files/{file_id}/text",
            json_body={"content": ("capacity heavy edit\n" * 256)[: max(1024, min(int(args.heavy_upload_bytes or 0), 64 * 1024))]},
            expected={200, 400, 403, 404, 415},
            label="drive text update heavy",
        ))
    if storage_id:
        for _ in range(repeat):
            samples.append(client.request(
                "GET",
                f"/api/storage/files/{storage_id}/download",
                expected={200, 400, 403, 404, 409},
                label="drive repeated download",
            ))
    upload_size = max(4096, min(int(args.heavy_upload_bytes or 0), 256 * 1024))
    chunk_size = max(2048, upload_size // 2)
    start = client.request(
        "POST",
        "/api/cloud-drive/resumable-upload/start",
        json_body={
            "filename": f"capacity-heavy-{index}.txt",
            "total_bytes": upload_size,
            "chunk_size": chunk_size,
            "target": "storage",
            "privacy_mode": "standard_plain",
            "virtual_path": f"/capacity/{run_id}/{index}/heavy.txt",
            "display_name": f"{client.username}-heavy.txt",
            "mime_type": "text/plain",
        },
        expected={200, 400, 403, 409, 422},
        label="resumable upload start",
    )
    samples.append(start)
    session_id = _first_file_text(start.get("body") or {}, "session", "session_id")
    if session_id:
        payload = _write_heavy_fixture(run_root, client.username, run_id, upload_size).read_bytes()
        chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]
        for chunk_index, chunk in enumerate(chunks):
            samples.append(client.request(
                "POST",
                f"/api/cloud-drive/resumable-upload/{session_id}/chunks/{chunk_index}",
                files={"chunk": (f"chunk-{chunk_index}.part", io.BytesIO(chunk), "application/octet-stream")},
                expected={200, 400, 403, 404, 409, 422},
                label="resumable upload chunk",
            ))
        samples.append(client.request(
            "POST",
            f"/api/cloud-drive/resumable-upload/{session_id}/complete",
            json_body={},
            expected={200, 400, 403, 404, 409, 422},
            label="resumable upload complete",
        ))
    candles = _synthetic_candles(96, base_price=3.0)
    samples.append(client.request(
        "POST",
        "/api/trading/bots/backtest",
        json_body={
            "strategy": "dca",
            "market_symbol": "XRP/USDT",
            "candles": candles,
            "initial_cash_points": 5000,
            "order_points": 50,
            "interval_candles": 6,
            "stop_loss_percent": 20,
            "take_profit_percent": 25,
        },
        expected={200, 400, 403, 409, 422},
        label="trading backtest dca",
    ))
    samples.append(client.request(
        "POST",
        "/api/trading/bots/backtest",
        json_body={
            "strategy": "workflow",
            "market_symbol": "XRP/USDT",
            "candles": candles,
            "initial_cash_points": 5000,
            "workflow": {
                "strategy_kind": "workflow",
                "branches": [{
                    "id": "heavy_branch",
                    "name": "heavy branch",
                    "priority": 10,
                    "logic": "AND",
                    "conditions": [{"metric": "price", "operator": "lt", "value": 4}],
                    "actions": [{"type": "buy_amount", "amount_points": 25, "order_type": "market"}],
                }],
            },
        },
        expected={200, 400, 403, 409, 422},
        label="trading backtest workflow",
    ))
    samples.append(client.request("GET", "/api/trading/history/export.csv", expected={200}, label="trading history export"))
    samples.append(client.request(
        "POST",
        "/api/storage/albums/smart-organize",
        json_body={"strategy": "folder", "visibility": "private"},
        expected={200, 400, 403, 409, 422},
        label="storage albums smart organize",
    ))


def exercise_root_points_chain(base_url: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    client = Client(
        base_url,
        "root",
        args.root_password,
        timeout=args.request_timeout,
        close_connections=bool(args.close_connections),
    )
    samples: list[dict[str, Any]] = [client.login()]
    if not samples[-1].get("ok"):
        return samples
    _append_request(samples, client, "GET", "/api/root/points/report", expected={200, 202}, label="root points report")
    _append_request(samples, client, "GET", "/api/root/points/chain/verify", expected={200, 202}, label="root points chain verify")
    _append_request(samples, client, "POST", "/api/root/points/chain/seal", json_body={"limit": 50}, expected={200, 202}, label="root points chain seal")
    _append_request(samples, client, "GET", "/api/root/points/chain/recovery", expected={200}, label="root points recovery status")
    _append_request(
        samples,
        client,
        "POST",
        "/api/root/points/chain/recovery/auto-handle",
        json_body={"confirm": "AUTO HANDLE POINTSCHAIN"},
        expected={200, 202, 409},
        label="root points emergency auto-handle",
    )
    _append_request(
        samples,
        client,
        "POST",
        "/api/root/points/governance/recovery-branch",
        json_body={
            "incident_tx_hash": "capacity-probe-missing-incident",
            "reason": "capacity probe recovery branch governance path",
            "excluded_tx_hashes": ["capacity-probe-missing-incident"],
            "recovery_strategy": "treasury_compensation",
            "loss_cause": "protocol_fault",
            "victim_statement": "capacity probe branch proposal",
            "victim_evidence_refs": ["capacity-probe"],
            "reference": "capacity_probe",
        },
        expected={200, 400, 403, 409, 422},
        label="root points recovery branch proposal",
    )
    _append_request(samples, client, "GET", "/api/root/trading/background/status?limit=20", expected={200}, label="root trading background status")
    for job_key in (
        "order_matching",
        "take_profit_stop_loss_scan",
        "bot_trigger_scan",
        "margin_liquidation_scan",
        "sitewide_metrics_refresh",
    ):
        _append_request(
            samples,
            client,
            "POST",
            "/api/root/trading/background/run-once",
            json_body={"job_key": job_key, "confirm": "RUN_TRADING_JOB_ONCE"},
            expected={200, 202, 400, 409, 422},
            label=f"root trading background enqueue {job_key}",
        )
    _append_request(samples, client, "POST", "/api/root/trading/orders/match", json_body={"limit": 50}, expected={200}, label="root trading orders match")
    _append_request(samples, client, "POST", "/api/root/trading/liquidations/scan", json_body={"limit": 50}, expected={200}, label="root trading liquidation scan")
    _append_request(samples, client, "POST", "/api/root/trading/sitewide/refresh", json_body={}, expected={200, 202}, label="root trading snapshots refresh")
    _append_request(samples, client, "GET", "/api/root/trading/verify", expected={200, 202}, label="root trading verify")
    _append_request(samples, client, "GET", "/api/admin/trading/report", expected={200, 404}, label="root trading report")
    _append_request(samples, client, "GET", "/api/root/trading/sitewide/pools", expected={200, 404}, label="root trading pools snapshot")
    _append_request(samples, client, "GET", "/api/admin/snapshots", expected={200, 403, 503}, label="root snapshots list")
    _append_request(samples, client, "GET", "/api/admin/snapshots/daily", expected={200, 403, 503}, label="root daily snapshot status")
    if load_includes(args, "heavy"):
        _append_request(
            samples,
            client,
            "POST",
            "/api/root/points/chain/backups",
            json_body={},
            expected={400, 410, 503},
            label="root points chain backup disabled",
        )
        _append_request(
            samples,
            client,
            "POST",
            "/api/admin/snapshots",
            json_body={"type": "manual", "notes": "capacity probe heavy snapshot"},
            expected={200, 400, 403, 500, 503},
            label="root manual snapshot create",
        )
    return samples


def exercise_user(
    base_url: str,
    username: str,
    index: int,
    run_root: Path,
    run_id: str,
    args: argparse.Namespace,
    progress: "RoundProgress | None" = None,
    peer_hot_wallet_address: str = "",
) -> list[dict[str, Any]]:
    if progress is not None:
        progress.start_user(username, index)
    client = Client(
        base_url,
        username,
        USER_PASSWORD,
        timeout=args.request_timeout,
        close_connections=bool(args.close_connections),
        progress=progress,
    )
    samples: list[dict[str, Any]] = []
    kinds = active_load_kinds(args)
    try:
        samples.append(client.login())
        if not samples[-1].get("ok"):
            return samples

        if kinds == {"light"}:
            for path, label in (
                ("/api/me?optional=1", "me"),
                ("/api/jobs", "jobs"),
                ("/api/shares?limit=20", "shares"),
                ("/api/trading/dashboard", "trading dashboard"),
            ):
                samples.append(client.request("GET", path, expected={200}, label=label))
            return samples

        if kinds == {"basic"}:
            for path, label in (
                ("/api/me?optional=1", "me"),
                ("/api/jobs", "jobs"),
                ("/api/shares?limit=20", "shares"),
                ("/api/trading/dashboard", "trading dashboard"),
                ("/api/trading/markets", "trading markets"),
                ("/api/games/catalog", "games catalog"),
            ):
                samples.append(client.request("GET", path, expected={200}, label=label))
            storage_id, file_id = upload_text_file(client, run_root, index, run_id, samples)
            if file_id:
                samples.append(client.request(
                    "GET",
                    f"/api/cloud-drive/files/{file_id}/preview",
                    expected={200, 400, 403, 404},
                    label="drive preview",
                ))
            return samples

        if "normal" in kinds:
            _points_chain_flow(client, samples, index, run_id, peer_hot_wallet_address=peer_hot_wallet_address)
    finally:
        if progress is not None and samples and not samples[-1].get("ok"):
            progress.finish_user(username)
    if not samples[-1].get("ok"):
        return samples

    storage_id = ""
    file_id = ""
    if "normal" in kinds:
        for path, label in (
            ("/api/me?optional=1", "me"),
            ("/api/jobs", "jobs"),
            ("/api/shares?limit=20", "shares"),
            ("/api/trading/dashboard", "trading dashboard"),
            ("/api/trading/grid-bots", "grid bots"),
            ("/api/videos?limit=5", "videos"),
            ("/api/games/catalog", "games catalog"),
        ):
            samples.append(client.request("GET", path, expected={200}, label=label))
        _trading_member_flow(client, samples, index, run_id)
        _appeals_flow(client, samples)

        room_id = first_chat_room(client, samples)
        if room_id:
            samples.append(client.request(
                "POST",
                f"/api/chat/rooms/{room_id}/messages",
                json_body={"content": f"capacity normal {run_id} {username}"},
                expected={200, 400, 403, 429},
                label="chat normal message",
            ))
            if "malicious" in kinds:
                samples.append(client.request(
                    "POST",
                    f"/api/chat/rooms/{room_id}/messages",
                    json_body={"content": "' OR 1=1; DROP TABLE chat_messages; --"},
                    expected={200, 400, 403, 429},
                    label="chat malicious text rejected_or_sanitized",
                ))

        board_id = first_public_board(client, samples)
        if board_id:
            samples.append(client.request(
                "POST",
                f"/api/community/boards/{board_id}/threads",
                json_body={
                    "title": f"Capacity thread {run_id} {username}",
                    "content": "capacity thread body",
                    "post_type": "discussion",
                },
                expected={200, 400, 403, 409, 429},
                label="community create thread",
            ))
            if "malicious" in kinds:
                samples.append(client.request(
                    "POST",
                    f"/api/community/boards/{board_id}/threads",
                    json_body={"title": "<img src=x onerror=alert(1)>", "content": "xss probe", "post_type": "discussion"},
                    expected={200, 400, 403, 409, 429},
                    label="community xss title",
                ))

    if "normal" in kinds or "heavy" in kinds:
        storage_id, file_id = upload_text_file(client, run_root, index, run_id, samples)
        if file_id and "normal" in kinds:
            samples.append(client.request(
                "GET",
                f"/api/cloud-drive/files/{file_id}/preview",
                expected={200, 400, 403, 404},
                label="drive preview",
            ))
        if storage_id and "normal" in kinds:
            samples.append(client.request(
                "POST",
                "/api/storage/share-links",
                json_body={"storage_file_id": storage_id, "can_preview": True, "max_views": 2},
                expected={200},
                label="drive share link",
            ))
            album = client.request(
                "POST",
                "/api/storage/albums",
                json_body={"title": f"Capacity Album {run_id} {username}", "visibility": "private"},
                expected={200},
                label="album create private",
            )
            samples.append(album)
            album_id = ((album.get("body") or {}).get("album") or {}).get("id")
            if album_id:
                samples.append(client.request(
                    "POST",
                    f"/api/storage/albums/{album_id}/files",
                    json_body={"storage_file_id": storage_id, "caption": "capacity caption"},
                    expected={200, 400},
                    label="album add file",
                ))
                samples.append(client.request(
                    "PUT",
                    f"/api/storage/albums/{album_id}",
                    json_body={"visibility": "unlisted"},
                    expected={200},
                    label="album enable share",
                ))

    if "normal" in kinds:
        samples.append(client.request(
            "POST",
            "/api/games/tetris/solo-scores",
            json_body={
                "score": 1000 + index,
                "raw_elapsed_ms": 60000,
                "penalty_seconds": 0,
                "elapsed_ms": 60000,
                "puzzle_id": f"capacity-{run_id}",
            },
            expected={200},
            label="game tetris score",
        ))
    if "malicious" in kinds:
        if "normal" not in kinds:
            room_id = first_chat_room(client, samples)
            if room_id:
                samples.append(client.request(
                    "POST",
                    f"/api/chat/rooms/{room_id}/messages",
                    json_body={"content": "' OR 1=1; DROP TABLE chat_messages; --"},
                    expected={200, 400, 403, 429},
                    label="chat malicious text rejected_or_sanitized",
                ))
            board_id = first_public_board(client, samples)
            if board_id:
                samples.append(client.request(
                    "POST",
                    f"/api/community/boards/{board_id}/threads",
                    json_body={"title": "<img src=x onerror=alert(1)>", "content": "xss probe", "post_type": "discussion"},
                    expected={200, 400, 403, 409, 429},
                    label="community xss title",
                ))
        samples.append(client.request(
            "POST",
            "/api/games/tetris/solo-scores",
            json_body={"score": -1, "raw_elapsed_ms": 0, "penalty_seconds": 0, "elapsed_ms": 0},
            expected={400},
            label="game invalid score rejected",
        ))
        _malicious_flow(client, samples)
    if "heavy" in kinds:
        _heavy_flow(client, run_root, index, run_id, storage_id, file_id, samples, args)
    if progress is not None:
        progress.finish_user(username)
    return samples


def summarize_samples(samples: list[dict[str, Any]], elapsed_seconds: float) -> dict[str, Any]:
    hard_failures: list[dict[str, Any]] = []
    app_limits: list[dict[str, Any]] = []
    server_failures: list[dict[str, Any]] = []
    unexpected_failures: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    by_label: dict[str, list[int]] = {}
    by_label_status: dict[str, dict[str, int]] = {}
    latencies: list[int] = []
    for sample in samples:
        status = int(sample.get("status") or 0)
        label = str(sample.get("label") or "unknown")
        status_counts[str(status)] = status_counts.get(str(status), 0) + 1
        by_label.setdefault(label, []).append(int(sample.get("elapsed_ms") or 0))
        label_counts = by_label_status.setdefault(label, {})
        label_counts[str(status)] = label_counts.get(str(status), 0) + 1
        latencies.append(int(sample.get("elapsed_ms") or 0))
        body = sample.get("body") or {}
        sample_ok = bool(sample.get("ok"))
        server_busy = status == 503 and (body.get("code") == "server_busy" or body.get("error") == "server_busy")
        app_limited = status == 429
        server_failed = (not sample_ok) and (status == 0 or server_busy or status >= 500)
        unexpected_failed = (not sample_ok) and not app_limited and not server_failed
        if app_limited:
            app_limits.append(sample)
        if server_failed:
            server_failures.append(sample)
        if unexpected_failed:
            unexpected_failures.append(sample)
        if server_failed or unexpected_failed:
            hard_failures.append(sample)
    app_limit_count = len(app_limits)
    hard_failure_count = len(hard_failures)
    return {
        "ok": not hard_failures and not app_limits,
        "server_stable": not server_failures,
        "application_limited": bool(app_limits),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "sample_count": len(samples),
        "hard_failure_count": hard_failure_count,
        "server_failure_count": len(server_failures),
        "unexpected_failure_count": len(unexpected_failures),
        "app_limit_count": app_limit_count,
        "status_counts": status_counts,
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p90": percentile(latencies, 90),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies) if latencies else 0,
        },
        "by_label_latency_ms": {
            label: {
                "count": len(values),
                "p50": percentile(values, 50),
                "p95": percentile(values, 95),
                "p99": percentile(values, 99),
                "max": max(values) if values else 0,
                "statuses": by_label_status.get(label) or {},
            }
            for label, values in sorted(by_label.items())
        },
        "hard_failures": hard_failures[:12],
        "server_failures": server_failures[:12],
        "unexpected_failures": unexpected_failures[:12],
        "app_limits": app_limits[:12],
    }


def run_load_round(instance: ServerInstance, usernames: list[str], account_count: int, args: argparse.Namespace) -> dict[str, Any]:
    run_id = f"{utc_stamp()}_{instance.profile.label}_{account_count}_{''.join(random.choice(string.ascii_lowercase) for _ in range(4))}"
    selected = usernames[:account_count]
    peer_targets: dict[str, str] = {}
    if load_includes(args, "normal"):
        try:
            sink_username = capacity_transfer_sink_username(run_id)
            addresses = collect_official_hot_wallet_addresses(
                instance.base_url,
                args,
                [*selected, sink_username],
                ensure_missing=True,
            )
            sink_address = addresses.get(sink_username, "")
            if sink_address:
                peer_targets = {username: sink_address for username in selected}
            else:
                peer_targets = peer_hot_wallet_targets(selected, addresses)
        except Exception as exc:
            print(f"[capacity] warning: could not preload pc0 peer wallet addresses: {exc}", flush=True)
    samples: list[dict[str, Any]] = []
    started = time.perf_counter()
    with CpuSampler(instance.master_pid, interval=args.cpu_sample_interval) as sampler:
        progress = RoundProgress(
            profile_label=instance.profile.label,
            account_count=account_count,
            sampler=sampler,
            args=args,
        )
        progress.start()
        try:
            with ThreadPoolExecutor(max_workers=account_count + 1) as pool:
                futures = [
                    pool.submit(
                        exercise_user,
                        instance.base_url,
                        username,
                        idx,
                        instance.run_root,
                        run_id,
                        args,
                        progress,
                        peer_targets.get(username, ""),
                    )
                    for idx, username in enumerate(selected)
                ]
                if not active_load_kinds(args).issubset({"light", "basic"}):
                    futures.append(pool.submit(exercise_root_points_chain, instance.base_url, args))
                completed = 0
                for future in as_completed(futures):
                    samples.extend(future.result())
                    completed += 1
                    if completed <= account_count and (completed == account_count or completed % max(1, min(10, account_count // 4 or 1)) == 0):
                        print(f"[capacity] completed accounts: {completed}/{account_count}", flush=True)
        finally:
            progress.stop()
        cpu_summary = sampler.summary()
    summary = summarize_samples(samples, time.perf_counter() - started)
    summary["cpu"] = cpu_summary
    return summary


def tail_log(path: Path, max_lines: int = 80) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    return lines[-max_lines:]


def profile_failed(round_summary: dict[str, Any], args: argparse.Namespace) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    latency = round_summary.get("latency_ms") or {}
    server_failures = round_summary.get("server_failures") or []
    if int(round_summary.get("hard_failure_count") or 0) > 0:
        reasons.append("hard_failures")
    if int(round_summary.get("app_limit_count") or 0) > 0 and not args.continue_after_app_limit:
        reasons.append("application_limit")
    if any(int(sample.get("status") or 0) == 0 for sample in server_failures):
        reasons.append("connection_errors")
    if any(
        int(sample.get("status") or 0) == 503
        and ((sample.get("body") or {}).get("code") == "server_busy" or (sample.get("body") or {}).get("error") == "server_busy")
        for sample in server_failures
    ):
        reasons.append("server_busy")
    if any(int(sample.get("status") or 0) >= 500 for sample in server_failures):
        reasons.append("hard_5xx")
    if int(latency.get("p95") or 0) >= args.hard_p95_ms:
        reasons.append("p95_latency")
    if int(latency.get("max") or 0) >= args.hard_max_ms:
        reasons.append("max_latency")
    return bool(reasons), reasons


def round_experience_degraded(probe: dict[str, Any], args: argparse.Namespace) -> bool:
    latency = probe.get("latency_ms") or {}
    return (
        int(latency.get("p95") or 0) >= int(args.ux_p95_ms)
        or int(latency.get("p99") or 0) >= int(args.ux_p99_ms)
    )


def ux_confirm_rounds(args: argparse.Namespace) -> int:
    return max(0, int(getattr(args, "ux_confirm_rounds", 3) or 0))


def round_experience_confirmed(round_result: dict[str, Any], args: argparse.Namespace) -> bool:
    probe = round_result.get("probe") or {}
    if not round_experience_degraded(probe, args):
        return False
    required = ux_confirm_rounds(args)
    if required <= 0:
        return True
    confirmations = round_result.get("ux_confirmation_rounds") or []
    if len(confirmations) < required:
        return False
    return all(
        round_experience_degraded((confirmation.get("probe") or confirmation), args)
        for confirmation in confirmations[:required]
    )


def build_ux_confirmation_summary(round_result: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    confirmations = round_result.get("ux_confirmation_rounds") or []
    required = ux_confirm_rounds(args)
    degraded = [
        confirmation
        for confirmation in confirmations
        if round_experience_degraded((confirmation.get("probe") or confirmation), args)
    ]
    return {
        "required": required,
        "attempted": len(confirmations),
        "degraded_count": len(degraded),
        "confirmed": round_experience_confirmed(round_result, args),
    }


def round_server_unstable(probe: dict[str, Any], args: argparse.Namespace) -> bool:
    latency = probe.get("latency_ms") or {}
    return (
        int(probe.get("server_failure_count") or 0) > 0
        or int(probe.get("unexpected_failure_count") or 0) > 0
        or int(latency.get("p95") or 0) >= int(args.hard_p95_ms)
        or int(latency.get("max") or 0) >= int(args.hard_max_ms)
    )


def round_snapshot(profile: dict[str, Any], round_result: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        "profile": profile,
        "accounts": int(round_result.get("accounts") or 0),
        "latency_ms": probe.get("latency_ms") or {},
        "status_counts": probe.get("status_counts") or {},
        "hard_failure_count": int(probe.get("hard_failure_count") or 0),
        "server_failure_count": int(probe.get("server_failure_count") or 0),
        "unexpected_failure_count": int(probe.get("unexpected_failure_count") or 0),
        "app_limit_count": int(probe.get("app_limit_count") or 0),
        "contaminated_after_app_limit": bool(round_result.get("contaminated_after_app_limit")),
        "cpu": probe.get("cpu") or {},
    }
    if round_result.get("ux_confirmation_rounds"):
        snapshot["ux_confirmation"] = round_result.get("ux_confirmation") or {}
        snapshot["ux_confirmation_latencies"] = [
            (item.get("probe") or item).get("latency_ms") or {}
            for item in round_result.get("ux_confirmation_rounds") or []
        ]
    return snapshot


def build_limit_report(results: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    profile_reports: list[dict[str, Any]] = []
    best_ux_ok: dict[str, Any] | None = None
    first_ux_degraded: dict[str, Any] | None = None
    first_app_limit: dict[str, Any] | None = None
    first_server_unstable: dict[str, Any] | None = None

    for profile_result in results:
        profile = profile_result["profile"]
        profile_limit = {
            "profile": profile,
            "max_accounts_before_ux_degradation": None,
            "ux_degradation_at": None,
            "application_limit_at": None,
            "server_instability_at": None,
        }
        for round_result in profile_result.get("rounds") or []:
            probe = round_result.get("probe") or {}
            snapshot = round_snapshot(profile, round_result, probe)
            app_limited = int(probe.get("app_limit_count") or 0) > 0
            unstable = round_server_unstable(probe, args)
            degraded = round_experience_confirmed(round_result, args)
            ux_ok = not degraded and not app_limited and not unstable
            if ux_ok:
                profile_limit["max_accounts_before_ux_degradation"] = snapshot
                if best_ux_ok is None or snapshot["accounts"] > int(best_ux_ok.get("accounts") or 0):
                    best_ux_ok = snapshot
            elif degraded and profile_limit["ux_degradation_at"] is None:
                profile_limit["ux_degradation_at"] = snapshot
                if first_ux_degraded is None or snapshot["accounts"] < int(first_ux_degraded.get("accounts") or 10**9):
                    first_ux_degraded = snapshot
            if app_limited and profile_limit["application_limit_at"] is None:
                profile_limit["application_limit_at"] = snapshot
                if first_app_limit is None or snapshot["accounts"] < int(first_app_limit.get("accounts") or 10**9):
                    first_app_limit = snapshot
            if unstable and profile_limit["server_instability_at"] is None:
                profile_limit["server_instability_at"] = snapshot
                if first_server_unstable is None or snapshot["accounts"] < int(first_server_unstable.get("accounts") or 10**9):
                    first_server_unstable = snapshot
        profile_reports.append(profile_limit)

    server_status = "observed" if first_server_unstable else "not_reached"
    server_note = ""
    if not first_server_unstable and first_app_limit:
        server_status = "not_reached_before_application_limit"
        server_note = "Application/rate limits were reached before server instability; use --continue-after-app-limit or adjust the account ladder to probe beyond that with caution."
    return {
        "thresholds": {
            "ux_p95_ms": int(args.ux_p95_ms),
            "ux_p99_ms": int(args.ux_p99_ms),
            "server_p95_ms": int(args.hard_p95_ms),
            "server_max_ms": int(args.hard_max_ms),
        },
        "experience": {
            "max_accounts_before_degradation": best_ux_ok,
            "degradation_starts_at": first_ux_degraded,
        },
        "application_limit": {
            "first_observed_at": first_app_limit,
        },
        "server_instability": {
            "status": server_status,
            "first_observed_at": first_server_unstable,
            "note": server_note,
        },
        "profiles": profile_reports,
    }


def recommend_hls_capacity_policy(profile: dict[str, Any], probe: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    workers = int(profile.get("workers") or 0)
    threads = int(profile.get("threads") or 0)
    lanes = workers * threads
    tier = str(getattr(args, "capacity_tier", "auto") or "auto").strip().lower()
    latency = probe.get("latency_ms") or {}
    cpu = probe.get("cpu") or {}
    try:
        p95 = float(latency.get("p95") or 0)
    except Exception:
        p95 = 0.0
    try:
        target_p95 = float(getattr(args, "target_p95_ms", 0) or 0)
    except Exception:
        target_p95 = 0.0
    try:
        cpu_peak = float(cpu.get("total_worker_cpu_peak_percent") or 0)
    except Exception:
        cpu_peak = 0.0
    latency_ratio = (p95 / target_p95) if target_p95 > 0 and p95 > 0 else 1.0

    reasons: list[str] = []
    max_concurrent = 1
    if tier in {"sbc", "legacy", "laptop"}:
        reasons.append(f"{tier}_tier_conservative")
    elif tier == "highend" and lanes >= 18 and latency_ratio <= 0.60 and (cpu_peak <= 300 or cpu_peak == 0):
        max_concurrent = 3
        reasons.append("highend_capacity_headroom")
    elif tier in {"midrange", "highend", "auto"} and lanes >= 12 and latency_ratio <= 0.65 and (cpu_peak <= 260 or cpu_peak == 0):
        max_concurrent = 2
        reasons.append("moderate_capacity_headroom")
    else:
        reasons.append("limited_latency_or_cpu_headroom")

    if max_concurrent == 1:
        reasons.append("protect_request_p95_p99_during_hls")
    return {
        "hls_max_concurrent": max_concurrent,
        "hls_serialize_all": 1,
        "basis": {
            "capacity_tier": tier,
            "worker_thread_lanes": lanes,
            "selected_round_p95_ms": p95,
            "target_p95_ms": target_p95,
            "p95_to_target_ratio": round(latency_ratio, 3),
            "worker_cpu_peak_percent": cpu_peak,
        },
        "reasons": reasons,
        "note": "Derived from request-capacity headroom; use scripts/testing/hls_premium_sizing_probe.py before raising this for production HLS workloads.",
    }


def choose_recommendation(results: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    for profile_result in results:
        profile = profile_result["profile"]
        for round_result in profile_result.get("rounds") or []:
            probe = round_result["probe"]
            if (
                not round_result.get("contaminated_after_app_limit")
                and
                int(probe.get("hard_failure_count") or 0) == 0
                and int(probe.get("app_limit_count") or 0) == 0
            ):
                fallback.append({"profile": profile, "round": round_result})
                if int((probe.get("latency_ms") or {}).get("p95") or 0) <= args.target_p95_ms:
                    candidates.append({"profile": profile, "round": round_result})
    pool = candidates or fallback
    if not pool:
        return {"ok": False, "msg": "no passing profile found"}
    best = max(
        pool,
        key=lambda item: (
            int(item["round"].get("accounts") or 0),
            int(item["profile"].get("workers") or 0) * int(item["profile"].get("threads") or 0),
            -int(((item["round"].get("probe") or {}).get("latency_ms") or {}).get("p95") or 0),
        ),
    )
    profile = best["profile"]
    probe = best["round"]["probe"]
    hls_capacity = recommend_hls_capacity_policy(profile, probe, args)
    return {
        "ok": True,
        "workers": profile["workers"],
        "threads": profile["threads"],
        "max_passing_accounts": best["round"]["accounts"],
        "target_p95_ms": args.target_p95_ms,
        "observed_latency_ms": probe.get("latency_ms") or {},
        "observed_status_counts": probe.get("status_counts") or {},
        "observed_cpu": probe.get("cpu") or {},
        "suggested_env": {
            "HACKME_DEV_GUNICORN_WORKERS": str(profile["workers"]),
            "HACKME_DEV_GUNICORN_THREADS": str(profile["threads"]),
            "HACKME_DEV_GUNICORN_MAX_REQUESTS": str(max(0, int(args.gunicorn_max_requests))),
            "HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER": str(max(0, int(args.gunicorn_max_requests_jitter))),
            "HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY": str(max(4, int(profile["threads"]))),
            "HACKME_MEDIA_HLS_MAX_CONCURRENT": str(hls_capacity["hls_max_concurrent"]),
            "HACKME_MEDIA_HLS_SERIALIZE_ALL": str(hls_capacity["hls_serialize_all"]),
        },
        "hls_capacity_policy": hls_capacity,
        "suggested_test_for_develop_args": [
            "--server-runner",
            "gunicorn",
            "--gunicorn-workers",
            str(profile["workers"]),
            "--gunicorn-threads",
            str(profile["threads"]),
            "--gunicorn-max-requests",
            str(max(0, int(args.gunicorn_max_requests))),
            "--gunicorn-max-requests-jitter",
            str(max(0, int(args.gunicorn_max_requests_jitter))),
        ],
    }


def _round_for_recommendation(results: list[dict[str, Any]], recommendation: dict[str, Any]) -> dict[str, Any] | None:
    if not recommendation.get("ok"):
        return None
    workers = int(recommendation.get("workers") or 0)
    threads = int(recommendation.get("threads") or 0)
    accounts = int(recommendation.get("max_passing_accounts") or 0)
    for profile_result in results:
        profile = profile_result.get("profile") or {}
        if int(profile.get("workers") or 0) != workers or int(profile.get("threads") or 0) != threads:
            continue
        for round_result in profile_result.get("rounds") or []:
            if int(round_result.get("accounts") or 0) == accounts:
                return round_result
    return None


def build_rc1_capacity_gate(
    results: list[dict[str, Any]],
    recommendation: dict[str, Any],
    limit_report: dict[str, Any],
) -> dict[str, Any]:
    selected_round = _round_for_recommendation(results, recommendation)
    selected_probe = (selected_round or {}).get("probe") or {}
    reasons: list[str] = []
    if not recommendation.get("ok"):
        reasons.append("no_recommendation")
    if recommendation.get("ok") and selected_round is None:
        reasons.append("selected_round_missing")
    if selected_round is not None:
        if bool(selected_round.get("contaminated_after_app_limit")):
            reasons.append("selected_round_contaminated_after_app_limit")
        if int(selected_probe.get("hard_failure_count") or 0) > 0:
            reasons.append("selected_round_hard_failures")
        if int(selected_probe.get("server_failure_count") or 0) > 0:
            reasons.append("selected_round_server_failures")
        if int(selected_probe.get("unexpected_failure_count") or 0) > 0:
            reasons.append("selected_round_unexpected_failures")
        if int(selected_probe.get("app_limit_count") or 0) > 0:
            reasons.append("selected_round_application_limit")

    profile_label = ""
    if recommendation.get("ok"):
        profile_label = f"{int(recommendation.get('workers') or 0)}x{int(recommendation.get('threads') or 0)}"
    return {
        "pass": not reasons,
        "reasons": reasons,
        "recommended_profile": profile_label,
        "max_safe_accounts": int(recommendation.get("max_passing_accounts") or 0) if recommendation.get("ok") else 0,
        "ux_degradation_at": (limit_report.get("experience") or {}).get("degradation_starts_at"),
        "server_instability_at": (limit_report.get("server_instability") or {}).get("first_observed_at"),
        "app_limit_at": (limit_report.get("application_limit") or {}).get("first_observed_at"),
        "selected_round_latency": selected_probe.get("latency_ms") or {},
        "selected_round_cpu": selected_probe.get("cpu") or {},
    }


def sync_capacity_defaults(
    recommendation: dict[str, Any],
    args: argparse.Namespace,
    *,
    output_path: Path,
    finished_at: str,
) -> dict[str, Any]:
    if args.no_sync_defaults:
        return {"ok": False, "skipped": True, "reason": "disabled_by_flag"}
    if active_load_kinds(args) != {"normal"}:
        return {"ok": False, "skipped": True, "reason": "non_normal_load_profile"}
    if args.close_connections:
        return {"ok": False, "skipped": True, "reason": "close_connections_probe"}
    if not recommendation.get("ok"):
        return {"ok": False, "skipped": True, "reason": "no_recommendation"}

    env_values = recommendation.get("suggested_env") or {}
    allowed_keys = [
        "HACKME_DEV_GUNICORN_WORKERS",
        "HACKME_DEV_GUNICORN_THREADS",
        "HACKME_DEV_GUNICORN_MAX_REQUESTS",
        "HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER",
        "HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY",
        "HACKME_MEDIA_HLS_MAX_CONCURRENT",
        "HACKME_MEDIA_HLS_SERIALIZE_ALL",
    ]
    path = Path(args.capacity_defaults_file).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated by scripts/testing/predeploy_capacity_probe.py",
        f"# Updated at: {finished_at}",
        f"# Source report: {output_path}",
        "# test_for_develop.sh reads this as local defaults only.",
        "# test_for_develop.sh auto settings use these values until --capacity-probe refreshes them.",
        "# Use --no-capacity-probe with HACKME_DEV_USE_CAPACITY_DEFAULTS=0 only for the hardware fallback.",
    ]
    for key in allowed_keys:
        value = str(env_values.get(key, "")).strip()
        if value:
            lines.append(f"{key}={value}")
    payload = "\n".join(lines) + "\n"
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)
    return {
        "ok": True,
        "path": str(path),
        "values": {key: str(env_values.get(key, "")) for key in allowed_keys if str(env_values.get(key, "")).strip()},
    }


def apply_capacity_tier(args: argparse.Namespace) -> str:
    tier = str(getattr(args, "capacity_tier", "auto") or "auto").strip().lower()
    if bool(getattr(args, "light", False)) and tier == "auto":
        tier = "legacy"
        args.capacity_tier = tier
    if tier == "auto":
        return "auto"
    preset = CAPACITY_TIER_PRESETS[tier]
    if not str(args.profiles or "").strip():
        args.profiles = str(preset["profiles"])
    if str(args.account_counts or "").strip().lower() in {"", "auto", "adaptive", "probe"}:
        args.account_counts = str(preset["account_counts"])
    if str(args.load_profile or "normal") == "normal" and not str(args.load_kinds or "").strip():
        args.load_profile = str(preset["load_profile"])
    if "start_accounts" in preset:
        args.start_accounts = min(int(args.start_accounts or preset["start_accounts"]), int(preset["start_accounts"]))
    args.max_rounds = min(int(args.max_rounds or preset["max_rounds"]), int(preset["max_rounds"]))
    args.max_accounts = min(int(args.max_accounts or preset["max_accounts"]), int(preset["max_accounts"]))
    args.ux_confirm_rounds = min(int(args.ux_confirm_rounds or 0), int(preset["ux_confirm_rounds"]))
    if "max_duration_seconds" in preset and float(args.max_duration_seconds or 0.0) <= 0:
        args.max_duration_seconds = float(preset["max_duration_seconds"])
    if "request_timeout" in preset:
        args.request_timeout = min(float(args.request_timeout or preset["request_timeout"]), float(preset["request_timeout"]))
    if str(args.load_profile) == "light" or str(args.load_kinds).strip().lower() == "light":
        args.heavy_repeat = 1
        args.heavy_upload_bytes = min(int(args.heavy_upload_bytes or 0), 4096)
    return tier


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    capacity_tier = apply_capacity_tier(args)
    load_kinds = sorted(active_load_kinds(args))
    profiles = parse_profiles(args.profiles) if args.profiles else default_profiles()
    account_ladder_label = AccountLadder(args).label()
    stamp = utc_stamp()
    top_root = Path(args.tmp_parent).resolve() / f"hackme_predeploy_capacity_{stamp}_{os.getpid()}"
    top_root.mkdir(parents=True, exist_ok=True)
    output_path = (
        Path(args.output).resolve()
        if args.output
        else Path(args.tmp_parent).resolve() / f"hackme_predeploy_capacity_report_{stamp}_{os.getpid()}.json"
    )

    results: list[dict[str, Any]] = []
    started_at = datetime.now(timezone.utc).isoformat()
    probe_started = time.monotonic()
    max_duration_seconds = max(0.0, float(args.max_duration_seconds or 0.0))
    stop_reason = ""
    print(f"[capacity] temp root: {top_root}", flush=True)
    print(f"[capacity] profiles: {', '.join(profile.label for profile in profiles)}", flush=True)
    print(
        f"[capacity] hardware tier: {capacity_tier}"
        + (f" ({CAPACITY_TIER_PRESETS[capacity_tier]['description']})" if capacity_tier in CAPACITY_TIER_PRESETS else ""),
        flush=True,
    )
    if capacity_tier == "highend":
        print(
            "[capacity] WARNING: highend has no account or round ceiling; it will keep increasing "
            "load until UX degradation, app limits, server instability, or hard failures stop it. "
            "Use only on machines where a temporary freeze/crash is acceptable.",
            flush=True,
        )
    if max_duration_seconds > 0:
        print(f"[capacity] time limit: {max_duration_seconds:g}s", flush=True)
    print(f"[capacity] account ladder: {account_ladder_label}", flush=True)
    print(f"[capacity] load profile: {args.load_profile} ({', '.join(load_kinds)})", flush=True)

    try:
        for index, profile in enumerate(profiles):
            if max_duration_seconds > 0 and time.monotonic() - probe_started >= max_duration_seconds:
                stop_reason = f"time_limit_{max_duration_seconds:g}s"
                print(f"[capacity] stopping before next profile: {stop_reason}", flush=True)
                break
            port = args.port + index if args.port else free_port()
            run_root = top_root / f"profile_{profile.label}"
            instance: ServerInstance | None = None
            profile_result: dict[str, Any] = {
                "profile": {"workers": profile.workers, "threads": profile.threads, "label": profile.label},
                "port": port,
                "rounds": [],
            }
            results.append(profile_result)
            try:
                print(f"[capacity] starting {profile.label} on port {port}", flush=True)
                instance = start_isolated_server(args, profile, run_root, port)
                profile_result["master_pid"] = instance.master_pid
                profile_result["log_path"] = str(instance.log_path)
                root = root_client(instance.base_url, args)
                prefix = f"cap_{int(time.time())}_{profile.workers}x{profile.threads}"
                usernames: list[str] = []
                app_limit_seen = False

                ladder = AccountLadder(args)
                while True:
                    if max_duration_seconds > 0 and time.monotonic() - probe_started >= max_duration_seconds:
                        stop_reason = f"time_limit_{max_duration_seconds:g}s"
                        profile_result["stopped_reason"] = stop_reason
                        print(f"[capacity] stopping before next round for {profile.label}: {stop_reason}", flush=True)
                        break
                    account_count = ladder.next()
                    if account_count is None:
                        break
                    ladder.ensure_usernames(root, usernames, prefix, account_count)
                    print(f"[capacity] {profile.label}: accounts={account_count}", flush=True)
                    print(f"[capacity] account sample: {format_account_sample(usernames[:account_count])}", flush=True)
                    probe = run_load_round(instance, usernames, account_count, args)
                    raw_ux_degraded = round_experience_degraded(probe, args)
                    ux_confirmation_rounds: list[dict[str, Any]] = []
                    if raw_ux_degraded:
                        ladder.refine()
                    if (
                        raw_ux_degraded
                        and ux_confirm_rounds(args) > 0
                        and int(probe.get("app_limit_count") or 0) == 0
                        and not round_server_unstable(probe, args)
                    ):
                        print(
                            "[capacity] "
                            f"{profile.label} accounts={account_count} crossed UX threshold; "
                            f"confirming {ux_confirm_rounds(args)} more time(s)",
                            flush=True,
                        )
                        for attempt in range(1, ux_confirm_rounds(args) + 1):
                            confirm_probe = run_load_round(instance, usernames, account_count, args)
                            confirm_degraded = round_experience_degraded(confirm_probe, args)
                            ux_confirmation_rounds.append({
                                "attempt": attempt,
                                "probe": confirm_probe,
                                "ux_degraded": confirm_degraded,
                            })
                            if int(confirm_probe.get("app_limit_count") or 0) > 0:
                                app_limit_seen = True
                            confirm_latency = confirm_probe.get("latency_ms") or {}
                            print(
                                "[capacity] "
                                f"{profile.label} accounts={account_count} "
                                f"ux-confirm={attempt}/{ux_confirm_rounds(args)} "
                                f"degraded={confirm_degraded} "
                                f"p95={confirm_latency.get('p95')}ms "
                                f"p99={confirm_latency.get('p99')}ms "
                                f"max={confirm_latency.get('max')}ms",
                                flush=True,
                            )
                    stopped, reasons = profile_failed(probe, args)
                    round_result = {
                        "accounts": account_count,
                        "probe": probe,
                        "stop_reasons": reasons,
                        "contaminated_after_app_limit": app_limit_seen,
                        "ux_degraded_raw": raw_ux_degraded,
                    }
                    if ux_confirmation_rounds:
                        round_result["ux_confirmation_rounds"] = ux_confirmation_rounds
                        round_result["ux_confirmation"] = build_ux_confirmation_summary(round_result, args)
                    if int(probe.get("app_limit_count") or 0) > 0:
                        app_limit_seen = True
                    profile_result["rounds"].append(round_result)
                    latency = probe.get("latency_ms") or {}
                    cpu = probe.get("cpu") or {}
                    print(
                        "[capacity] "
                        f"{profile.label} accounts={account_count} "
                        f"ok={probe.get('ok')} failures={probe.get('hard_failure_count')} "
                        f"app_limits={probe.get('app_limit_count')} "
                        f"server_failures={probe.get('server_failure_count')} "
                        f"statuses={probe.get('status_counts')} "
                        f"p95={latency.get('p95')}ms p99={latency.get('p99')}ms max={latency.get('max')}ms "
                        f"active_workers={cpu.get('active_worker_peak')} "
                        f"cpu_peak={cpu.get('total_worker_cpu_peak_percent')}%",
                        flush=True,
                    )
                    slow_labels = sorted(
                        (probe.get("by_label_latency_ms") or {}).items(),
                        key=lambda item: int((item[1] or {}).get("p95") or 0),
                        reverse=True,
                    )[:8]
                    if slow_labels:
                        print(
                            "[capacity] slowest labels: "
                            + " | ".join(
                                f"{label}:p95={stats.get('p95')}ms p99={stats.get('p99')}ms max={stats.get('max')}ms"
                                for label, stats in slow_labels
                            ),
                            flush=True,
                        )
                    if stopped and args.hard_failure_stop:
                        print(f"[capacity] stopping {profile.label}: {', '.join(reasons)}", flush=True)
                        break
                    cooldown = max(0.0, float(args.round_cooldown_seconds or 0.0))
                    if cooldown > 0:
                        print(f"[capacity] cooldown {cooldown:g}s", flush=True)
                        time.sleep(cooldown)
                if capacity_tier in {"sbc", "legacy", "laptop"} and profile_result.get("rounds"):
                    last_reasons = set((profile_result["rounds"][-1] or {}).get("stop_reasons") or [])
                    if last_reasons & {"hard_failures", "connection_errors", "server_busy"}:
                        stop_reason = f"{capacity_tier}_profile_failure"
                        print(
                            f"[capacity] stopping remaining profiles for {capacity_tier}: "
                            + ", ".join(sorted(last_reasons)),
                            flush=True,
                        )
                        break
            except Exception as exc:
                profile_result["error"] = f"{type(exc).__name__}: {exc}"
                if args.verbose:
                    raise
                print(f"[capacity] {profile.label} error: {profile_result['error']}", flush=True)
            finally:
                if instance is not None:
                    profile_result["log_tail"] = tail_log(instance.log_path)
                    stop_server(instance)
    finally:
        finished_at = datetime.now(timezone.utc).isoformat()
        recommendation = choose_recommendation(results, args)
        limit_report = build_limit_report(results, args)
        rc1_capacity_gate = build_rc1_capacity_gate(results, recommendation, limit_report)
        synced_defaults = sync_capacity_defaults(
            recommendation,
            args,
            output_path=output_path,
            finished_at=finished_at,
        )
        report = {
            "ok": any((round_item.get("probe") or {}).get("ok") for result in results for round_item in result.get("rounds", [])),
            "started_at": started_at,
            "finished_at": finished_at,
            "repo_root": str(REPO_ROOT),
            "temp_root": str(top_root),
            "cleanup": "kept" if args.keep_run_root else "removed",
            "load": {
                "capacity_tier": capacity_tier,
                "capacity_tier_description": CAPACITY_TIER_PRESETS.get(capacity_tier, {}).get("description", ""),
                "profile": args.load_profile,
                "kinds": load_kinds,
                "heavy_repeat": max(1, min(int(args.heavy_repeat or 1), 20)),
                "heavy_upload_bytes": max(0, int(args.heavy_upload_bytes or 0)),
            },
            "thresholds": {
                "target_p95_ms": args.target_p95_ms,
                "ux_p95_ms": args.ux_p95_ms,
                "ux_p99_ms": args.ux_p99_ms,
                "ux_confirm_rounds": ux_confirm_rounds(args),
                "hard_p95_ms": args.hard_p95_ms,
                "hard_max_ms": args.hard_max_ms,
                "max_duration_seconds": max_duration_seconds,
                "close_connections": bool(args.close_connections),
                "app_limits_disabled": not bool(args.keep_app_limits),
                "backpressure_disabled": bool(args.disable_backpressure),
                "gunicorn_max_requests": max(0, int(args.gunicorn_max_requests)),
                "gunicorn_max_requests_jitter": max(0, int(args.gunicorn_max_requests_jitter)),
            },
            "profiles": results,
            "limits": limit_report,
            "recommendation": recommendation,
            "rc1_capacity_gate": rc1_capacity_gate,
            "synced_defaults": synced_defaults,
            "stop_reason": stop_reason,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        ux_start = (limit_report.get("experience") or {}).get("degradation_starts_at")
        app_start = (limit_report.get("application_limit") or {}).get("first_observed_at")
        server_limit = (limit_report.get("server_instability") or {})
        if ux_start:
            print(f"[capacity] UX degradation starts at: {ux_start['accounts']} accounts", flush=True)
        if app_start:
            print(f"[capacity] application/rate limit starts at: {app_start['accounts']} accounts", flush=True)
        if server_limit.get("first_observed_at"):
            print(f"[capacity] server instability starts at: {server_limit['first_observed_at']['accounts']} accounts", flush=True)
        else:
            print(f"[capacity] server instability: {server_limit.get('status')}", flush=True)
        print(
            "[capacity] rc1_capacity_gate: "
            f"{'PASS' if rc1_capacity_gate.get('pass') else 'FAIL'} "
            f"profile={rc1_capacity_gate.get('recommended_profile') or '-'} "
            f"max_safe_accounts={rc1_capacity_gate.get('max_safe_accounts')} "
            f"reasons={rc1_capacity_gate.get('reasons')}",
            flush=True,
        )
        print(f"[capacity] report: {output_path}", flush=True)
        if synced_defaults.get("ok"):
            print(f"[capacity] synced defaults: {synced_defaults['path']}", flush=True)
        if args.keep_run_root:
            print(f"[capacity] kept temp root: {top_root}", flush=True)
        else:
            shutil.rmtree(top_root, ignore_errors=True)
            print(f"[capacity] removed temp root: {top_root}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
