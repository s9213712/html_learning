#!/usr/bin/env python3
"""Playwright-backed trading background-engine correctness probe.

This script targets an isolated hackme_web dev/test runtime. It drives setup
through browser sessions, closes member sessions, then verifies that server-side
background jobs match orders, trigger TP/SL, run bots, accrue interest, and
liquidate margin positions. In ``--trigger-mode auto`` it does not call the root
run-once endpoint; it waits for server-owned workers to run without any browser
session being active.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.points_chain import BURN_WALLET_ADDRESS, PointsLedgerService, create_official_hot_wallet


DEFAULT_USER_PASSWORD = os.environ.get("HACKME_TRADING_PROBE_USER_PASSWORD") or secrets.token_urlsafe(24)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class Recorder:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, name: str, ok: bool, detail: str = "", **data: Any) -> None:
        self.checks.append(Check(name=name, ok=bool(ok), detail=detail, data=data))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    def require(self, name: str, ok: bool, detail: str = "", **data: Any) -> None:
        self.add(name, ok, detail, **data)
        if not ok:
            raise RuntimeError(f"{name}: {detail}")

    @property
    def failures(self) -> list[Check]:
        return [row for row in self.checks if not row.ok]


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def taiwan_id_number(index: int) -> str:
    letter_code = 10
    digits = [1, 2, 3, 4, 5, (index // 100) % 10, (index // 10) % 10, index % 10]
    weights = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    total = (letter_code // 10) * weights[0] + (letter_code % 10) * weights[1]
    for digit, weight in zip(digits, weights[2:10]):
        total += digit * weight
    check = (10 - (total % 10)) % 10
    return "A" + "".join(str(digit) for digit in digits) + str(check)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def db_one(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    conn = connect(db_path)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def db_all(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def db_exec(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def db_exec_many(db_path: Path, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
    conn = connect(db_path)
    try:
        for sql, params in statements:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


MUTATED_TRADING_SETTING_KEYS = (
    "trading.price_source",
    "trading.enabled",
    "trading.borrowing_enabled",
    "trading.margin_liquidation_enabled",
    "trading.bot_auto_scan_enabled",
    "trading.bot_audit_enabled",
    "trading.borrow_interest_percent_daily",
    "trading.borrow_apr_btc_eth_percent",
    "trading.borrow_apr_usdt_points_percent",
    "trading.borrow_interest_interval_hours",
    "trading.borrow_interest_minimum_hours",
    "trading.background_worker_dev_ready_enabled",
    "trading.qa_live_price_provider_enabled",
)
MUTATED_TRADING_MARKETS = ("ETH/POINTS", "BTC/POINTS")


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not table_exists(conn, table):
        return []
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def snapshot_row(conn: sqlite3.Connection, table: str, where: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    if not table_exists(conn, table):
        return None
    row = conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
    return dict(row) if row else None


def snapshot_rows(conn: sqlite3.Connection, table: str, key_column: str) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, table):
        return {}
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return {str(row[key_column]): dict(row) for row in rows}


def restore_row(conn: sqlite3.Connection, table: str, key_columns: tuple[str, ...], row: dict[str, Any]) -> None:
    columns = [column for column in table_columns(conn, table) if column in row]
    if not columns:
        return
    where_sql = " AND ".join(f"{column}=?" for column in key_columns)
    key_values = [row[column] for column in key_columns]
    exists = conn.execute(f"SELECT 1 FROM {table} WHERE {where_sql}", key_values).fetchone()
    if exists:
        update_columns = [column for column in columns if column not in key_columns]
        if not update_columns:
            return
        assignments = ", ".join(f"{column}=?" for column in update_columns)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {where_sql}",
            [row[column] for column in update_columns] + key_values,
        )
        return
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [row[column] for column in columns],
    )


def snapshot_trading_probe_runtime(db_path: Path) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        return {
            "settings": {
                key: snapshot_row(conn, "trading_settings", "key=?", (key,))
                for key in MUTATED_TRADING_SETTING_KEYS
            },
            "markets": {
                symbol: snapshot_row(conn, "trading_markets", "symbol=?", (symbol,))
                for symbol in MUTATED_TRADING_MARKETS
            },
            "registry": {
                symbol: snapshot_row(conn, "trading_markets_registry", "symbol=?", (symbol,))
                for symbol in MUTATED_TRADING_MARKETS
            },
            "background_jobs": snapshot_rows(conn, "trading_background_jobs", "job_key"),
            "background_locks": snapshot_rows(conn, "trading_background_locks", "job_key"),
            "background_queue": snapshot_rows(conn, "trading_background_job_queue", "id"),
        }
    finally:
        conn.close()


def restore_trading_probe_runtime(db_path: Path, snapshot: dict[str, Any]) -> None:
    conn = connect(db_path)
    try:
        for key, row in (snapshot.get("settings") or {}).items():
            if row:
                restore_row(conn, "trading_settings", ("key",), row)
            elif table_exists(conn, "trading_settings"):
                conn.execute("DELETE FROM trading_settings WHERE key=?", (key,))
        for symbol, row in (snapshot.get("markets") or {}).items():
            if row:
                restore_row(conn, "trading_markets", ("symbol",), row)
        for symbol, row in (snapshot.get("registry") or {}).items():
            if row:
                restore_row(conn, "trading_markets_registry", ("symbol",), row)
        for row in (snapshot.get("background_jobs") or {}).values():
            restore_row(conn, "trading_background_jobs", ("job_key",), row)
        for row in (snapshot.get("background_locks") or {}).values():
            restore_row(conn, "trading_background_locks", ("job_key",), row)
        for row in (snapshot.get("background_queue") or {}).values():
            restore_row(conn, "trading_background_job_queue", ("id",), row)
        conn.commit()
    finally:
        conn.close()


def api(page, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return page.evaluate(
        """
        async ({method, path, body}) => {
          const csrfRes = await fetch('/api/csrf-token', {credentials: 'same-origin'});
          const csrfJson = await csrfRes.json().catch(() => ({}));
          const token = csrfJson.csrf_token || '';
          const headers = {'X-CSRF-Token': token};
          const options = {method, credentials: 'same-origin', headers};
          if (body !== null && body !== undefined) {
            headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
          }
          const res = await fetch('/api' + path, options);
          const text = await res.text();
          let parsed = null;
          try { parsed = JSON.parse(text); } catch (_) { parsed = {raw: text}; }
          return {status: res.status, ok: res.ok, body: parsed, text};
        }
        """,
        {"method": method.upper(), "path": path, "body": body},
    )


def login(page, base_url: str, username: str, password: str) -> dict[str, Any]:
    last_error = ""
    for attempt in range(1, 4):
        try:
            page.goto(base_url + "/api/version", wait_until="domcontentloaded")
            res = api(page, "POST", "/login", {"username": username, "password": password})
            page.goto(base_url + "/api/version", wait_until="domcontentloaded")
            return res
        except Exception as exc:
            last_error = str(exc)
            if attempt >= 3:
                break
            time.sleep(0.5 * attempt)
    return {"status": 0, "ok": False, "body": {"ok": False, "error": last_error}, "text": last_error}


def assert_api_ok(rec: Recorder, name: str, res: dict[str, Any], *, statuses={200}, body_ok: bool | None = True) -> None:
    payload = res.get("body") if isinstance(res.get("body"), dict) else {}
    ok = int(res.get("status") or 0) in set(statuses)
    if body_ok is True:
        ok = ok and payload.get("ok") is True
    elif body_ok is False:
        ok = ok and payload.get("ok") is not True
    rec.require(name, ok, f"status={res.get('status')} body={json.dumps(payload, ensure_ascii=False)[:240]}")


def create_user(page, db_path: Path, username: str, index: int, *, password: str = DEFAULT_USER_PASSWORD) -> int:
    payload = {
        "username": username,
        "password": password,
        "password_confirm": password,
        "nickname": username,
        "real_name": f"Trading QA {index}",
        "id_number": taiwan_id_number(700 + index),
        "birthdate": "2000-01-01",
        "phone": f"09{index:08d}",
        "role": "user",
        "status": "active",
        "member_level": "trusted",
    }
    created = api(page, "POST", "/admin/users", payload)
    if int(created["status"]) not in {200, 409}:
        raise RuntimeError(f"create user failed {username}: {created}")
    row = db_one(db_path, "SELECT id FROM users WHERE username=?", (username,))
    if row:
        return int(row["id"])
    raise RuntimeError(f"created user not found in database: {username}")


def points_service(db_path: Path, runtime_dir: Path) -> PointsLedgerService:
    seed_path = runtime_dir / ".chain_seed"
    chain_seed = seed_path.read_text(encoding="utf-8").strip() if seed_path.exists() else "test-secret"

    def get_db():
        conn = connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    return PointsLedgerService(
        get_db=get_db,
        chain_secret=chain_seed,
        backup_dir=runtime_dir / "points_chain_backups",
    )


def actor_row(db_path: Path, *, username: str | None = None, role: str | None = None) -> dict[str, Any]:
    if username:
        row = db_one(
            db_path,
            "SELECT id, username, role FROM users WHERE username=? AND status='active' ORDER BY id LIMIT 1",
            (username,),
        )
    else:
        row = db_one(
            db_path,
            "SELECT id, username, role FROM users WHERE role=? AND status='active' ORDER BY id LIMIT 1",
            (role,),
        )
    if not row:
        raise RuntimeError(f"actor not found username={username!r} role={role!r}")
    return {"id": int(row["id"]), "username": str(row["username"]), "role": str(row["role"])}


def official_hot_wallet(service: PointsLedgerService, user_id: int) -> dict[str, Any]:
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        wallet = create_official_hot_wallet(conn, user_id=int(user_id), chain_secret=service.chain_secret)
        conn.commit()
        return dict(wallet)
    finally:
        conn.close()


def force_request_proved(service: PointsLedgerService, tx_hash: str) -> None:
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_transfer_requests SET created_at='2026-01-01T00:00:00Z' WHERE tx_group_hash=?",
            (tx_hash,),
        )
        conn.commit()
    finally:
        conn.close()


def set_governance_timelock_ready(service: PointsLedgerService, proposal_uuid: str) -> None:
    ready_at = "2026-01-01T00:00:00Z"
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (proposal_uuid,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"governance proposal not found: {proposal_uuid}")
        payload = json.loads(row["payload_json"] or "{}")
        guard = payload.get("execution_guard") if isinstance(payload, dict) else None
        if isinstance(guard, dict):
            guard["timelock_until"] = ready_at
            guard["timelock_ends_at"] = ready_at
        execution_payload_hash = service._governance_execution_payload_hash(
            action_type=row["action_type"],
            governance_domain=row["governance_domain"],
            target_wallet_address=row["target_wallet_address"],
            target_address=row["target_address"],
            target_branch=row["target_branch"],
            requested_amount=row["requested_amount"],
            requested_asset=row["requested_asset"],
            payload=payload,
        )
        conn.execute(
            """
            UPDATE points_chain_governance_proposals
            SET timelock_until=?, timelock_ends_at=?, payload_json=?, execution_payload_hash=?
            WHERE proposal_uuid=?
            """,
            (
                ready_at,
                ready_at,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                execution_payload_hash,
                proposal_uuid,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def governed_treasury_grant(
    db_path: Path,
    runtime_dir: Path,
    *,
    user_id: int,
    amount: int,
    request_uuid: str,
) -> dict[str, Any]:
    service = points_service(db_path, runtime_dir)
    root = actor_row(db_path, username="root")
    manager = actor_row(db_path, role="manager")
    destination_wallet = official_hot_wallet(service, user_id)
    root_wallet = official_hot_wallet(service, root["id"])
    manager_wallet = official_hot_wallet(service, manager["id"])
    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination_wallet["address"],
        amount=int(amount),
        reason="playwright trading background QA seed",
        reference=request_uuid,
        action_type="TREASURY_TRANSFER",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    set_governance_timelock_ready(service, proposal_uuid)
    service.sign_governance_multisig(
        actor=root,
        proposal_uuid=proposal_uuid,
        signer_wallet_address=root_wallet["address"],
    )
    signed = service.sign_governance_multisig(
        actor=manager,
        proposal_uuid=proposal_uuid,
        signer_wallet_address=manager_wallet["address"],
    )
    if signed.get("multisig", {}).get("ready") is not True:
        raise RuntimeError(f"treasury multisig not ready: {signed}")
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    transfer = executed["result"]["transfer"]
    proved = service.explorer_transaction(transfer["transaction_hash"])["transaction"]
    if (proved.get("finality") or {}).get("finality_status") == "pending":
        force_request_proved(service, transfer["transaction_hash"])
        proved = service.explorer_transaction(transfer["transaction_hash"])["transaction"]
    return {"proposal_uuid": proposal_uuid, "transfer": transfer, "proved": proved}


def is_settled_for_background_seed(transaction: dict[str, Any]) -> bool:
    if not transaction or transaction.get("status") != "confirmed":
        return False
    finality = transaction.get("finality") or {}
    status = str(finality.get("finality_status") or "")
    settlement_rail = str(transaction.get("settlement_rail") or transaction.get("input_data", {}).get("settlement_rail") or "")
    chain_required = bool(transaction.get("chain_required", True))
    if not chain_required or settlement_rail in {"internal_hot_wallet", "internal_system_burn"}:
        return status == "internal_settled"
    return status in {"proved", "sealed"}


def direct_prepare_market(db_path: Path, *, price: int) -> None:
    now = utc_now()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.price_source", "test_live_price_provider", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.enabled", "true", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.borrowing_enabled", "true", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.margin_liquidation_enabled", "true", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.bot_auto_scan_enabled", "true", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.bot_audit_enabled", "true", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.borrow_interest_percent_daily", "24", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.borrow_apr_btc_eth_percent", "8760", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.borrow_apr_usdt_points_percent", "8760", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.borrow_interest_interval_hours", "1", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.borrow_interest_minimum_hours", "1", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.background_worker_dev_ready_enabled", "true", now, 0),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("trading.qa_live_price_provider_enabled", "true", now, 0),
        ),
        (
            "UPDATE trading_markets SET manual_price_points=?, price_source='test_live_price_provider', max_price_jump_percent=1000000, fee_rate_percent=0.3, min_order_points=1, max_order_points=1000000, enabled=1, spot_enabled=1, live_price_confirmed_at=COALESCE(live_price_confirmed_at, ?), updated_at=? WHERE symbol='ETH/POINTS'",
            (int(price), now, now),
        ),
        (
            "UPDATE trading_markets SET manual_price_points=?, price_source='test_live_price_provider', max_price_jump_percent=1000000, fee_rate_percent=0.3, min_order_points=1, max_order_points=1000000, enabled=1, spot_enabled=1, live_price_confirmed_at=COALESCE(live_price_confirmed_at, ?), updated_at=? WHERE symbol='BTC/POINTS'",
            (int(price) * 10, now, now),
        ),
        (
            "UPDATE trading_markets_registry SET enabled=1, allow_margin=1, allow_bots=1, allow_risk_grade_usage=1, live_price_enabled=1, reference_price_enabled=1, updated_at=? WHERE symbol IN ('ETH/POINTS', 'BTC/POINTS')",
            (now,),
        ),
    ]
    db_exec_many(db_path, statements)


def set_price_and_due_jobs(db_path: Path, price: int, job_keys: list[str], *, risk_grade: bool = False) -> None:
    now = utc_now()
    price_source = "test_live_price_provider"
    statements = [
        (
            "UPDATE trading_markets SET manual_price_points=?, price_source=?, max_price_jump_percent=1000000, updated_at=?, live_price_confirmed_at=COALESCE(live_price_confirmed_at, ?) WHERE symbol='ETH/POINTS'",
            (int(price), price_source, now, now),
        ),
        (
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES ('trading.price_source', ?, ?, 0)",
            (price_source, now),
        ),
    ]
    for key in job_keys:
        statements.append(
            (
                "UPDATE trading_background_jobs SET enabled=1, interval_seconds=1, next_run_at=NULL, lease_until=NULL, lease_owner=NULL, updated_at=? WHERE job_key=?",
                (now, key),
            )
        )
    db_exec_many(db_path, statements)


def configure_background_jobs(db_path: Path, *, enabled: bool) -> None:
    now = utc_now()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            "UPDATE trading_background_jobs SET enabled=?, interval_seconds=1, next_run_at=NULL, lease_until=NULL, lease_owner=NULL, updated_at=?",
            (1 if enabled else 0, now),
        ),
    ]
    if not enabled:
        statements.extend([
            ("DELETE FROM trading_background_locks", ()),
            ("DELETE FROM trading_background_job_queue WHERE status IN ('queued', 'running')", ()),
        ])
    db_exec_many(db_path, statements)


def deplete_trial_credits(db_path: Path, user_ids: list[int]) -> None:
    now = utc_now()
    statements = []
    for user_id in user_ids:
        statements.append(
            (
                """
                INSERT OR REPLACE INTO trading_trial_credits (
                    user_id, initial_points, available_points, locked_points, deployed_points,
                    status, activated_at, expires_at, updated_at
                ) VALUES (?, 0, 0, 0, 0, 'depleted', ?, ?, ?)
                """,
                (int(user_id), now, now, now),
            )
        )
    db_exec_many(db_path, statements)


def drain_hot_wallet_for_margin_liquidation(
    db_path: Path,
    runtime_dir: Path,
    *,
    user_id: int,
    username: str,
    keep_points: int = 5,
    request_uuid: str,
) -> dict[str, Any]:
    service = points_service(db_path, runtime_dir)
    wallet = official_hot_wallet(service, user_id)
    actor = actor_row(db_path, username=username)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        available = int(
            service._wallet_identity_available_for_address(
                conn,
                user_id=int(user_id),
                address=wallet["address"],
            )
            or 0
        )
    finally:
        conn.close()
    drain_amount = max(0, available - max(0, int(keep_points)))
    if drain_amount <= 0:
        return {"ok": True, "drained_points": 0, "available_before": available, "wallet": wallet}
    result = service.submit_wallet_transaction(
        actor=actor,
        source_wallet_address=wallet["address"],
        destination_wallet_address=BURN_WALLET_ADDRESS,
        amount_points=drain_amount,
        fee_points=0,
        request_uuid=request_uuid,
        memo="QA deterministic cross-margin drain before liquidation probe",
    )
    tx = result.get("transaction") or {}
    return {
        "ok": bool(result.get("ok")) and tx.get("status") == "confirmed",
        "drained_points": drain_amount,
        "available_before": available,
        "wallet": wallet,
        "transaction": tx,
    }


def wait_until(rec: Recorder, name: str, predicate, *, timeout: float = 15.0, interval: float = 0.25) -> Any:
    deadline = time.time() + timeout
    last_value = None
    while time.time() < deadline:
        last_value = predicate()
        if last_value:
            rec.add(name, True, str(last_value)[:260])
            return last_value
        time.sleep(interval)
    rec.add(name, False, f"timeout after {timeout}s last={last_value}")
    raise RuntimeError(name)


def background_run_marker(db_path: Path) -> int:
    return int(db_one(db_path, "SELECT COALESCE(MAX(id), 0) AS id FROM trading_background_job_runs")["id"] or 0)


def trigger_background_jobs(
    page,
    db_path: Path,
    rec: Recorder,
    job_keys: list[str],
    *,
    label: str,
    trigger_mode: str,
    before: int | None = None,
) -> None:
    before = background_run_marker(db_path) if before is None else int(before)
    if trigger_mode == "run-once":
        if page is None:
            raise RuntimeError("run-once trigger mode requires a root browser page")
        for job_key in job_keys:
            res = api(
                page,
                "POST",
                "/root/trading/background/run-once",
                {"job_key": job_key, "confirm": "RUN_TRADING_JOB_ONCE"},
            )
            assert_api_ok(rec, f"{label} enqueue {job_key}", res, statuses={202})
    elif trigger_mode == "auto":
        rec.add(
            f"{label} automatic scheduler armed",
            True,
            f"waiting for server worker to run {job_keys} after run_id>{before}",
        )
    else:
        raise RuntimeError(f"unknown trigger mode: {trigger_mode}")

    def all_jobs_finished() -> bool:
        rows = db_all(
            db_path,
            """
            SELECT job_key, status, error
            FROM trading_background_job_runs
            WHERE id>? AND job_key IN (%s)
            ORDER BY id ASC
            """ % ",".join("?" for _ in job_keys),
            (before, *job_keys),
        )
        latest = {str(row["job_key"]): row for row in rows}
        if not all(job_key in latest for job_key in job_keys):
            return False
        failed = [dict(row) for row in latest.values() if row["status"] not in {"success", "queued", "running"}]
        if failed:
            raise RuntimeError(f"{label} background job failed: {failed}")
        return all(str(latest[job_key]["status"]) == "success" for job_key in job_keys)

    wait_until(
        rec,
        f"{label} background {trigger_mode} jobs finished",
        all_jobs_finished,
        timeout=30,
    )


def user_page(browser, base_url: str, username: str, password: str):
    ctx = browser.new_context(ignore_https_errors=True)
    page = ctx.new_page()
    res = login(page, base_url, username, password)
    if int(res["status"]) != 200 or res.get("body", {}).get("ok") is not True:
        ctx.close()
        raise RuntimeError(f"login failed for {username}: {res}")
    return ctx, page


def run_stress_burst(page, count: int) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        async ({count}) => {
          async function one(i) {
            const csrfRes = await fetch('/api/csrf-token', {credentials: 'same-origin'});
            const csrfJson = await csrfRes.json().catch(() => ({}));
            const res = await fetch('/api/trading/orders', {
              method: 'POST',
              credentials: 'same-origin',
              headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfJson.csrf_token || ''},
              body: JSON.stringify({
                market_symbol: 'ETH/POINTS',
                side: 'buy',
                order_type: 'market',
                quantity: '0.01'
              })
            });
            const text = await res.text();
            let body = {};
            try { body = JSON.parse(text); } catch (_) { body = {raw: text}; }
            return {index: i, status: res.status, ok: body.ok === true, body};
          }
          return Promise.all(Array.from({length: count}, (_, i) => one(i)));
        }
        """,
        {"count": int(count)},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Playwright trading background correctness QA")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runtime-dir", required=True)
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--user-password", default=DEFAULT_USER_PASSWORD)
    parser.add_argument("--out", default="")
    parser.add_argument("--stress-orders", type=int, default=30)
    parser.add_argument("--trigger-mode", choices=("auto", "run-once"), default="auto")
    parser.add_argument(
        "--keep-mutated-settings",
        action="store_true",
        help="Do not restore trading settings/market prices/background job schedules after the probe. Intended only for disposable runtimes.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    db_path = runtime_dir / "database" / "database.db"
    out_dir = Path(args.out).expanduser().resolve() if args.out else runtime_dir / "reports" / "qa" / f"trading_background_{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)

    rec = Recorder()
    scenario: dict[str, Any] = {"db_path": str(db_path), "base_url": base_url, "trigger_mode": args.trigger_mode, "users": {}}
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    runtime_snapshot = snapshot_trading_probe_runtime(db_path)
    restore_state = {"done": False}

    def restore_probe_runtime_once() -> None:
        if restore_state["done"] or args.keep_mutated_settings:
            return
        restore_trading_probe_runtime(db_path, runtime_snapshot)
        restore_state["done"] = True
        scenario["runtime_settings_restored"] = True

    atexit.register(restore_probe_runtime_once)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        root_ctx = browser.new_context(ignore_https_errors=True)
        root_page = root_ctx.new_page()
        root_login = login(root_page, base_url, "root", args.root_password)
        assert_api_ok(rec, "root login", root_login)
        feature_snapshot_response = api(root_page, "GET", "/admin/features")
        assert_api_ok(rec, "feature flag snapshot before trading probe", feature_snapshot_response)
        feature_snapshot = {
            key: bool((feature_snapshot_response.get("body") or {}).get("features", {}).get(key))
            for key in (
                "feature_economy_enabled",
                "feature_trading_enabled",
                "feature_reports_notifications_enabled",
            )
        }
        scenario["feature_flags_before"] = feature_snapshot
        assert_api_ok(
            rec,
            "enable feature flags",
            api(
                root_page,
                "PUT",
                "/admin/features",
                {
                    "feature_economy_enabled": True,
                    "feature_trading_enabled": True,
                    "feature_reports_notifications_enabled": True,
                },
            ),
        )
        assert_api_ok(rec, "background status API initializes schema", api(root_page, "GET", "/root/trading/background/status?limit=20"))
        configure_background_jobs(db_path, enabled=False)
        direct_prepare_market(db_path, price=100)
        rec.add("direct deterministic market setup", True, "ETH/POINTS manual test price=100 with background jobs paused")

        prefix = f"qa_bg_{int(time.time())}_"
        users = {
            "spot_sl": f"{prefix}spot_sl",
            "spot_tp": f"{prefix}spot_tp",
            "limit": f"{prefix}limit",
            "margin_liq": f"{prefix}margin_liq",
            "margin_tp": f"{prefix}margin_tp",
            "margin_interest": f"{prefix}margin_interest",
            "workflow_bot": f"{prefix}workflow",
            "dca_bot": f"{prefix}dca",
            "grid_bot": f"{prefix}grid",
            "stress_a": f"{prefix}stress_a",
            "stress_b": f"{prefix}stress_b",
        }
        user_ids: dict[str, int] = {}
        for index, (role, username) in enumerate(users.items(), start=1):
            user_id = create_user(root_page, db_path, username, index, password=args.user_password)
            user_ids[role] = user_id
            seed_points = 60 if role == "margin_liq" else 50000
            grant = governed_treasury_grant(
                db_path,
                runtime_dir,
                user_id=user_id,
                amount=seed_points,
                request_uuid=f"{prefix}{role}_treasury_seed",
            )
            rec.require(
                f"fund {role} through governed treasury grant",
                is_settled_for_background_seed(grant["proved"]),
                json.dumps(
                    {
                        "proposal_uuid": grant["proposal_uuid"],
                        "transaction_hash": grant["transfer"]["transaction_hash"],
                        "settlement_rail": grant["proved"].get("settlement_rail"),
                        "chain_required": grant["proved"].get("chain_required"),
                        "finality_status": (grant["proved"].get("finality") or {}).get("finality_status"),
                    },
                    ensure_ascii=False,
                ),
            )
        scenario["users"] = {role: {"username": users[role], "id": user_ids[role]} for role in users}
        deplete_trial_credits(db_path, list(user_ids.values()))
        rec.add("trial credits depleted for deterministic PointsChain funding", True, f"users={len(user_ids)}")

        root_report_before = api(root_page, "GET", "/admin/trading/report")
        assert_api_ok(rec, "root trading report before scenario", root_report_before)
        reserve_before = int(((root_report_before["body"].get("report") or {}).get("reserve_pool") or {}).get("balance_points") or 0)
        scenario["reserve_before"] = reserve_before

        member_contexts = []
        pages: dict[str, Any] = {}
        for role, username in users.items():
            ctx, page = user_page(browser, base_url, username, args.user_password)
            member_contexts.append(ctx)
            pages[role] = page

        # Browser/UI sanity for member trading surface before closing sessions.
        pages["spot_sl"].goto(base_url + "/", wait_until="domcontentloaded")
        pages["spot_sl"].wait_for_function(
            "() => !!document.querySelector('#trading-order-form') && !!document.querySelector('#trading-submit-order-btn')",
            timeout=5000,
        )
        ui_state = pages["spot_sl"].evaluate(
            """
            () => {
              if (typeof window.switchModuleTab === 'function') window.switchModuleTab('trading');
              else document.querySelector('#tab-module-trading')?.click();
              const form = document.querySelector('#trading-order-form');
              const submit = document.querySelector('#trading-submit-order-btn');
              const module = document.querySelector('#module-trading');
              return {
                form_present: !!form,
                submit_present: !!submit,
                module_present: !!module,
                module_active: !!module?.classList?.contains('active'),
              };
            }
            """
        )
        rec.require(
            "member trading UI loaded",
            bool(ui_state.get("form_present") and ui_state.get("submit_present") and ui_state.get("module_present")),
            json.dumps(ui_state, ensure_ascii=False),
        )

        orders: dict[str, Any] = {}
        orders["spot_sl_buy"] = api(
            pages["spot_sl"],
            "POST",
            "/trading/orders",
            {"market_symbol": "ETH/POINTS", "side": "buy", "order_type": "market", "quantity": "10", "stop_loss_percent": 5, "take_profit_percent": 20},
        )
        assert_api_ok(rec, "spot stop-loss seed buy", orders["spot_sl_buy"])
        orders["spot_tp_buy"] = api(
            pages["spot_tp"],
            "POST",
            "/trading/orders",
            {"market_symbol": "ETH/POINTS", "side": "buy", "order_type": "market", "quantity": "10", "stop_loss_percent": 20, "take_profit_percent": 5},
        )
        assert_api_ok(rec, "spot take-profit seed buy", orders["spot_tp_buy"])
        orders["limit_buy"] = api(
            pages["limit"],
            "POST",
            "/trading/orders",
            {"market_symbol": "ETH/POINTS", "side": "buy", "order_type": "limit", "quantity": "5", "limit_price_points": 92},
        )
        assert_api_ok(rec, "limit order seed", orders["limit_buy"])
        orders["margin_liq_open"] = api(
            pages["margin_liq"],
            "POST",
            "/trading/margin/open",
            {"market_symbol": "ETH/POINTS", "position_type": "margin_long", "quantity": "1", "collateral_points": 50, "idempotency_key": f"{prefix}margin_liq"},
        )
        assert_api_ok(rec, "margin liquidation seed open", orders["margin_liq_open"])
        drain = drain_hot_wallet_for_margin_liquidation(
            db_path,
            runtime_dir,
            user_id=user_ids["margin_liq"],
            username=users["margin_liq"],
            keep_points=5,
            request_uuid=f"{prefix}margin_liq_drain",
        )
        rec.require(
            "margin liquidation seed wallet drained through pc0 burn",
            bool(drain.get("ok")),
            f"available_before={drain.get('available_before')} drained={drain.get('drained_points')}",
            drain=drain,
        )
        orders["margin_tp_open"] = api(
            pages["margin_tp"],
            "POST",
            "/trading/margin/open",
            {"market_symbol": "ETH/POINTS", "position_type": "margin_long", "quantity": "1", "collateral_points": 50, "take_profit_percent": 5, "idempotency_key": f"{prefix}margin_tp"},
        )
        assert_api_ok(rec, "margin take-profit seed open", orders["margin_tp_open"])
        orders["margin_interest_open"] = api(
            pages["margin_interest"],
            "POST",
            "/trading/margin/open",
            {"market_symbol": "ETH/POINTS", "position_type": "margin_long", "quantity": "1", "collateral_points": 90, "idempotency_key": f"{prefix}margin_interest"},
        )
        assert_api_ok(rec, "margin interest seed open", orders["margin_interest_open"])
        interest_uuid = (orders["margin_interest_open"]["body"].get("position") or {}).get("position_uuid")
        opened_at = (datetime.utcnow().replace(microsecond=0) - timedelta(hours=3, minutes=2)).isoformat()
        db_exec(
            db_path,
            "UPDATE trading_margin_positions SET opened_at=?, interest_accrued_hours=0, interest_points=0, interest_paid_points=0, interest_carry_micropoints=0 WHERE position_uuid=?",
            (opened_at, interest_uuid),
        )
        workflow_bot_create = api(
            pages["workflow_bot"],
            "POST",
            "/trading/bots",
            {
                "bot_type": "conditional",
                "name": f"{prefix}conditional",
                "market_symbol": "ETH/POINTS",
                "side": "buy",
                "order_type": "market",
                "quantity": "1",
                "trigger_type": "always",
                "trigger_price_points": 0,
                "max_runs": 1,
                "cooldown_seconds": 0,
            },
        )
        assert_api_ok(rec, "workflow/conditional bot seed", workflow_bot_create)
        dca_bot_create = api(
            pages["dca_bot"],
            "POST",
            "/trading/bots",
            {
                "bot_type": "dca",
                "name": f"{prefix}dca",
                "market_symbol": "ETH/POINTS",
                "budget_points": 100,
                "interval_hours": 1,
                "max_runs": 1,
                "enabled": False,
            },
        )
        assert_api_ok(rec, "DCA bot seed disabled before auto scan", dca_bot_create)
        grid_bot_create = api(
            pages["grid_bot"],
            "POST",
            "/trading/grid-bots",
            {
                "name": f"{prefix}grid",
                "market_symbol": "ETH/POINTS",
                "upper_price_points": 100,
                "lower_price_points": 80,
                "grid_count": 3,
                "order_amount_points": 100,
                "confirm_thin_profit": True,
            },
        )
        assert_api_ok(rec, "grid bot seed", grid_bot_create)
        dca_uuid = (dca_bot_create["body"].get("bot") or {}).get("bot_uuid")
        db_exec(
            db_path,
            "UPDATE trading_bots SET enabled=1, enabled_at=?, last_run_at=NULL, last_error='', updated_at=? WHERE bot_uuid=?",
            (utc_now(), utc_now(), dca_uuid),
        )
        rec.add("DCA bot armed for background scan without initial route run", True, f"bot_uuid={dca_uuid}")
        scenario["uuids"] = {
            "limit_order": (orders["limit_buy"]["body"].get("order") or {}).get("order_uuid"),
            "margin_liq": (orders["margin_liq_open"]["body"].get("position") or {}).get("position_uuid"),
            "margin_tp": (orders["margin_tp_open"]["body"].get("position") or {}).get("position_uuid"),
            "margin_interest": interest_uuid,
            "workflow_bot": (workflow_bot_create["body"].get("bot") or {}).get("bot_uuid"),
            "dca_bot": dca_uuid,
            "grid_bot": (grid_bot_create["body"].get("bot") or {}).get("bot_uuid"),
        }

        for ctx in member_contexts:
            ctx.close()
        scenario["member_contexts_closed_before_background"] = True
        rec.add("all setup member browser sessions closed before background trigger", True, "no active member Playwright browser context remains")
        if args.trigger_mode == "auto":
            root_ctx.close()
            root_page = None
            scenario["root_context_closed_before_background"] = True
            rec.add("root browser session closed before automatic background trigger", True, "server worker must run without a logged-in browser session")

        # Stage 1: after member browsers close, server-side jobs must run.
        # Price drop should match the limit order, trigger spot stop-loss, and
        # trigger all three bot families.
        stage_1_jobs = ["price_refresh", "order_matching", "take_profit_stop_loss_scan", "bot_trigger_scan", "interest_accrual"]
        stage_1_before = background_run_marker(db_path)
        set_price_and_due_jobs(db_path, 89, stage_1_jobs)
        trigger_background_jobs(
            root_page,
            db_path,
            rec,
            stage_1_jobs,
            label="stage 1 price drop",
            trigger_mode=args.trigger_mode,
            before=stage_1_before,
        )
        wait_until(
            rec,
            "background matched limit order without active member browser",
            lambda: db_one(db_path, "SELECT status FROM trading_orders WHERE order_uuid=?", (scenario["uuids"]["limit_order"],))["status"] == "filled",
            timeout=20,
        )
        wait_until(
            rec,
            "background triggered spot stop-loss without active member browser",
            lambda: int(db_one(db_path, "SELECT quantity_units FROM trading_spot_positions WHERE user_id=? AND market_symbol='ETH/POINTS'", (user_ids["spot_sl"],))["quantity_units"] or 0) == 0,
            timeout=20,
        )
        wait_until(
            rec,
            "background triggered workflow/conditional bot without active browser",
            lambda: int(
                db_one(
                    db_path,
                    """
                    SELECT COUNT(*) AS c
                    FROM trading_bot_runs r
                    JOIN trading_bots b ON b.id=r.bot_id
                    WHERE b.bot_uuid=? AND r.status='triggered' AND COALESCE(r.order_uuid, '')<>''
                    """,
                    (scenario["uuids"]["workflow_bot"],),
                )["c"]
                or 0
            )
            >= 1,
            timeout=20,
        )
        wait_until(
            rec,
            "background triggered DCA bot without active browser",
            lambda: int(
                db_one(
                    db_path,
                    """
                    SELECT COUNT(*) AS c
                    FROM trading_bot_runs r
                    JOIN trading_bots b ON b.id=r.bot_id
                    WHERE b.bot_uuid=? AND b.bot_type='dca' AND r.status='triggered' AND COALESCE(r.order_uuid, '')<>''
                    """,
                    (scenario["uuids"]["dca_bot"],),
                )["c"]
                or 0
            )
            >= 1,
            timeout=20,
        )
        wait_until(
            rec,
            "background scanned grid bot and filled crossed grid order without active browser",
            lambda: int(
                db_one(
                    db_path,
                    """
                    SELECT COUNT(*) AS c
                    FROM trading_grid_orders
                    WHERE grid_bot_id=(SELECT id FROM trading_grid_bots WHERE bot_uuid=?)
                      AND status='filled'
                    """,
                    (scenario["uuids"]["grid_bot"],),
                )["c"]
                or 0
            )
            >= 1,
            timeout=20,
        )
        wait_until(
            rec,
            "background accrued margin interest without active member browser",
            lambda: int(db_one(db_path, "SELECT interest_accrued_hours FROM trading_margin_positions WHERE position_uuid=?", (interest_uuid,))["interest_accrued_hours"] or 0) >= 3,
            timeout=20,
        )

        # Stage 2: price rebound should trigger spot TP and margin TP.
        stage_2_jobs = ["price_refresh", "take_profit_stop_loss_scan"]
        stage_2_before = background_run_marker(db_path)
        set_price_and_due_jobs(db_path, 106, stage_2_jobs)
        trigger_background_jobs(
            root_page,
            db_path,
            rec,
            stage_2_jobs,
            label="stage 2 price rebound",
            trigger_mode=args.trigger_mode,
            before=stage_2_before,
        )
        wait_until(
            rec,
            "background triggered spot take-profit without active member browser",
            lambda: int(db_one(db_path, "SELECT quantity_units FROM trading_spot_positions WHERE user_id=? AND market_symbol='ETH/POINTS'", (user_ids["spot_tp"],))["quantity_units"] or 0) == 0,
            timeout=20,
        )
        wait_until(
            rec,
            "background triggered margin take-profit without active member browser",
            lambda: db_one(db_path, "SELECT status FROM trading_margin_positions WHERE position_uuid=?", (scenario["uuids"]["margin_tp"],))["status"] == "closed",
            timeout=20,
        )

        # Stage 3: crash price should liquidate the weak margin account.
        stage_3_jobs = ["price_refresh", "margin_liquidation_scan"]
        stage_3_before = background_run_marker(db_path)
        set_price_and_due_jobs(db_path, 30, stage_3_jobs, risk_grade=True)
        trigger_background_jobs(
            root_page,
            db_path,
            rec,
            stage_3_jobs,
            label="stage 3 crash",
            trigger_mode=args.trigger_mode,
            before=stage_3_before,
        )
        wait_until(
            rec,
            "background liquidated margin account without active member browser",
            lambda: db_one(db_path, "SELECT status FROM trading_margin_positions WHERE position_uuid=?", (scenario["uuids"]["margin_liq"],))["status"] == "liquidated",
            timeout=20,
        )

        # Re-login only after the no-browser background checks completed, then
        # inspect root UI/API and run a Playwright-driven stress burst.
        root_ctx2 = browser.new_context(ignore_https_errors=True)
        root_page2 = root_ctx2.new_page()
        assert_api_ok(rec, "root re-login after background run", login(root_page2, base_url, "root", args.root_password))
        root_page2.goto(base_url + "/", wait_until="domcontentloaded")
        root_page2.evaluate(
            """
            () => {
              if (typeof switchModuleTab === 'function') switchModuleTab('server');
              if (typeof switchServerTab === 'function') switchServerTab('settings');
              if (typeof switchSettingsSection === 'function') switchSettingsSection('trading');
            }
            """
        )
        root_page2.wait_for_selector("#root-trading-background-panel", state="attached", timeout=5000)
        root_ui_state = root_page2.evaluate(
            """
            () => ({
              panel: !!document.querySelector('#root-trading-background-panel'),
              summary: !!document.querySelector('#root-trading-background-summary'),
              jobs: !!document.querySelector('#root-trading-background-jobs'),
              runs: !!document.querySelector('#root-trading-background-runs'),
            })
            """
        )
        rec.require(
            "root background UI panel wired",
            all(root_ui_state.values()),
            json.dumps(root_ui_state, ensure_ascii=False),
        )
        bg_status = api(root_page2, "GET", "/root/trading/background/status?limit=30")
        assert_api_ok(rec, "root background status API after no-login jobs", bg_status)
        bg_jobs = bg_status["body"].get("jobs") or []
        recent_runs = bg_status["body"].get("recent_runs") or []
        scenario["background_terminal"] = {
            "recent_job_keys": sorted({str(row.get("job_key") or "") for row in recent_runs if row.get("job_key")}),
            "failure_counts": {
                str(row.get("job_key") or ""): int(row.get("failure_count") or 0)
                for row in bg_jobs if row.get("job_key")
            },
        }
        rec.require(
            "background job run log contains expected jobs",
            {"order_matching", "take_profit_stop_loss_scan", "bot_trigger_scan", "margin_liquidation_scan", "interest_accrual"}.issubset({row.get("job_key") for row in recent_runs}),
            f"recent={[row.get('job_key') for row in recent_runs[:12]]}",
        )
        rec.require(
            "background jobs have no recorded failures",
            all(int(row.get("failure_count") or 0) == 0 for row in bg_jobs),
            json.dumps([{row.get("job_key"): row.get("failure_count")} for row in bg_jobs], ensure_ascii=False),
        )

        stress_contexts = []
        stress_results: list[dict[str, Any]] = []
        set_price_and_due_jobs(db_path, 100, ["price_refresh"])
        for role in ("stress_a", "stress_b"):
            ctx, page = user_page(browser, base_url, users[role], args.user_password)
            stress_contexts.append(ctx)
            stress_results.extend(run_stress_burst(page, max(1, int(args.stress_orders))))
        for ctx in stress_contexts:
            ctx.close()
        no_5xx = all(int(row.get("status") or 0) < 500 for row in stress_results)
        success_count = sum(1 for row in stress_results if row.get("ok"))
        scenario["concurrent_stress"] = {
            "requested_per_user": max(1, int(args.stress_orders)),
            "request_count": len(stress_results),
            "success_count": success_count,
            "statuses": sorted({int(row.get("status") or 0) for row in stress_results}),
            "no_5xx": no_5xx,
        }
        rec.require(
            "Playwright concurrent order stress has no 5xx and produces fills",
            no_5xx and success_count > 0,
            f"requests={len(stress_results)} success={success_count} statuses={sorted({row.get('status') for row in stress_results})}",
        )

        verify_trading = api(root_page2, "GET", "/root/trading/verify")
        assert_api_ok(rec, "trading verify_state after background/stress", verify_trading, statuses={200, 202})
        verify_chain = api(root_page2, "GET", "/root/points/chain/verify")
        assert_api_ok(rec, "PointsChain verify after background/stress", verify_chain, statuses={200, 202})
        root_report_after = api(root_page2, "GET", "/admin/trading/report")
        assert_api_ok(rec, "root trading report after scenario", root_report_after)
        reserve_after = int(((root_report_after["body"].get("report") or {}).get("reserve_pool") or {}).get("balance_points") or 0)
        scenario["reserve_after"] = reserve_after
        bad_wallets = [
            dict(row)
            for row in db_all(
                db_path,
                """
                SELECT user_id,
                       soft_balance + hard_balance AS points_balance,
                       soft_frozen + hard_frozen AS points_frozen
                FROM points_wallets
                WHERE soft_balance + hard_balance < 0
                   OR soft_frozen + hard_frozen < 0
                """,
            )
        ]
        bad_margin_locks = [
            dict(row)
            for row in db_all(
                db_path,
                "SELECT user_id, market_symbol, locked_quantity_units FROM trading_spot_positions WHERE locked_quantity_units < 0",
            )
        ]
        scenario["domain_terminal"] = {
            "limit_order_status": str(db_one(
                db_path,
                "SELECT status FROM trading_orders WHERE order_uuid=?",
                (scenario["uuids"]["limit_order"],),
            )["status"]),
            "margin_liquidation_status": str(db_one(
                db_path,
                "SELECT status FROM trading_margin_positions WHERE position_uuid=?",
                (scenario["uuids"]["margin_liq"],),
            )["status"]),
            "margin_take_profit_status": str(db_one(
                db_path,
                "SELECT status FROM trading_margin_positions WHERE position_uuid=?",
                (scenario["uuids"]["margin_tp"],),
            )["status"]),
            "margin_interest_hours": int(db_one(
                db_path,
                "SELECT interest_accrued_hours FROM trading_margin_positions WHERE position_uuid=?",
                (scenario["uuids"]["margin_interest"],),
            )["interest_accrued_hours"] or 0),
            "spot_stop_loss_quantity_units": int(db_one(
                db_path,
                "SELECT quantity_units FROM trading_spot_positions WHERE user_id=? AND market_symbol='ETH/POINTS'",
                (user_ids["spot_sl"],),
            )["quantity_units"] or 0),
            "spot_take_profit_quantity_units": int(db_one(
                db_path,
                "SELECT quantity_units FROM trading_spot_positions WHERE user_id=? AND market_symbol='ETH/POINTS'",
                (user_ids["spot_tp"],),
            )["quantity_units"] or 0),
            "workflow_bot_triggered_runs": int(db_one(
                db_path,
                "SELECT COUNT(*) AS c FROM trading_bot_runs r JOIN trading_bots b ON b.id=r.bot_id WHERE b.bot_uuid=? AND r.status='triggered' AND COALESCE(r.order_uuid, '')<>''",
                (scenario["uuids"]["workflow_bot"],),
            )["c"] or 0),
            "dca_bot_triggered_runs": int(db_one(
                db_path,
                "SELECT COUNT(*) AS c FROM trading_bot_runs r JOIN trading_bots b ON b.id=r.bot_id WHERE b.bot_uuid=? AND r.status='triggered' AND COALESCE(r.order_uuid, '')<>''",
                (scenario["uuids"]["dca_bot"],),
            )["c"] or 0),
            "grid_filled_orders": int(db_one(
                db_path,
                "SELECT COUNT(*) AS c FROM trading_grid_orders WHERE grid_bot_id=(SELECT id FROM trading_grid_bots WHERE bot_uuid=?) AND status='filled'",
                (scenario["uuids"]["grid_bot"],),
            )["c"] or 0),
            "negative_wallet_rows": bad_wallets,
            "negative_spot_lock_rows": bad_margin_locks,
            "reserve_before": reserve_before,
            "reserve_after": reserve_after,
        }
        rec.require("wallet balances/frozen amounts remain non-negative", not bad_wallets, json.dumps(bad_wallets, ensure_ascii=False))
        rec.require("spot locked quantities remain non-negative", not bad_margin_locks, json.dumps(bad_margin_locks, ensure_ascii=False))
        rec.require("reserve pool remains non-negative and collects income", reserve_after >= 0 and reserve_after >= reserve_before, f"before={reserve_before} after={reserve_after}")

        feature_restore = api(root_page2, "PUT", "/admin/features", feature_snapshot)
        assert_api_ok(rec, "restore feature flags after trading probe", feature_restore)
        feature_after_response = api(root_page2, "GET", "/admin/features")
        assert_api_ok(rec, "feature flag readback after trading probe", feature_after_response)
        feature_after = {
            key: bool((feature_after_response.get("body") or {}).get("features", {}).get(key))
            for key in feature_snapshot
        }
        scenario["feature_flags_after"] = feature_after
        scenario["feature_flags_restored"] = feature_after == feature_snapshot
        rec.require(
            "trading probe feature flags restored exactly",
            feature_after == feature_snapshot,
            json.dumps({"before": feature_snapshot, "after": feature_after}, ensure_ascii=False),
        )
        root_ctx2.close()
        browser.close()
    restore_probe_runtime_once()
    if args.keep_mutated_settings:
        scenario["runtime_settings_restored"] = False

    report = {
        "ok": not rec.failures,
        "base_url": base_url,
        "runtime_dir": str(runtime_dir),
        "scenario": scenario,
        "checks": [row.__dict__ for row in rec.checks],
        "failures": [row.__dict__ for row in rec.failures],
    }
    json_path = out_dir / "trading_background_correctness.json"
    md_path = out_dir / "trading_background_correctness.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_lines = [
        "# Trading Background Correctness QA",
        "",
        f"- ok: `{report['ok']}`",
        f"- base_url: `{base_url}`",
        f"- runtime_dir: `{runtime_dir}`",
        f"- trigger_mode: `{args.trigger_mode}`",
        f"- reserve_before: `{scenario.get('reserve_before')}`",
        f"- reserve_after: `{scenario.get('reserve_after')}`",
        "",
        "## Checks",
    ]
    for row in rec.checks:
        md_lines.append(f"- [{'PASS' if row.ok else 'FAIL'}] {row.name}: {row.detail}")
    md_path.write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")
    print(f"[artifact] {json_path}", flush=True)
    print(f"[artifact] {md_path}", flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
