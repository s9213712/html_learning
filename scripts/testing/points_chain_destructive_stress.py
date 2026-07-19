#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import sqlite3
import ssl
import string
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.testing.db_stress_probe import ResourceMonitor
from services.platform.db_mode_triggers import register_app_mode_function
from services.points_chain import PointsLedgerService
from services.points_chain.economy_layer import append_economy_event, load_economy_policy, replay_economy_events
from services.server.finance_database import get_finance_db


class ProbeClient:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = float(timeout)
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""
        self.lock = threading.Lock()

    def refresh_csrf(self) -> None:
        res = self.session.get(f"{self.base_url}/api/csrf-token", timeout=self.timeout)
        res.raise_for_status()
        try:
            self.csrf = str(res.json().get("csrf_token") or "")
        except Exception:
            self.csrf = ""
        if not self.csrf:
            self.csrf = str(self.session.cookies.get("csrf_token") or "")

    def login(self) -> dict[str, Any]:
        self.refresh_csrf()
        res = self.session.post(
            f"{self.base_url}/api/login",
            json={"username": self.username, "password": self.password},
            headers={"X-CSRF-Token": self.csrf},
            timeout=self.timeout,
        )
        self.csrf = str(res.cookies.get("csrf_token") or self.session.cookies.get("csrf_token") or self.csrf)
        return _json_response(res)

    def request(self, method: str, path: str, *, expected: set[int] | None = None, **kwargs) -> dict[str, Any]:
        method = method.upper()
        expected = expected or {200}
        headers = dict(kwargs.pop("headers", {}) or {})
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not self.csrf:
                self.refresh_csrf()
            headers.setdefault("X-CSRF-Token", self.csrf)
        started = time.perf_counter()
        with self.lock:
            try:
                res = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    timeout=self.timeout,
                    **kwargs,
                )
                if method in {"POST", "PUT", "PATCH", "DELETE"} and res.status_code in {400, 403} and "csrf" in res.text.lower()[:300]:
                    self.refresh_csrf()
                    headers["X-CSRF-Token"] = self.csrf
                    started = time.perf_counter()
                    res = self.session.request(
                        method,
                        f"{self.base_url}{path}",
                        headers=headers,
                        timeout=self.timeout,
                        **kwargs,
                    )
                payload = _json_response(res)
                management_headers = {
                    key: value
                    for key, value in res.headers.items()
                    if key.lower().startswith("x-management-plane-")
                }
                if management_headers:
                    payload["management_headers"] = management_headers
                payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
                payload["expected"] = int(res.status_code) in expected
                return payload
            except Exception as exc:
                return {
                    "status": 0,
                    "ok": False,
                    "expected": False,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error": f"{exc.__class__.__name__}: {str(exc)[:240]}",
                }


def _json_response(res: requests.Response) -> dict[str, Any]:
    try:
        body = res.json()
    except Exception:
        body = {"raw": res.text[:500]}
    if not isinstance(body, dict):
        body = {"body": body}
    body.setdefault("ok", 200 <= int(res.status_code) < 400)
    body["status"] = int(res.status_code)
    return body


def utc_old(seconds: int = 900) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def random_unowned_address(rng: random.Random) -> str:
    return "pc1" + "".join(rng.choice("0123456789abcdef") for _ in range(48))


def parse_pids(value: str) -> list[int]:
    pids = []
    for item in str(value or "").replace(",", " ").split():
        try:
            pids.append(int(item))
        except Exception:
            pass
    return pids


def pick_other_client_address(
    clients: list[dict[str, Any]],
    sender: dict[str, Any],
    *,
    rng: random.Random,
) -> str:
    candidates = [item["address"] for item in clients if item.get("address") != sender.get("address")]
    if not candidates:
        return random_unowned_address(rng)
    return rng.choice(candidates)


