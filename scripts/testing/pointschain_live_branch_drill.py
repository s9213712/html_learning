#!/usr/bin/env python3
"""Run a live governed recovery-branch drill against an isolated server."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        token = self.csrf()
        if token:
            headers["X-CSRF-Token"] = token
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} HTTP {exc.code}: {raw[:800]}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def login(self, username: str, password: str) -> dict:
        self.request("GET", "/")
        return self.request("POST", "/api/login", {"username": username, "password": password})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--manager-username", default="admin")
    from scripts.testing.probe_credentials import add_manager_password_argument
    add_manager_password_argument(parser)
    parser.add_argument("--incident-tx-hash", required=True)
    parser.add_argument("--victim-wallet", required=True)
    parser.add_argument("--claim-amount", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    root = Client(args.base_url)
    manager = Client(args.base_url)
    root_login = root.login("root", args.root_password)
    manager_login = manager.login(args.manager_username, args.manager_password)
    if not root_login.get("ok"):
        raise RuntimeError(f"root login failed: {root_login}")
    if not manager_login.get("ok"):
        raise RuntimeError(f"manager login failed: {manager_login}")

    created = root.request(
        "POST",
        "/api/admin/points/governance/recovery-branch",
        {
            "incident_tx_hash": args.incident_tx_hash,
            "reason": "live governed tainted-remainder branch drill",
            "recovery_strategy": "tainted_remainder_return",
            "loss_cause": "private_key_leak",
            "victim_statement": "Live drill: user-caused compromise, only unused attacker tainted remainder should be returned.",
            "victim_evidence_refs": [args.incident_tx_hash],
            "victim_claims": [
                {
                    "claim_id": "live-drill-claim",
                    "wallet_address": args.victim_wallet,
                    "claim_amount_points": args.claim_amount,
                    "review_status": "approved",
                    "statement": "Live drill approved victim claim.",
                }
            ],
            "reference": "live-governed-branch-drill",
        },
    )
    proposal_uuid = (created.get("proposal") or {}).get("proposal_uuid")
    if not proposal_uuid:
        raise RuntimeError(f"proposal creation failed: {created}")

    root_vote = root.request(
        "POST",
        f"/api/points/governance/proposals/{urllib.parse.quote(proposal_uuid)}/vote",
        {"vote": "yes", "recovery_choice": "tainted_remainder_return"},
    )
    manager_vote = manager.request(
        "POST",
        f"/api/points/governance/proposals/{urllib.parse.quote(proposal_uuid)}/vote",
        {"vote": "yes", "recovery_choice": "tainted_remainder_return"},
    )
    executed = manager.request(
        "POST",
        f"/api/admin/points/governance/proposals/{urllib.parse.quote(proposal_uuid)}/execute",
        {},
    )
    result = {
        "ok": bool(executed.get("ok")),
        "proposal_uuid": proposal_uuid,
        "root_vote_status": (root_vote.get("proposal") or {}).get("status"),
        "manager_vote_status": (manager_vote.get("proposal") or {}).get("status"),
        "execution_action": (executed.get("result") or {}).get("action"),
        "branch_uuid": (executed.get("result") or {}).get("branch_uuid"),
        "parent_branch_uuid": (executed.get("result") or {}).get("parent_branch_uuid"),
        "recovery_seed": (executed.get("result") or {}).get("recovery_seed"),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
