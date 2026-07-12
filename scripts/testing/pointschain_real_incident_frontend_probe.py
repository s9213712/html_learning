#!/usr/bin/env python3
"""Focused live Playwright checks for PointsChain incident hardening UI.

This complements backend attack probes by exercising the real frontend:
governance filters/folding, Treasury signer payload visibility, and dispute
button prompt/error behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_address(seed: str) -> str:
    return "pc1" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:48]


def fetch_json(page, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return page.evaluate(
        """async ({method, path, payload}) => {
            const cookieValue = name => {
                const part = document.cookie.split('; ').find(item => item.startsWith(name + '='));
                return part ? decodeURIComponent(part.split('=').slice(1).join('=')) : '';
            };
            const opts = {method, credentials: 'same-origin', headers: {'Accept': 'application/json'}};
            const csrf = cookieValue('csrf_token');
            if (csrf) opts.headers['X-CSRF-Token'] = csrf;
            if (method !== 'GET') {
                opts.headers['Content-Type'] = 'application/json';
                opts.body = JSON.stringify(payload || {});
            }
            const res = await fetch(path, opts);
            let body = {};
            try { body = await res.json(); } catch (err) { body = {raw: await res.text()}; }
            return {status: res.status, ok: res.ok, body};
        }""",
        {"method": method.upper(), "path": path, "payload": payload or {}},
    )


def login(page, base_url: str, username: str, password: str) -> dict[str, Any]:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.evaluate("() => fetch('/api/csrf-token', {credentials: 'same-origin'})")
    result = fetch_json(page, "POST", "/api/login", {"username": username, "password": password})
    if result["status"] == 200 and result["body"].get("ok"):
        page.goto(base_url + "/", wait_until="networkidle")
    return result


def ensure_official_hot_wallet(page) -> str:
    result = fetch_json(page, "POST", "/api/points/wallet/onboarding", {"mode": "official_hot"})
    if result["status"] not in {200, 409}:
        raise AssertionError(f"official hot wallet setup failed: {result}")
    onboarding = (result.get("body") or {}).get("onboarding") or {}
    wallet = onboarding.get("wallet") or (result.get("body") or {}).get("wallet_identity") or {}
    address = str(wallet.get("address") or "").strip().lower()
    if not address:
        wallets = onboarding.get("wallets") or []
        for item in wallets:
            if str(item.get("wallet_type") or "") == "official_hot":
                address = str(item.get("address") or "").strip().lower()
                break
    if not address:
        raise AssertionError(f"official hot wallet address missing: {result}")
    return address


def assert_system_account_has_no_member_hot_wallet(page) -> dict[str, Any]:
    result = fetch_json(page, "POST", "/api/points/wallet/onboarding", {"mode": "official_hot"})
    body = result.get("body") or {}
    if result["status"] != 403 or body.get("code") != "system_account_no_member_wallet":
        raise AssertionError(f"root system-account wallet guard failed: {result}")
    return {"status": result["status"], "code": body.get("code"), "msg": body.get("msg", "")}


def switch_to_economy_governance(page) -> None:
    page.evaluate(
        """() => {
            if (typeof switchModuleTab === 'function') switchModuleTab('economy');
        }"""
    )
    page.wait_for_timeout(500)
    page.locator("#tab-economy-governance").click(timeout=5000)
    page.wait_for_selector("#economy-governance-page.active", timeout=10000)


def wait_for_text(page, selector: str, needle: str, timeout_ms: int = 10000) -> str:
    page.wait_for_function(
        """({selector, needle}) => {
            const el = document.querySelector(selector);
            return el && (el.innerText || el.textContent || '').includes(needle);
        }""",
        arg={"selector": selector, "needle": needle},
        timeout=timeout_ms,
    )
    return page.locator(selector).inner_text(timeout=3000)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live frontend PointsChain incident-hardening checks.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", required=True)
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    from scripts.testing.probe_credentials import add_manager_password_argument
    add_manager_password_argument(parser, "--admin-password")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    browser_errors: list[dict[str, str]] = []
    checks: dict[str, Any] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        root_context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        admin_context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        root_page = root_context.new_page()
        admin_page = admin_context.new_page()
        for label, page in (("root", root_page), ("admin", admin_page)):
            page.on("console", lambda msg, label=label: browser_errors.append({"label": label, "type": "console", "text": msg.text[:500]}) if msg.type == "error" else None)
            page.on("pageerror", lambda exc, label=label: browser_errors.append({"label": label, "type": "pageerror", "text": str(exc)[:500]}))

        root_login = login(root_page, base_url, "root", args.root_password)
        admin_login = login(admin_page, base_url, "admin", args.admin_password)
        checks["login"] = {
            "root_status": root_login["status"],
            "admin_status": admin_login["status"],
            "root_must_change_password": bool((root_login.get("body") or {}).get("must_change_password")),
            "admin_must_change_password": bool((admin_login.get("body") or {}).get("must_change_password")),
        }
        if root_login["status"] != 200 or admin_login["status"] != 200:
            raise AssertionError(f"login failed: {checks['login']}")

        root_wallet_guard = assert_system_account_has_no_member_hot_wallet(root_page)
        admin_wallet = ensure_official_hot_wallet(admin_page)
        checks["signer_wallets"] = {
            "root_system_account_wallet_guard": root_wallet_guard,
            "admin": admin_wallet,
        }

        destination = test_address(f"frontend-treasury-dest-{time.time()}")
        proposal = fetch_json(
            admin_page,
            "POST",
            "/api/admin/points/governance/treasury-transfer",
            {
                "destination_wallet_address": destination,
                "amount": 7,
                "reason": "frontend signer payload probe",
                "reference": f"frontend-signer-{int(time.time())}",
                "action_type": "TREASURY_TRANSFER",
            },
        )
        if proposal["status"] != 200 or not proposal["body"].get("ok"):
            raise AssertionError(f"treasury proposal create failed: {proposal}")
        proposal_uuid = proposal["body"]["proposal"]["proposal_uuid"]
        checks["proposal_uuid"] = proposal_uuid
        for page, voter in ((root_page, "root"), (admin_page, "admin")):
            vote = fetch_json(page, "POST", f"/api/points/governance/proposals/{proposal_uuid}/vote", {"vote": "yes"})
            if vote["status"] != 200 or not vote["body"].get("ok"):
                raise AssertionError(f"{voter} vote failed: {vote}")

        switch_to_economy_governance(admin_page)
        status_tabs_text = admin_page.locator("#economy-governance-status-tabs").inner_text(timeout=5000)
        category_options = admin_page.locator("#economy-governance-category-select").inner_text(timeout=5000)
        if not all(label in status_tabs_text for label in ("審核中", "投票中", "已結案")):
            raise AssertionError(f"governance status tabs missing: {status_tabs_text}")
        if not all(label in category_options for label in ("全部治理", "選取案件", "官方財庫", "Mint 申請", "參數")):
            raise AssertionError(f"governance category options missing: {category_options}")
        checks["governance_filters"] = {"status_tabs": status_tabs_text, "categories": category_options}

        signer_text = wait_for_text(admin_page, "#economy-treasury-signer-pending-list", "signing hash")
        if "payload" not in signer_text:
            raise AssertionError(f"signer center payload hash missing: {signer_text}")
        checks["treasury_signer_center"] = {"visible": True, "pending_sample": signer_text[:500]}

        admin_page.locator("button[data-governance-status-filter='review']").click(timeout=5000)
        admin_page.wait_for_timeout(600)
        admin_page.locator("#economy-governance-category-select").select_option("all")
        admin_page.wait_for_timeout(600)
        toggles = admin_page.locator("[data-governance-toggle-proposal]")
        if toggles.count() < 1:
            raise AssertionError("no governance proposal cards available for folding check")
        before_panels = admin_page.locator(".economy-governance-proposal-action-panel").count()
        toggles.first.click(timeout=5000)
        admin_page.wait_for_timeout(300)
        after_open = admin_page.locator(".economy-governance-proposal-action-panel").count()
        toggles.first.click(timeout=5000)
        admin_page.wait_for_timeout(300)
        after_close = admin_page.locator(".economy-governance-proposal-action-panel").count()
        if after_open <= before_panels or after_close != before_panels:
            raise AssertionError({"before": before_panels, "after_open": after_open, "after_close": after_close})
        checks["governance_fold"] = {"before": before_panels, "after_open": after_open, "after_close": after_close}

        transfer = fetch_json(
            admin_page,
            "POST",
            "/api/points/transactions/submit",
            {
                "source_wallet_address": admin_wallet,
                "destination_wallet_address": test_address(f"frontend-dispute-to-{time.time()}"),
                "amount_points": 3,
                "fee_points": 1,
                "memo": "frontend dispute button probe",
                "request_uuid": f"frontend-dispute-{int(time.time() * 1000)}",
            },
        )
        if transfer["status"] != 200 or not transfer["body"].get("ok"):
            raise AssertionError(f"transfer setup failed: {transfer}")

        admin_page.locator("#tab-economy-transactions").click(timeout=5000)
        admin_page.wait_for_selector("#economy-transactions-page.active", timeout=10000)
        admin_page.evaluate(
            """async () => {
                if (typeof loadEconomyTransactions === 'function') await loadEconomyTransactions();
            }"""
        )
        admin_page.wait_for_function(
            "() => document.querySelectorAll('[data-dispute-tx]').length > 0",
            timeout=10000,
        )
        prompts: list[str] = []

        def on_dialog(dialog):
            prompts.append(dialog.message)
            if len(prompts) == 1:
                dialog.accept("太短")
            elif len(prompts) == 2:
                dialog.accept("此交易疑似未授權轉出，請依鏈上紀錄與附證審核。")
            elif len(prompts) == 3:
                dialog.accept("private_key_leak")
            elif len(prompts) == 4:
                dialog.accept("frontend-dispute-evidence-ref")
            else:
                dialog.dismiss()

        admin_page.on("dialog", on_dialog)
        admin_page.locator("[data-dispute-tx]").first.click(timeout=5000)
        admin_page.wait_for_timeout(1000)
        dispute_msg = admin_page.locator("#economy-transactions-msg").inner_text(timeout=5000)
        if not prompts or "至少 12" not in prompts[0] or "疑義交易已送出" not in dispute_msg:
            raise AssertionError({"prompts": prompts, "dispute_msg": dispute_msg})
        checks["dispute_button_prompt"] = {"first_prompt": prompts[0], "message": dispute_msg}
        browser.close()

    expected_browser_errors = [
        item for item in browser_errors
        if item.get("label") == "root"
        and item.get("type") == "console"
        and "status of 403" in str(item.get("text") or "")
    ]
    unexpected_browser_errors = [
        item for item in browser_errors
        if item not in expected_browser_errors
    ]
    payload = {
        "ok": not unexpected_browser_errors,
        "base_url": base_url,
        "checked_at": now_text(),
        "checks": checks,
        "browser_errors": unexpected_browser_errors[:50],
        "expected_browser_errors": expected_browser_errors[:20],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
