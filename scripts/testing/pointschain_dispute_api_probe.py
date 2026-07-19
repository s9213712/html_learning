#!/usr/bin/env python3
"""Targeted API probe for address-proven PointsChain dispute flow.

The probe sets up a real self-custody transfer with local keys, then exercises
the public/admin HTTP dispute API. Private keys never leave this process.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.platform.db_mode_triggers import register_app_mode_function
from services.points_chain import (
    PointsLedgerService,
    address_dispute_payload,
    address_from_public_key,
    bind_self_custody_wallet,
    ensure_points_economy_schema,
    wallet_binding_payload,
    wallet_transaction_payload,
)
from services.points_chain.economy_layer import append_economy_event
from services.points_chain.schema import canonical_json, sha256_text


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
        )

    def csrf(self) -> str:
        for cookie in self.cookies:
            if cookie.name == "csrf_token":
                return cookie.value
        return ""

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        token = self.csrf()
        if token:
            headers["X-CSRF-Token"] = token
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        status = 0
        raw = ""
        try:
            with self.opener.open(req, timeout=30) as resp:
                status = int(resp.getcode())
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read().decode("utf-8", errors="replace")
        body_obj: dict[str, Any]
        try:
            parsed = json.loads(raw) if raw else {}
            body_obj = parsed if isinstance(parsed, dict) else {"body": parsed}
        except json.JSONDecodeError:
            body_obj = {"raw": raw[:500]}
        body_obj.setdefault("ok", 200 <= status < 400)
        body_obj["status"] = status
        return body_obj

    def login(self, username: str, password: str) -> dict:
        self.request("GET", "/")
        return self.request("POST", "/api/login", {"username": username, "password": password})


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(data)).decode("ascii").rstrip("=")


def public_jwk(private_key) -> dict:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(numbers.x.to_bytes(32, "big")),
        "y": b64url(numbers.y.to_bytes(32, "big")),
    }


def signature(private_key, payload: dict) -> str:
    der = private_key.sign(canonical_json(payload).encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    return b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def actor(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "username": str(row["username"]),
        "role": str(row["role"]),
        "member_level": row["member_level"] if "member_level" in row.keys() else "trusted",
        "effective_level": row["effective_level"] if "effective_level" in row.keys() else "trusted",
    }


def ensure_user(conn: sqlite3.Connection, *, username: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row:
        return row
    cols = {item["name"] for item in conn.execute("PRAGMA table_info(users)").fetchall()}
    payload = {
        "username": username,
        "role": "user",
        "status": "active",
        "member_level": "trusted",
        "base_level": "trusted",
        "effective_level": "trusted",
    }
    insert_cols = [key for key in payload if key in cols]
    conn.execute(
        f"INSERT INTO users ({','.join(insert_cols)}) VALUES ({','.join('?' for _ in insert_cols)})",
        tuple(payload[key] for key in insert_cols),
    )
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def self_custody_wallet_for(service: PointsLedgerService, user_id: int, *, label: str) -> tuple[dict, object]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = public_jwk(private_key)
    address = address_from_public_key(jwk)
    payload = wallet_binding_payload(
        user_id=user_id,
        wallet_type="self_custody_cold",
        address=address,
        public_key_jwk=jwk,
    )
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        wallet = bind_self_custody_wallet(
            conn,
            user_id=user_id,
            wallet_type="self_custody_cold",
            public_key_jwk=jwk,
            address=address,
            signature=signature(private_key, payload),
            backup_confirmed=True,
            label=label,
        )
        conn.commit()
        return wallet, private_key
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
    service.explorer_transaction(tx_hash)


def fixture_mint_to_treasury(service: PointsLedgerService, *, root: dict, amount: int, ref: str) -> None:
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        append_economy_event(
            conn,
            chain_secret=service.chain_secret,
            event_type="mint",
            transaction_type="qa_dispute_api_fixture_mint",
            source_fund_key="mint",
            destination_fund_key="official_treasury",
            amount=int(amount),
            idempotency_key=ref,
            metadata={"fixture": True, "reason": "address dispute API probe setup"},
            actor=root,
            chain_branch=service._canonical_branch_uuid(conn),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fixture_grant(service: PointsLedgerService, *, root: dict, destination: str, amount: int, ref: str) -> dict:
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        transfer = service._official_wallet_grant_locked(
            conn,
            actor=root,
            destination_wallet_address=destination,
            amount=amount,
            reason=f"address dispute API probe fixture grant {ref}",
            request_uuid=ref,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    force_request_proved(service, transfer["transaction_hash"])
    return transfer


def signed_transfer(
    service: PointsLedgerService,
    *,
    actor_value: dict,
    wallet: dict,
    private_key,
    destination: str,
    amount: int,
    fee: int,
    request_uuid: str,
) -> dict:
    branch = service.explorer_wallet(wallet["address"])["wallet"]["chain_branch"]
    payload = wallet_transaction_payload(
        user_id=actor_value["id"],
        source_wallet_address=wallet["address"],
        destination_wallet_address=destination,
        amount_points=amount,
        fee_points=fee,
        request_uuid=request_uuid,
        memo="address dispute API probe theft transfer",
        chain_branch=branch,
        signer_key_id=wallet["public_key_hash"],
    )
    return service.submit_wallet_transaction(
        actor=actor_value,
        source_wallet_address=wallet["address"],
        destination_wallet_address=destination,
        amount_points=amount,
        fee_points=fee,
        request_uuid=request_uuid,
        memo="address dispute API probe theft transfer",
        signature=signature(private_key, payload),
    )


def contains_identity_leak(value: Any) -> bool:
    forbidden = {"user_id", "username", "email", "ip", "ip_address", "client_ip", "reporter_user_id"}
    if isinstance(value, dict):
        return any(str(key).lower() in forbidden or contains_identity_leak(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_identity_leak(item) for item in value)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser, "--password")
    parser.add_argument("--runtime-root", default="/tmp/hackme_web_isolated_54343/hackme_web/runtime")
    parser.add_argument("--out", required=True)
    parser.add_argument("--mode", default="dev_ready")
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root)
    db_path = runtime_root / "database" / "database.db"
    chain_secret = (runtime_root / ".chain_seed").read_text(encoding="utf-8").strip()

    def get_db() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        register_app_mode_function(conn, mode_reader=lambda: args.mode)
        return conn

    service = PointsLedgerService(
        get_db=get_db,
        chain_secret=chain_secret,
        backup_dir=runtime_root / "database" / "points_chain_backups",
        mode_reader=lambda: args.mode,
    )
    run_id = str(int(time.time()))
    conn = get_db()
    try:
        service.ensure_schema(conn)
        ensure_points_economy_schema(conn)
        root_row = conn.execute("SELECT * FROM users WHERE username='root'").fetchone()
        victim_row = ensure_user(conn, username=f"api_dispute_victim_{run_id}")
        suspect_row = ensure_user(conn, username=f"api_dispute_suspect_{run_id}")
        conn.commit()
    finally:
        conn.close()

    root = actor(root_row)
    victim = actor(victim_row)
    victim_wallet, victim_key = self_custody_wallet_for(service, victim["id"], label=f"api dispute victim {run_id}")
    suspect_wallet, _suspect_key = self_custody_wallet_for(service, int(suspect_row["id"]), label=f"api dispute suspect {run_id}")
    fixture_mint_to_treasury(service, root=root, amount=100, ref=f"api-dispute-mint-{run_id}")
    fixture_grant(service, root=root, destination=victim_wallet["address"], amount=50, ref=f"api-dispute-grant-{run_id}")
    theft = signed_transfer(
        service,
        actor_value=victim,
        wallet=victim_wallet,
        private_key=victim_key,
        destination=suspect_wallet["address"],
        amount=21,
        fee=1,
        request_uuid=f"api-dispute-theft-{run_id}",
    )
    force_request_proved(service, theft["transaction_hash"])

    statement = "API probe signed address dispute statement with evidence."
    evidence = [theft["transaction_hash"], "api-probe-evidence"]
    tx = service.explorer_transaction(theft["transaction_hash"])["transaction"]
    runtime_mode = service._address_dispute_runtime_mode()
    signed_payload = address_dispute_payload(
        tx_hash=theft["transaction_hash"],
        from_wallet_address=victim_wallet["address"],
        to_wallet_address=suspect_wallet["address"],
        amount_points=21,
        statement_hash=sha256_text(statement),
        evidence_hash=sha256_text(canonical_json(evidence)),
        nonce=f"api-dispute-open-{run_id}",
        chain_branch=tx["chain_branch"],
        purpose="address_dispute_open",
        runtime_mode=runtime_mode,
    )
    body = {
        "tx_hash": theft["transaction_hash"],
        "statement": statement,
        "claimed_amount_points": 21,
        "loss_cause": "private_key_leak",
        "evidence": evidence,
        "from_wallet_address": victim_wallet["address"],
        "to_wallet_address": suspect_wallet["address"],
        "chain_branch": tx["chain_branch"],
        "public_key_jwk": public_jwk(victim_key),
        "signature": signature(victim_key, signed_payload),
        "signature_nonce": f"api-dispute-open-{run_id}",
    }

    wrong_purpose_payload = address_dispute_payload(
        tx_hash=theft["transaction_hash"],
        from_wallet_address=victim_wallet["address"],
        to_wallet_address=suspect_wallet["address"],
        amount_points=21,
        statement_hash=sha256_text(statement),
        evidence_hash=sha256_text(canonical_json(evidence)),
        nonce=f"api-dispute-wrong-purpose-{run_id}",
        chain_branch=tx["chain_branch"],
        purpose="address_dispute_reply",
        runtime_mode=runtime_mode,
    )
    wrong_purpose_body = dict(body)
    wrong_purpose_body["signature"] = signature(victim_key, wrong_purpose_payload)
    wrong_purpose_body["signature_nonce"] = f"api-dispute-wrong-purpose-{run_id}"

    wrong_branch_body = dict(body)
    wrong_branch_body["chain_branch"] = "production"

    client = Client(args.base_url)
    login = client.login(args.username, args.password)
    if int(login.get("status") or 0) != 200 or not login.get("ok"):
        raise RuntimeError(f"login failed: {login}")

    wrong_purpose = client.request("POST", "/api/points/transactions/disputes", wrong_purpose_body)
    wrong_branch = client.request("POST", "/api/points/transactions/disputes", wrong_branch_body)
    dispute = client.request("POST", "/api/points/transactions/disputes", body)
    replay = client.request("POST", "/api/points/transactions/disputes", body)

    dispute_uuid = (dispute.get("dispute") or {}).get("dispute_uuid")
    reviewed = {}
    if dispute_uuid:
        reviewed = client.request(
            "POST",
            f"/api/admin/points/transactions/disputes/{urllib.parse.quote(dispute_uuid)}/review",
            {
                "status": "approved",
                "review_note": "API probe manager review",
                "recommended_strategy": "tainted_remainder_return",
                "create_proposal": True,
            },
        )
    disputes_list = client.request("GET", "/api/points/transactions/disputes?limit=20")

    proposal = reviewed.get("proposal") or {}
    risk_proposal = reviewed.get("address_risk_proposal") or {}
    freeze_proposal = reviewed.get("address_freeze_proposal") or {}
    provisional_freeze = reviewed.get("provisional_freeze") or {}
    result = {
        "ok": bool(
            dispute_uuid
            and int(wrong_purpose.get("status") or 0) == 400
            and int(wrong_branch.get("status") or 0) == 400
            and int(replay.get("status") or 0) == 400
            and proposal.get("proposal_uuid")
            and risk_proposal.get("proposal_uuid")
            and freeze_proposal.get("proposal_uuid")
            and provisional_freeze.get("status") == "active"
            and not contains_identity_leak(dispute)
            and not contains_identity_leak(reviewed)
            and not contains_identity_leak(disputes_list)
        ),
        "fixture_usernames": [victim["username"], str(suspect_row["username"])],
        "tx_hash": theft["transaction_hash"],
        "dispute_uuid": dispute_uuid,
        "wrong_purpose_status": wrong_purpose.get("status"),
        "wrong_branch_status": wrong_branch.get("status"),
        "replay_status": replay.get("status"),
        "review_status": (reviewed.get("dispute") or {}).get("status"),
        "proposal_uuid": proposal.get("proposal_uuid"),
        "proposal_action_type": proposal.get("action_type"),
        "address_risk_proposal_uuid": risk_proposal.get("proposal_uuid"),
        "address_freeze_proposal_uuid": freeze_proposal.get("proposal_uuid"),
        "provisional_freeze_status": provisional_freeze.get("status"),
        "provisional_freeze_expires_at": provisional_freeze.get("expires_at"),
        "redaction": {
            "create_response_identity_leak": contains_identity_leak(dispute),
            "review_response_identity_leak": contains_identity_leak(reviewed),
            "list_response_identity_leak": contains_identity_leak(disputes_list),
        },
        "wrong_purpose_msg": wrong_purpose.get("msg") or wrong_purpose.get("message") or "",
        "wrong_branch_msg": wrong_branch.get("msg") or wrong_branch.get("message") or "",
        "replay_msg": replay.get("msg") or replay.get("message") or "",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
