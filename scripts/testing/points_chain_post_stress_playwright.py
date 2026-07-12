#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fetch_json(page, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return page.evaluate(
        """async ({method, path, payload}) => {
            const cookieValue = name => {
                const part = document.cookie.split('; ').find(item => item.startsWith(name + '='));
                return part ? decodeURIComponent(part.split('=').slice(1).join('=')) : '';
            };
            const opts = {
                method,
                credentials: 'same-origin',
                headers: {'Accept': 'application/json'}
            };
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


def switch_module(page, module: str) -> None:
    page.evaluate(
        """module => {
            if (typeof switchModuleTab !== 'function') throw new Error('switchModuleTab missing');
            switchModuleTab(module);
        }""",
        module,
    )
    page.wait_for_timeout(700)


def switch_economy_page(page, subpage: str) -> None:
    page.evaluate(
        """subpage => {
            if (typeof switchEconomyPage !== 'function') {
                const tab = document.querySelector(`[data-economy-page="${subpage}"]`);
                if (!tab) throw new Error(`economy tab missing: ${subpage}`);
                tab.click();
                return;
            }
            switchEconomyPage(subpage);
        }""",
        subpage,
    )
    page.wait_for_timeout(1200)


def text(page, selector: str) -> str:
    return page.locator(selector).inner_text(timeout=3000).strip()


def visible(page, selector: str) -> bool:
    return page.locator(selector).is_visible(timeout=3000)


def root_checks(page, base_url: str, password: str) -> dict[str, Any]:
    login_result = login(page, base_url, "root", password)
    if login_result["status"] != 200 or not login_result["body"].get("ok"):
        return {"ok": False, "login": login_result}
    switch_module(page, "economy")

    wallet_api = fetch_json(page, "GET", "/api/points/wallet")
    tx_api = fetch_json(page, "GET", "/api/points/transactions?limit=10")
    report_api = fetch_json(page, "GET", "/api/root/points/report")
    fee_api = fetch_json(page, "GET", "/api/points/explorer/fee-estimate?fee_points=0")

    switch_economy_page(page, "balance")
    wallet_card_visible = visible(page, "#economy-root-wallet-management-card")
    wallet_card_text = text(page, "#economy-root-wallet-management-card") if wallet_card_visible else ""

    switch_economy_page(page, "transactions")
    tx_page_visible = visible(page, "#economy-transactions-page")
    tx_list_text = text(page, "#economy-transactions-list")
    tx_summary = {
        "pending": text(page, "#economy-transactions-pending-count"),
        "confirmed": text(page, "#economy-transactions-confirmed-count"),
        "failed": text(page, "#economy-transactions-failed-count"),
    }

    switch_economy_page(page, "chain")
    page.wait_for_function(
        """() => {
            const el = document.querySelector('#economy-chain-ok');
            return el && el.textContent.trim() && !['-', '讀取中'].includes(el.textContent.trim());
        }""",
        timeout=10000,
    )
    chain = {
        "visible": visible(page, "#economy-chain-page"),
        "ok_text": text(page, "#economy-chain-ok"),
        "counts": text(page, "#economy-chain-counts"),
        "blocks": text(page, "#economy-chain-blocks"),
        "unsealed": text(page, "#economy-chain-unsealed"),
        "status": text(page, "#economy-chain-status"),
        "details": {
            "seal": visible(page, "#economy-chain-seal-details"),
            "audit": visible(page, "#economy-chain-audit-details"),
            "incident": visible(page, "#economy-chain-incident-details"),
            "unsealed": visible(page, "#economy-chain-unsealed-details"),
        },
    }
    ok = all(
        [
            wallet_api["status"] == 200,
            tx_api["status"] == 200,
            report_api["status"] == 200,
            fee_api["status"] == 200,
            wallet_card_visible,
            "official" in wallet_card_text.lower() or "treasury" in wallet_card_text.lower() or "pc1" in wallet_card_text.lower(),
            tx_page_visible,
            bool(tx_list_text),
            chain["visible"],
            chain["ok_text"] in {"完整", "ok", "OK"},
            "ledger" in chain["counts"],
            all(chain["details"].values()),
        ]
    )
    return {
        "ok": ok,
        "login": login_result,
        "apis": {
            "wallet": wallet_api["status"],
            "transactions": tx_api["status"],
            "root_report": report_api["status"],
            "fee_estimate": fee_api["status"],
        },
        "wallet_card_visible": wallet_card_visible,
        "wallet_card_text": wallet_card_text[:500],
        "transactions": {"visible": tx_page_visible, "summary": tx_summary, "list_sample": tx_list_text[:500]},
        "chain": chain,
        "fee_market": (fee_api.get("body") or {}).get("estimate", {}).get("network_fee_state", {}),
    }


def member_checks(page, base_url: str, username: str, password: str) -> dict[str, Any]:
    login_result = login(page, base_url, username, password)
    if login_result["status"] != 200 or not login_result["body"].get("ok"):
        return {"ok": False, "login": login_result}
    switch_module(page, "economy")
    wallet_api = fetch_json(page, "GET", "/api/points/wallet")
    tx_api = fetch_json(page, "GET", "/api/points/transactions?limit=10")
    notifications_api = fetch_json(page, "GET", "/api/notifications?limit=20")
    switch_economy_page(page, "transactions")
    tx_visible = visible(page, "#economy-transactions-page")
    tx_text = text(page, "#economy-transactions-list")
    ok = all(
        [
            wallet_api["status"] == 200,
            tx_api["status"] == 200,
            notifications_api["status"] == 200,
            tx_visible,
            bool(tx_text),
        ]
    )
    return {
        "ok": ok,
        "login": login_result,
        "apis": {
            "wallet": wallet_api["status"],
            "transactions": tx_api["status"],
            "notifications": notifications_api["status"],
        },
        "transactions": {"visible": tx_visible, "list_sample": tx_text[:500]},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Focused Playwright post-stress check for an already running hackme_web server.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", required=True)
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--member-username", default="admin")
    from scripts.testing.probe_credentials import add_manager_password_argument
    add_manager_password_argument(parser, "--member-password")
    args = parser.parse_args()

    errors: list[dict[str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        root_context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        member_context = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})

        def attach(page, label: str) -> None:
            page.on("console", lambda msg: errors.append({"label": label, "type": "console", "text": msg.text[:500]}) if msg.type in {"error"} else None)
            page.on("pageerror", lambda exc: errors.append({"label": label, "type": "pageerror", "text": str(exc)[:500]}))

        root_page = root_context.new_page()
        member_page = member_context.new_page()
        attach(root_page, "root")
        attach(member_page, "member")
        root = root_checks(root_page, args.base_url.rstrip("/"), args.root_password)
        member = member_checks(member_page, args.base_url.rstrip("/"), args.member_username, args.member_password)
        browser.close()

    payload = {
        "ok": bool(root.get("ok")) and bool(member.get("ok")) and not errors,
        "base_url": args.base_url.rstrip("/"),
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "root": root,
        "member": member,
        "browser_errors": errors[:50],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