def db_path(runtime_root: str) -> Path:
    root = Path(runtime_root)
    candidates = [
        root / "runtime" / "database" / "database.db",
        root / "hackme_web" / "runtime" / "database" / "database.db",
        root / "database" / "database.db",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise SystemExit(f"database.db not found under {runtime_root}")


def finance_db_path(runtime_root: str) -> Path | None:
    database = db_path(runtime_root)
    candidate = database.parent / "finance.db"
    return candidate if candidate.exists() else None


def chain_seed_path(runtime_root: str) -> Path:
    root = Path(runtime_root)
    candidates = [
        root / "runtime" / ".chain_seed",
        root / "hackme_web" / "runtime" / ".chain_seed",
        root / "secrets" / ".chain_seed",
        root / ".chain_seed",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise SystemExit(f".chain_seed not found under {runtime_root}")


def service_for_runtime(runtime_root: str, *, mode: str) -> PointsLedgerService:
    core_database = db_path(runtime_root)
    finance_database = finance_db_path(runtime_root)
    chain_secret = chain_seed_path(runtime_root).read_text(encoding="utf-8").strip()

    def get_db() -> sqlite3.Connection:
        if finance_database is not None:
            return get_finance_db(
                finance_database,
                core_db_path=core_database,
                register_app_mode=lambda conn: register_app_mode_function(conn, mode_reader=lambda: mode),
            )
        conn = sqlite3.connect(str(core_database), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        register_app_mode_function(conn, mode_reader=lambda: mode)
        return conn

    return PointsLedgerService(
        get_db=get_db,
        chain_secret=chain_secret,
        backup_dir=core_database.parent / "points_chain_backups",
        mode_reader=lambda: mode,
    )


def root_actor_for_service(service: PointsLedgerService) -> dict[str, Any]:
    conn = service.get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE username='root'").fetchone()
        if not row:
            raise RuntimeError("root user not found for fixture grant")
        keys = set(row.keys())
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "role": str(row["role"]),
            "member_level": row["member_level"] if "member_level" in keys else "trusted",
            "effective_level": row["effective_level"] if "effective_level" in keys else "trusted",
        }
    finally:
        conn.close()


def fixture_official_grant(service: PointsLedgerService, *, root_actor: dict[str, Any], destination: str, amount: int, request_uuid: str) -> dict[str, Any]:
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        transfer = service._official_wallet_grant_locked(
            conn,
            actor=root_actor,
            destination_wallet_address=destination,
            amount=int(amount),
            reason="destructive stress fixture official grant",
            request_uuid=request_uuid,
        )
        conn.commit()
        return transfer
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def numeric_latency_summary(values: list[float]) -> dict[str, Any]:
    values = sorted(float(value or 0) for value in values if float(value or 0) > 0)
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min_ms": round(values[0], 3),
        "median_ms": round(median(values), 3),
        "p95_ms": round(values[int(len(values) * 0.95) - 1], 3),
        "p99_ms": round(values[int(len(values) * 0.99) - 1], 3),
        "max_ms": round(values[-1], 3),
    }


def economy_fund_balances(service: PointsLedgerService) -> dict[str, int]:
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        policy = load_economy_policy(conn)
        replay = replay_economy_events(
            conn,
            policy=policy,
            chain_secret=service.chain_secret,
            persist_cache=False,
            chain_branch=service._canonical_branch_uuid(conn),
        )
        balances = replay.get("balances") or {}
        return {str(key): int((value or {}).get("balance") or 0) for key, value in balances.items()}
    finally:
        conn.close()


def fixture_mint_to_treasury(service: PointsLedgerService, *, root_actor: dict[str, Any], amount: int, request_uuid: str) -> dict[str, Any]:
    amount = int(amount or 0)
    if amount <= 0:
        return {"event_uuid": "", "created": False, "amount": 0, "skipped": True}
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        event, created = append_economy_event(
            conn,
            chain_secret=service.chain_secret,
            event_type="mint",
            transaction_type="qa_destructive_stress_fixture_mint",
            source_fund_key="mint",
            destination_fund_key="official_treasury",
            amount=int(amount),
            idempotency_key=request_uuid,
            metadata={"fixture": True, "reason": "destructive stress isolated test funding"},
            actor=root_actor,
            chain_branch=service._canonical_branch_uuid(conn),
        )
        conn.commit()
        return {"event_uuid": event["event_uuid"], "created": bool(created), "amount": int(amount)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_fixture_treasury_funding(
    service: PointsLedgerService,
    *,
    root_actor: dict[str, Any],
    needed_amount: int,
    request_uuid: str,
) -> dict[str, Any]:
    balances = economy_fund_balances(service)
    treasury_balance = int(balances.get("official_treasury") or 0)
    shortfall = max(0, int(needed_amount or 0) - treasury_balance)
    if shortfall <= 0:
        return {
            "event_uuid": "",
            "created": False,
            "amount": 0,
            "skipped": True,
            "treasury_balance_before": treasury_balance,
            "needed_amount": int(needed_amount or 0),
        }
    minted = fixture_mint_to_treasury(
        service,
        root_actor=root_actor,
        amount=shortfall,
        request_uuid=request_uuid,
    )
    minted.update({
        "treasury_balance_before": treasury_balance,
        "needed_amount": int(needed_amount or 0),
        "shortfall": shortfall,
    })
    return minted


def db_scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int((row[0] if row else 0) or 0)


def force_proved(db: Path, prefix: str) -> dict[str, int]:
    conn = sqlite3.connect(str(db))
    try:
        old = utc_old()
        cur = conn.execute(
            """
            UPDATE points_chain_transfer_requests
            SET created_at=?
            WHERE request_uuid LIKE ? AND status='pending'
            """,
            (old, f"{prefix}%"),
        )
        conn.commit()
        return {"aged_pending_requests": int(cur.rowcount or 0)}
    finally:
        conn.close()


def db_counts(db: Path, prefix: str) -> dict[str, Any]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        pending = db_scalar(conn, "SELECT COUNT(*) FROM points_chain_transfer_requests WHERE request_uuid LIKE ? AND status='pending'", (f"{prefix}%",))
        confirmed = db_scalar(conn, "SELECT COUNT(*) FROM points_chain_transfer_requests WHERE request_uuid LIKE ? AND status='confirmed'", (f"{prefix}%",))
        failed = db_scalar(conn, "SELECT COUNT(*) FROM points_chain_transfer_requests WHERE request_uuid LIKE ? AND status LIKE 'failed%'", (f"{prefix}%",))
        duplicate_request_uuid = db_scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT request_uuid FROM points_chain_transfer_requests
                GROUP BY request_uuid HAVING COUNT(*) > 1
            )
            """,
        )
        duplicate_active_wallet = db_scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT address FROM points_wallet_identities
                WHERE status IN ('pending_backup', 'active')
                GROUP BY address HAVING COUNT(*) > 1
            )
            """,
        )
        fee_to_burn = db_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM points_ledger
            WHERE action_type IN ('wallet_transfer_fee', 'chain_acceleration_fee')
              AND public_metadata_json LIKE '%burn%'
            """,
        )
        return {
            "prefix_pending": pending,
            "prefix_confirmed": confirmed,
            "prefix_failed": failed,
            "duplicate_request_uuid_groups": duplicate_request_uuid,
            "duplicate_active_wallet_address_groups": duplicate_active_wallet,
            "fee_ledgers_with_burn_metadata": fee_to_burn,
            "database_bytes": db.stat().st_size if db.exists() else 0,
        }
    finally:
        conn.close()


def pending_request_uuids(db: Path, prefix: str) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [
            str(row[0])
            for row in conn.execute(
                """
                SELECT request_uuid
                FROM points_chain_transfer_requests
                WHERE request_uuid LIKE ? AND status='pending'
                ORDER BY id ASC
                """,
                (f"{prefix}%",),
            ).fetchall()
        ]
    finally:
        conn.close()


def finalize_prefix_pending_via_sweep_job(client: ProbeClient, db: Path, prefix: str) -> dict[str, Any]:
    before = pending_request_uuids(db, prefix)
    sweeps = []
    # Finality maintenance is an explicit root management-plane job. The old
    # per-request explorer/list path was slow at 50K scale and is no longer a
    # valid release-gate finalization primitive.
    max_attempts = max(8, min(120, ((len(before) + 24) // 25) + 12))
    for attempt in range(max_attempts):
        if not pending_request_uuids(db, prefix):
            break
        res = run_finality_sweep_job(client, f"prefix_finality_sweep_{attempt + 1}", limit=100)
        latest = res.get("latest") if isinstance(res.get("latest"), dict) else {}
        snapshot = latest.get("snapshot") if isinstance(latest.get("snapshot"), dict) else {}
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
        sweeps.append({
            "attempt": attempt + 1,
            "status": res.get("status"),
            "elapsed_ms": (res.get("start") or {}).get("elapsed_ms"),
            "finalized_count": summary.get("finalized_count"),
            "confirmed_count": summary.get("confirmed_count"),
            "job_succeeded": (res.get("status_check") or {}).get("job_succeeded"),
            "msg": res.get("msg"),
            "error": res.get("error"),
        })
        lock_text = f"{res.get('msg') or ''} {res.get('error') or ''} {(res.get('latest') or {}).get('error') or ''}".lower()
        if int(res.get("status") or 0) in {0, 400, 503} and "locked" in lock_text:
            time.sleep(min(5.0, 0.25 * (attempt + 1)))
    remaining = pending_request_uuids(db, prefix)
    return {
        "attempted": len(before),
        "confirmed": max(0, len(before) - len(remaining)),
        "remaining_pending": len(remaining),
        "remaining_request_uuids": remaining[:50],
        "samples": sweeps,
    }


def latency_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(float(item.get("elapsed_ms") or 0) for item in samples if float(item.get("elapsed_ms") or 0) > 0)
    status = Counter(str(item.get("status", 0)) for item in samples)
    if not values:
        return {"count": len(samples), "status": dict(status)}
    return {
        "count": len(samples),
        "status": dict(sorted(status.items())),
        "p50_ms": round(float(median(values)), 3),
        "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 3),
        "p99_ms": round(values[min(len(values) - 1, int(len(values) * 0.99))], 3),
        "max_ms": round(values[-1], 3),
    }


def active_wallet(client: ProbeClient) -> tuple[str, dict[str, Any]]:
    wallet = client.request("GET", "/api/points/wallet")
    address = str((wallet.get("wallet") or {}).get("active_wallet_address") or "")
    if not address:
        onboarding = client.request("GET", "/api/points/wallet/onboarding")
        for item in ((onboarding.get("onboarding") or {}).get("wallets") or []):
            if item.get("wallet_type") == "official_hot" and item.get("address"):
                address = str(item["address"])
                break
    return address, wallet


def ensure_official_hot_wallet(client: ProbeClient) -> str:
    address, _wallet = active_wallet(client)
    if address:
        return address
    res = client.request(
        "POST",
        "/api/points/wallet/onboarding",
        json={"mode": "official_hot"},
        expected={200, 400, 409},
    )
    if int(res.get("status") or 0) != 200:
        raise RuntimeError(f"wallet onboarding failed for {client.username}: {res}")
    address = str((res.get("wallet_identity") or {}).get("address") or "")
    if not address:
        address, _wallet = active_wallet(client)
    if not address:
        raise RuntimeError(f"wallet address missing for {client.username}")
    return address


def create_or_get_user(root: ProbeClient, username: str, password: str) -> dict[str, Any]:
    search_path = f"/api/admin/users?q={username}&page_size=100"
    users = root.request("GET", search_path, expected={200})
    for item in users.get("users") or []:
        if item.get("username") == username:
            return {"id": int(item["id"]), "username": username, "created": False}
    res = root.request(
        "POST",
        "/api/admin/users",
        json={
            "username": username,
            "password": password,
            "password_confirm": password,
            "nickname": username,
            "role": "user",
            "status": "active",
        },
        expected={200, 409},
    )
    if int(res.get("status") or 0) not in {200, 409}:
        raise RuntimeError(f"user create failed: {username}: {res}")
    users = root.request("GET", search_path, expected={200})
    for item in users.get("users") or []:
        if item.get("username") == username:
            return {"id": int(item["id"]), "username": username, "created": int(res.get("status") or 0) == 200}
    raise RuntimeError(f"user not found after create: {username}")


def login_with_retry(client: ProbeClient, *, attempts: int = 8, base_sleep: float = 0.5) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for attempt in range(max(1, int(attempts))):
        try:
            last = client.login()
        except Exception as exc:
            last = {
                "status": 0,
                "ok": False,
                "error": f"{exc.__class__.__name__}: {str(exc)[:240]}",
            }
        if int(last.get("status") or 0) not in {0, 429, 503}:
            return last
        time.sleep(float(base_sleep) * (attempt + 1))
    return last


def wallet_balance(client: ProbeClient) -> dict[str, int]:
    res = client.request("GET", "/api/points/wallet")
    wallet = res.get("wallet") or {}
    return {
        "balance": int(wallet.get("points_balance") or 0),
        "frozen": int(wallet.get("points_frozen") or 0),
        "account_balance": int(wallet.get("account_points_balance") or 0),
        "account_frozen": int(wallet.get("account_points_frozen") or 0),
    }


def fee_market_snapshot(client: ProbeClient, label: str) -> dict[str, Any]:
    base = client.request("GET", "/api/points/explorer/fee-estimate?fee_points=0", expected={200})
    estimate = base.get("estimate") or {}
    network = estimate.get("network_fee_state") or {}
    suggested_fee = int(network.get("suggested_priority_fee_points") or estimate.get("fee_reference_points") or 0)
    accelerated = client.request(
        "GET",
        f"/api/points/explorer/fee-estimate?fee_points={max(0, suggested_fee)}",
        expected={200},
    )
    accelerated_estimate = accelerated.get("estimate") or {}
    return {
        "label": label,
        "status": base.get("status"),
        "suggested_status": accelerated.get("status"),
        "pending_transfer_count": int(network.get("pending_transfer_count") or 0),
        "unsealed_ledger_count": int(network.get("unsealed_ledger_count") or 0),
        "recent_ledger_count": int(network.get("recent_ledger_count") or 0),
        "congestion_ratio": float(network.get("congestion_ratio") or 0),
        "congestion_label": network.get("congestion_label") or "",
        "base_fee_points": int(network.get("base_fee_points") or 0),
        "suggested_priority_fee_points": suggested_fee,
        "suggested_total_fee_points": int(network.get("suggested_total_fee_points") or 0),
        "zero_fee_estimated_seconds_min": int(estimate.get("estimated_seconds_min") or 0),
        "zero_fee_estimated_seconds_max": int(estimate.get("estimated_seconds_max") or 0),
        "suggested_fee_estimated_seconds_min": int(accelerated_estimate.get("estimated_seconds_min") or 0),
        "suggested_fee_estimated_seconds_max": int(accelerated_estimate.get("estimated_seconds_max") or 0),
        "speedup_ratio_at_suggested_fee": float(accelerated_estimate.get("speedup_ratio") or 0),
    }


def wait_management_job(client: ProbeClient, status_url: str, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
    deadline = time.perf_counter() + max(1.0, float(timeout_seconds))
    last: dict[str, Any] = {"status": 0, "ok": False, "error": "job status was not checked"}
    while time.perf_counter() < deadline:
        last = client.request("GET", status_url, expected={200, 404})
        job = last.get("job") if isinstance(last.get("job"), dict) else {}
        status = str(job.get("status") or "")
        if status in {"succeeded", "failed", "cancelled"}:
            last["job_terminal"] = True
            last["job_succeeded"] = status == "succeeded"
            return last
        time.sleep(0.25)
    last["job_terminal"] = False
    last["job_succeeded"] = False
    last["error"] = f"management job did not finish within {timeout_seconds}s"
    return last


def run_finality_sweep_job(root: ProbeClient, label: str, *, limit: int = 100) -> dict[str, Any]:
    started = root.request(
        "POST",
        "/api/root/points/finality-sweep",
        json={"limit": int(limit)},
        expected={202},
    )
    result: dict[str, Any] = {"op": label, "start": started}
    status_url = str(started.get("status_url") or "")
    if status_url:
        result["status_check"] = wait_management_job(root, status_url, timeout_seconds=30)
    latest = root.request("GET", "/api/root/points/finality-sweep/latest", expected={200, 404})
    result["latest"] = latest
    result["ok"] = bool(started.get("expected")) and bool((result.get("status_check") or {}).get("job_succeeded", True))
    result["status"] = int(started.get("status") or 0)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Destructive PointsChain/trading stress probe for isolated hackme_web runtimes.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--out", required=True)
    from scripts.testing.probe_credentials import ROOT_PASSWORD_ENV_NAMES, add_root_password_argument
    add_root_password_argument(
        parser,
        env_names=("HACKME_POINTS_STRESS_ROOT_PASSWORD", *ROOT_PASSWORD_ENV_NAMES),
    )
    parser.add_argument("--accounts", type=int, default=20)
    parser.add_argument("--grant-points", type=int, default=5000)
    parser.add_argument("--transfer-ops", type=int, default=50)
    parser.add_argument("--direct-transfer-ops", type=int, default=0, help="Additional service-layer pc0->pc0 transfers for high-volume financial invariant stress.")
    parser.add_argument("--trading-ops", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--external-transfer-every", type=int, default=5, help="Send every Nth wallet transfer to an unowned pc1 address. 0 disables this path.")
    parser.add_argument("--max-external-transfers", type=int, default=0, help="Maximum unowned pc1 bridge/withdrawal transfers. 0 means unlimited.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--mode", default="dev_ready")
    parser.add_argument("--server-pids", default=os.environ.get("HACKME_SERVER_PIDS", ""), help="Comma/space separated server process PIDs to sample during the probe.")
    parser.add_argument("--resource-interval", type=float, default=1.0)
    args = parser.parse_args()

    requests.packages.urllib3.disable_warnings()
    ssl._create_default_https_context = ssl._create_unverified_context

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_started = time.perf_counter()
    core_database = db_path(args.runtime_root)
    database = finance_db_path(args.runtime_root) or core_database
    points_service = service_for_runtime(args.runtime_root, mode=args.mode)
    root_actor = root_actor_for_service(points_service)
    prefix = "dstress-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-"
    rng = random.Random(prefix)
    samples: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    fee_market_samples: list[dict[str, Any]] = []
    resource_summary: dict[str, Any] = {}
    monitor = None
    pids = parse_pids(args.server_pids)
    if pids:
        database_dir = core_database.parent
        monitored_paths = {
            "main": database_dir / "database.db",
            "auth": database_dir / "auth.db",
            "audit": database_dir / "audit.db",
            "control": database_dir / "control.db",
            "finance": database_dir / "finance.db",
            "points_chain": database_dir / "points_chain.db",
            "trading": database_dir / "trading.db",
            "jobs": database_dir / "jobs.db",
        }
        monitor = ResourceMonitor(
            runtime_root=Path(args.runtime_root),
            paths={label: path for label, path in monitored_paths.items() if path.exists()},
            interval=max(0.2, float(args.resource_interval or 1.0)),
            pids=pids,
        )
        monitor.start()

    root = ProbeClient(args.base_url, "root", args.root_password, timeout=args.timeout)
    root_login = login_with_retry(root)
    if int(root_login.get("status") or 0) != 200:
        if monitor:
            monitor.stop()
        raise SystemExit(f"root login failed: {root_login}")
    fee_market_samples.append(fee_market_snapshot(root, "baseline_before_internal_grants"))

    users: list[dict[str, Any]] = []
    password = os.environ.get("HACKME_POINTS_STRESS_ACCOUNT_PASSWORD") or f"Dstress-{secrets.token_urlsafe(18)}"
    for idx in range(max(1, int(args.accounts))):
        username = f"dstress_{datetime.now(timezone.utc).strftime('%H%M%S')}_{idx:02d}"
        users.append(create_or_get_user(root, username, password))

    clients: list[dict[str, Any]] = []
    for item in users:
        client = ProbeClient(args.base_url, item["username"], password, timeout=args.timeout)
        login = login_with_retry(client)
        samples.append({"op": "login", **login})
        if int(login.get("status") or 0) != 200:
            findings.append({"severity": "high", "title": "stress user login failed", "user": item["username"], "response": login})
            continue
        address = ensure_official_hot_wallet(client)
        before = wallet_balance(client)
        clients.append({"user": item, "client": client, "address": address, "balance_before_grant": before})

    fixture_mint = ensure_fixture_treasury_funding(
        points_service,
        root_actor=root_actor,
        needed_amount=max(0, len(clients) * int(args.grant_points) + 1000),
        request_uuid=prefix + "fixture-mint",
    )
    samples.append({"op": "fixture_mint_to_treasury", "status": 200, "ok": True, "expected": True, **fixture_mint})

    grant_samples = []
    for item in clients:
        request_uuid = prefix + "grant-" + item["user"]["username"]
        grant = fixture_official_grant(
            points_service,
            root_actor=root_actor,
            destination=item["address"],
            amount=int(args.grant_points),
            request_uuid=request_uuid,
        )
        res = {
            "ok": True,
            "status": 200,
            "expected": True,
            "op": "official_grant_fixture_internal",
            "transaction_hash": grant.get("transaction_hash"),
            "tx_group_hash": grant.get("transaction_hash"),
            "request_uuid": request_uuid,
            "settlement_rail": grant.get("settlement_rail") or "internal_hot_wallet",
            "fixture": True,
        }
        grant_samples.append(res)
        samples.append(res)
        after_grant = wallet_balance(item["client"])
        item["balance_after_internal_grant"] = after_grant
        item["grant_hash"] = res.get("transaction_hash") or res.get("tx_group_hash")
        if after_grant["balance"] < item["balance_before_grant"]["balance"] + int(args.grant_points):
            findings.append({
                "severity": "critical",
                "title": "pc0 official grant did not credit immediately",
                "user": item["user"]["username"],
                "before": item["balance_before_grant"],
                "after_grant": after_grant,
                "transaction_hash": item.get("grant_hash"),
            })

    fee_market_samples.append(fee_market_snapshot(root, "after_internal_official_grants"))
    forced_grants = force_proved(database, prefix + "grant-")
    grant_sweep = run_finality_sweep_job(root, "root_finalize_grants", limit=100)
    samples.append(grant_sweep)
    root_refresh = root.request("GET", "/api/points/transactions?limit=100&compact=1&sweep=0", expected={200})
    samples.append({"op": "root_observe_grants_after_finality_sweep", **root_refresh})
    explorer_finalized_grants = finalize_prefix_pending_via_sweep_job(root, database, prefix + "grant-")
    for item in clients:
        after_confirm = wallet_balance(item["client"])
        item["balance_after_confirmed_grant"] = after_confirm
        if after_confirm["balance"] < item["balance_before_grant"]["balance"] + int(args.grant_points):
            findings.append({
                "severity": "critical",
                "title": "official grant did not credit after forced proved finalization",
                "user": item["user"]["username"],
                "before": item["balance_before_grant"],
                "after_confirm": after_confirm,
                "transaction_hash": item.get("grant_hash"),
            })

    transfer_tasks: list[tuple[dict[str, Any], str, int, int, str, str]] = []
    external_transfer_count = 0
    external_every = max(0, int(args.external_transfer_every or 0))
    max_external = max(0, int(args.max_external_transfers or 0))
    for idx in range(max(1, int(args.transfer_ops))):
        sender = clients[idx % len(clients)]
        should_external = bool(external_every and idx % external_every == 0 and (max_external <= 0 or external_transfer_count < max_external))
        if should_external:
            destination = random_unowned_address(rng)
            external_transfer_count += 1
        else:
            destination = pick_other_client_address(clients, sender, rng=rng)
        amount = rng.randint(5, 45)
        fee = rng.randint(1, 30)
        transfer_tasks.append((sender, destination, amount, fee, prefix + f"tx-{idx:04d}", "stress transfer"))

    def submit_transfer(task: tuple[dict[str, Any], str, int, int, str, str]) -> dict[str, Any]:
        sender, destination, amount, fee, request_uuid, memo = task
        payload = {
            "source_wallet_address": sender["address"],
            "destination_wallet_address": destination,
            "amount_points": amount,
            "fee_points": fee,
            "request_uuid": request_uuid,
            "memo": memo,
            "compact": True,
        }
        res = sender["client"].request(
            "POST",
            "/api/points/transactions/submit",
            json=payload,
            expected={200, 409},
        )
        if int(res.get("status") or 0) == 0:
            first_error = {
                "status": res.get("status"),
                "elapsed_ms": res.get("elapsed_ms"),
                "error": res.get("error"),
            }
            res = sender["client"].request(
                "POST",
                "/api/points/transactions/submit",
                json=payload,
                expected={200, 409},
            )
            res["transport_retried"] = True
            res["transport_first_error"] = first_error
        res.update({
            "op": "wallet_transfer",
            "request_uuid": request_uuid,
            "sender_username": sender["user"]["username"],
            "sender_address": sender["address"],
            "amount": amount,
            "fee": fee,
        })
        return res

    with ThreadPoolExecutor(max_workers=max(1, int(args.concurrency))) as pool:
        for fut in as_completed([pool.submit(submit_transfer, task) for task in transfer_tasks]):
            samples.append(fut.result())

    direct_transfer_completed = 0
    direct_transfer_errors = 0
    direct_latency_values: list[float] = []
    direct_status_counts: Counter[str] = Counter()
    direct_error_samples: list[dict[str, Any]] = []

    def submit_direct_transfer(idx: int) -> dict[str, Any]:
        sender = clients[idx % len(clients)]
        recipient = clients[(idx * 7 + 1) % len(clients)]
        if recipient["address"] == sender["address"]:
            recipient = clients[(idx + 1) % len(clients)]
        amount = 1 + (idx % 5)
        request_uuid = prefix + f"direct-{idx:06d}"
        started = time.perf_counter()
        try:
            actor = {
                "id": int(sender["user"]["id"]),
                "username": sender["user"]["username"],
                "role": "user",
                "member_level": "trusted",
                "effective_level": "trusted",
            }
            result = points_service.submit_wallet_transaction(
                actor=actor,
                source_wallet_address=sender["address"],
                destination_wallet_address=recipient["address"],
                amount_points=amount,
                fee_points=0,
                request_uuid=request_uuid,
                memo="direct service-layer extreme transfer",
            )
            return {
                "op": "direct_wallet_transfer",
                "ok": True,
                "expected": True,
                "status": 200,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "request_uuid": request_uuid,
                "tx_group_hash": result.get("tx_group_hash"),
                "settlement_rail": ((result.get("transaction") or {}).get("settlement_rail") or ""),
                "amount": amount,
            }
        except Exception as exc:
            return {
                "op": "direct_wallet_transfer",
                "ok": False,
                "expected": False,
                "status": 0,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "request_uuid": request_uuid,
                "error": f"{exc.__class__.__name__}: {str(exc)[:240]}",
                "amount": amount,
            }

    direct_ops = max(0, int(args.direct_transfer_ops or 0))
    if direct_ops:
        print(f"[direct-transfer] starting {direct_ops} service-layer pc0 transfers", flush=True)
        max_workers = max(1, int(args.concurrency))
        max_inflight = max_workers * 4
        with ThreadPoolExecutor(max_workers=max(1, int(args.concurrency))) as pool:
            futures = set()
            next_idx = 0
            while next_idx < direct_ops and len(futures) < max_inflight:
                futures.add(pool.submit(submit_direct_transfer, next_idx))
                next_idx += 1
            while futures:
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for fut in done:
                    result = fut.result()
                    direct_transfer_completed += 1
                    elapsed_ms = float(result.get("elapsed_ms") or 0)
                    if elapsed_ms > 0:
                        direct_latency_values.append(elapsed_ms)
                    direct_status_counts[str(result.get("status", 0))] += 1
                    if not result.get("ok"):
                        direct_transfer_errors += 1
                        if len(direct_error_samples) < 100:
                            direct_error_samples.append(result)
                            samples.append(result)
                    if direct_transfer_completed % 5000 == 0 or direct_transfer_completed == direct_ops:
                        samples.append({
                            "op": "direct_wallet_transfer_progress",
                            "ok": True,
                            "expected": True,
                            "status": 200,
                            "completed": direct_transfer_completed,
                            "requested": direct_ops,
                            "errors": direct_transfer_errors,
                            "elapsed_ms": elapsed_ms,
                        })
                        print(
                            f"[direct-transfer] completed {direct_transfer_completed}/{direct_ops} "
                            f"errors={direct_transfer_errors}",
                            flush=True,
                        )
                while next_idx < direct_ops and len(futures) < max_inflight:
                    futures.add(pool.submit(submit_direct_transfer, next_idx))
                    next_idx += 1

    duplicate_task = (clients[0], clients[1]["address"], 11, 1, prefix + "duplicate-once", "duplicate idempotency")
    dup_first = submit_transfer(duplicate_task)
    dup_second = submit_transfer(duplicate_task)
    samples.extend([{**dup_first, "op": "duplicate_first"}, {**dup_second, "op": "duplicate_second"}])
    if not (dup_first.get("transaction_hash") and dup_first.get("transaction_hash") == dup_second.get("transaction_hash") and dup_second.get("created") is False):
        findings.append({"severity": "high", "title": "duplicate request_uuid was not idempotent", "first": dup_first, "second": dup_second})

    rich = max(clients, key=lambda item: wallet_balance(item["client"])["balance"])
    rich_balance = wallet_balance(rich["client"])["balance"]
    oversized_amount = max(100, rich_balance // 3)
    overspend_tasks = [
        (rich, random_unowned_address(rng), oversized_amount, 1, prefix + f"overspend-{idx:02d}", "overspend probe")
        for idx in range(12)
    ]
    overspend_results = []
    with ThreadPoolExecutor(max_workers=min(12, max(1, int(args.concurrency)))) as pool:
        for fut in as_completed([pool.submit(submit_transfer, task) for task in overspend_tasks]):
            result = fut.result()
            result["op"] = "overspend_transfer"
            overspend_results.append(result)
            samples.append(result)
    if not any(int(r.get("status") or 0) == 409 for r in overspend_results):
        findings.append({"severity": "critical", "title": "overspend burst did not produce any insufficient-balance rejection", "balance": rich_balance, "amount": oversized_amount})
    rich_after_overspend = wallet_balance(rich["client"])
    if rich_after_overspend["balance"] < 0:
        findings.append({"severity": "critical", "title": "wallet balance went negative after overspend burst", "wallet": rich["address"], "balance": rich_after_overspend})

    fee_market_samples.append(fee_market_snapshot(root, "after_pending_transfer_burst"))
    pending_ok = [
        s
        for s in samples
        if s.get("op") in {"wallet_transfer", "duplicate_first", "overspend_transfer"}
        and int(s.get("status") or 0) == 200
        and str(((s.get("request") or {}).get("status")) or "").lower() == "pending"
        and str(((s.get("request") or {}).get("settlement_rail")) or "").lower() != "internal_hot_wallet"
    ]
    if pending_ok:
        target = pending_ok[0]
        owner = next((item for item in clients if item["user"]["username"] == target.get("sender_username")), clients[0])
        accel = owner["client"].request(
            "POST",
            "/api/points/explorer/accelerate",
            json={
                "ledger_ref": target.get("transaction_hash") or target.get("tx_group_hash"),
                "fee_points": 25,
                "request_uuid": prefix + "accelerate-1",
            },
            expected={200, 409},
        )
        accel["op"] = "accelerate_pending"
        samples.append(accel)
        if int(accel.get("status") or 0) != 200:
            findings.append({"severity": "high", "title": "transaction owner could not accelerate pending transfer", "target": target, "response": accel})
    else:
        samples.append({
            "op": "accelerate_pending_skipped",
            "status": 200,
            "ok": True,
            "expected": True,
            "reason": "no cold-chain pending transfer; pc0 internal transfers are immediately settled and cannot be accelerated",
        })

    notification_checks = []
    for item in clients[:2]:
        notices = item["client"].request("GET", "/api/notifications?limit=50", expected={200, 503})
        notification_checks.append({
            "user": item["user"]["username"],
            "status": notices.get("status"),
            "types": [n.get("type") for n in (notices.get("notifications") or [])[:20]],
            "unread_count": notices.get("unread_count"),
        })

    forced_transfers = force_proved(database, prefix)
    for _ in range(3):
        sweep_result = run_finality_sweep_job(root, "root_finalize_transfers", limit=100)
        samples.append(sweep_result)
        refreshed = root.request("GET", "/api/points/transactions?limit=100&compact=1&sweep=0", expected={200})
        samples.append({"op": "root_observe_transfers_after_finality_sweep", **refreshed})
        if int(((refreshed.get("summary") or {}).get("pending_count") or 0)) == 0:
            break
        time.sleep(0.2)
    explorer_finalized = finalize_prefix_pending_via_sweep_job(root, database, prefix)
    fee_market_samples.append(fee_market_snapshot(root, "after_forced_finality_before_seal"))

    trading_results = []
    trading_price_mode = root.request(
        "POST",
        "/api/root/trading/settings",
        json={"settings": {"price_source": "manual_root"}},
        expected={200, 400, 403, 503},
    )
    samples.append({"op": "root_trading_price_mode_manual", **trading_price_mode})
    markets = clients[0]["client"].request("GET", "/api/trading/markets", expected={200, 403, 503})
    samples.append({"op": "trading_markets", **markets})
    market_list = markets.get("markets") or markets.get("data") or []
    if isinstance(market_list, list) and market_list:
        market = market_list[0]
        symbol = market.get("symbol") or market.get("market_symbol") or market.get("display_symbol") or "BTC/POINTS"
    else:
        symbol = "BTC/POINTS"
    for idx in range(max(0, int(args.trading_ops))):
        client = clients[idx % len(clients)]["client"]
        res = client.request(
            "POST",
            "/api/trading/orders",
            json={
                "market_symbol": symbol,
                "side": "buy",
                "order_type": "limit",
                "quantity": "1",
                "limit_price_points": 100,
            },
            expected={200, 400, 403, 409, 503},
        )
        res["op"] = "trading_limit_buy"
        trading_results.append(res)
        samples.append(res)

    margin_probe = clients[0]["client"].request(
        "POST",
        "/api/trading/margin/open",
        json={
            "market_symbol": symbol,
            "position_type": "margin_long",
            "quantity": "1000000",
            "collateral_points": 1,
            "idempotency_key": prefix + "margin-exhaustion",
        },
        expected={200, 400, 403, 409, 503},
    )
    margin_probe["op"] = "margin_exhaustion_probe"
    samples.append(margin_probe)

    seal = root.request("POST", "/api/root/points/chain/seal", json={"limit": 500}, expected={200, 202, 400, 409})
    verify = root.request("GET", "/api/root/points/chain/verify", expected={200, 202})
    root_report = root.request("GET", "/api/root/points/report", expected={200, 202})
    trading_refresh = root.request("POST", "/api/root/trading/sitewide/refresh", json={"reason": "destructive_stress"}, expected={200, 202, 400, 409, 503})
    trading_pools = root.request("GET", "/api/root/trading/sitewide/pools", expected={200, 400, 404, 409, 503})
    samples.extend([
        {"op": "root_chain_seal", **seal},
        {"op": "root_chain_verify", **verify},
        {"op": "root_points_report", **root_report},
        {"op": "root_trading_refresh", **trading_refresh},
        {"op": "root_trading_pools", **trading_pools},
    ])
    management_snapshot_reads = []
    for op_name, started in (
        ("root_chain_seal_snapshot", seal),
        ("root_chain_verify_snapshot", verify),
        ("root_points_report_snapshot", root_report),
        ("root_trading_refresh_snapshot", trading_refresh),
    ):
        snapshot_url = str(started.get("latest_snapshot_url") or "")
        if not snapshot_url:
            continue
        snapshot_res = root.request("GET", snapshot_url, expected={200, 404})
        snapshot_res["op"] = op_name
        management_snapshot_reads.append(snapshot_res)
        samples.append(snapshot_res)
    fee_market_samples.append(fee_market_snapshot(root, "after_seal"))

    counts = db_counts(database, prefix)
    if explorer_finalized["remaining_pending"]:
        findings.append({
            "severity": "critical",
            "title": "forced-proved pending transfers remained pending after root list and explorer finalization",
            "remaining_pending": explorer_finalized["remaining_pending"],
            "remaining_request_uuids": explorer_finalized["remaining_request_uuids"],
        })
    if len(fee_market_samples) >= 3:
        def assert_fee_market_monotonic(before: dict[str, Any], after: dict[str, Any], label: str) -> None:
            before_congestion = float(before.get("congestion_ratio") or 0)
            after_congestion = float(after.get("congestion_ratio") or 0)
            if after_congestion <= before_congestion:
                return
            if after["suggested_priority_fee_points"] < before["suggested_priority_fee_points"]:
                findings.append({
                    "severity": "high",
                    "title": f"suggested priority fee did not rise with {label} congestion",
                    "before": before,
                    "after": after,
                })
            if after["zero_fee_estimated_seconds_min"] < before["zero_fee_estimated_seconds_min"]:
                findings.append({
                    "severity": "high",
                    "title": f"zero-fee finality estimate got faster while {label} congestion increased",
                    "before": before,
                    "after": after,
                })

        baseline = fee_market_samples[0]
        internal_grants = fee_market_samples[1]
        transfer_pending = fee_market_samples[2]
        assert_fee_market_monotonic(baseline, internal_grants, "internal grant")
        assert_fee_market_monotonic(internal_grants, transfer_pending, "transfer burst")
        for snapshot in fee_market_samples:
            if snapshot["suggested_priority_fee_points"] > 0 and snapshot["suggested_fee_estimated_seconds_min"] >= snapshot["zero_fee_estimated_seconds_min"]:
                findings.append({
                    "severity": "medium",
                    "title": "suggested priority fee did not improve minimum finality estimate",
                    "snapshot": snapshot,
                })
    if counts["duplicate_request_uuid_groups"]:
        findings.append({"severity": "critical", "title": "duplicate request_uuid rows exist", "count": counts["duplicate_request_uuid_groups"]})
    if counts["duplicate_active_wallet_address_groups"]:
        findings.append({"severity": "critical", "title": "duplicate active wallet address bindings exist", "count": counts["duplicate_active_wallet_address_groups"]})
    if not bool(verify.get("ok")):
        findings.append({"severity": "critical", "title": "PointsChain verify failed after destructive stress", "verification": verify.get("verification")})
    hard_5xx = [s for s in samples if int(s.get("status") or 0) >= 500 and int(s.get("status") or 0) != 503]
    if hard_5xx:
        findings.append({"severity": "high", "title": "HTTP 5xx during destructive stress", "count": len(hard_5xx), "samples": hard_5xx[:10]})
    if monitor:
        resource_summary = monitor.stop()
    status_by_operation = {
        op: dict(Counter(str(item.get("status", 0)) for item in samples if item.get("op") == op))
        for op in sorted({str(item.get("op") or "") for item in samples})
    }
    if direct_ops:
        status_by_operation["direct_wallet_transfer"] = dict(direct_status_counts)

    payload = {
        "ok": not findings,
        "prefix": prefix,
        "fixture_usernames": [str(item.get("username") or "") for item in users],
        "base_url": args.base_url,
        "runtime_root": args.runtime_root,
        "database": str(database),
        "elapsed_seconds": round(time.perf_counter() - run_started, 3),
        "accounts_requested": int(args.accounts),
        "accounts_active": len(clients),
        "grant_points": int(args.grant_points),
        "transfer_ops_requested": int(args.transfer_ops),
        "direct_transfer_ops_requested": direct_ops,
        "direct_transfer_completed": direct_transfer_completed,
        "direct_transfer_errors": direct_transfer_errors,
        "external_transfer_count": external_transfer_count,
        "idempotency_probe": {
            "first_transaction_hash": str(dup_first.get("transaction_hash") or ""),
            "second_transaction_hash": str(dup_second.get("transaction_hash") or ""),
            "second_created": dup_second.get("created"),
        },
        "overspend_probe": {
            "attempt_count": len(overspend_results),
            "insufficient_balance_rejection_count": sum(
                1 for item in overspend_results if int(item.get("status") or 0) == 409
            ),
            "balance_before": rich_balance,
            "balance_after": rich_after_overspend.get("balance"),
        },
        "direct_latency": numeric_latency_summary(direct_latency_values),
        "direct_status_counts": dict(direct_status_counts),
        "forced_grants": forced_grants,
        "explorer_finalized_grants": explorer_finalized_grants,
        "forced_transfers": forced_transfers,
        "explorer_finalized_transfers": explorer_finalized,
        "latency": latency_summary(samples),
        "resource_monitor": resource_summary,
        "fee_market_samples": fee_market_samples,
        "status_by_operation": status_by_operation,
        "transport_retry_count": len([s for s in samples if s.get("transport_retried")]),
        "transport_retry_samples": [s for s in samples if s.get("transport_retried")][:20],
        "notification_checks": notification_checks,
        "db_counts": counts,
        "seal": seal,
        "verify": verify,
        "trading": {
            "market_symbol": symbol,
            "order_status": dict(Counter(str(item.get("status", 0)) for item in trading_results)),
            "margin_probe": margin_probe,
            "sitewide_refresh": trading_refresh,
            "sitewide_pools": trading_pools,
        },
        "management_snapshot_reads": management_snapshot_reads,
        "findings": findings,
        "sample_errors": ([s for s in samples if not s.get("expected", True) or int(s.get("status") or 0) >= 500] + direct_error_samples)[:100],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
