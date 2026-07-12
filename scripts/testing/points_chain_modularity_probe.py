#!/usr/bin/env python3
"""Live API + Playwright probe for basic points mode without PointsChain."""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


class HttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
        )
        self.csrf = ""

    def request(self, method: str, path: str, payload=None):
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as resp:
                text = resp.read().decode("utf-8", "replace")
                status = resp.getcode()
        except urllib.error.HTTPError as exc:
            status = exc.code
            text = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(text) if text else {}
        except Exception:
            body = {"raw": text[:500]}
        return {"status": status, "body": body}

    def csrf_token(self):
        result = self.request("GET", "/api/csrf-token")
        self.csrf = str((result.get("body") or {}).get("csrf_token") or "")
        return result

    def login(self, username: str, password: str):
        self.csrf_token()
        result = self.request("POST", "/api/login", {"username": username, "password": password})
        if result.get("status") == 200 and (result.get("body") or {}).get("ok"):
            self.csrf_token()
        return result


def assert_api(record: dict, name: str, result: dict, *, status: int | tuple[int, ...], ok=None, code=None):
    expected = status if isinstance(status, tuple) else (status,)
    body = result.get("body") or {}
    passed = result.get("status") in expected
    if ok is not None:
        passed = passed and body.get("ok") is ok
    if code is not None:
        passed = passed and body.get("code") == code
    record[name] = {
        "passed": bool(passed),
        "status": result.get("status"),
        "ok": body.get("ok"),
        "code": body.get("code"),
        "msg": body.get("msg") or body.get("message") or "",
    }
    if not passed:
        raise AssertionError(f"{name} failed: {record[name]}")


def fetch_json_in_page(page, method: str, path: str, payload=None):
    return page.evaluate(
        """async ({method, path, payload}) => {
            const cookieValue = name => {
                const part = document.cookie.split('; ').find(item => item.startsWith(name + '='));
                return part ? decodeURIComponent(part.split('=').slice(1).join('=')) : '';
            };
            if (!cookieValue('csrf_token')) await fetch('/api/csrf-token', {credentials: 'same-origin'});
            const opts = {
                method,
                credentials: 'same-origin',
                headers: {'Accept': 'application/json', 'X-CSRF-Token': cookieValue('csrf_token') || ''}
            };
            if (payload !== null) {
                opts.headers['Content-Type'] = 'application/json';
                opts.body = JSON.stringify(payload);
            }
            const response = await fetch(path, opts);
            const text = await response.text();
            let body = {};
            try { body = text ? JSON.parse(text) : {}; } catch (err) { body = {raw: text.slice(0, 500)}; }
            return {status: response.status, ok: response.ok, body};
        }""",
        {"method": method, "path": path, "payload": payload},
    )


def run_playwright(base_url: str, root_password: str, screenshot_dir: Path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(base_url + "/", wait_until="domcontentloaded")
        login = fetch_json_in_page(page, "POST", "/api/login", {"username": "root", "password": root_password})
        if login["status"] != 200 or not login["body"].get("ok"):
            raise AssertionError(f"playwright root login failed: {login}")
        page.goto(base_url + "/", wait_until="networkidle")
        page.wait_for_function(
            "() => document.body.classList.contains('app-authenticated') && typeof switchModuleTab === 'function'",
            timeout=15000,
        )
        page.evaluate("() => switchModuleTab('economy')")
        page.wait_for_timeout(1200)
        visible = page.evaluate(
            """() => {
                const visible = id => {
                    const el = document.getElementById(id);
                    return !!el && getComputedStyle(el).display !== 'none';
                };
                const text = id => (document.getElementById(id)?.textContent || '').trim();
                return {
                    economy_visible: visible('module-economy'),
                    balance_tab_visible: visible('tab-economy-balance'),
                    transactions_tab_visible: visible('tab-economy-transactions'),
                    explorer_tab_visible: visible('tab-economy-explorer'),
                    chain_tab_visible: visible('tab-economy-chain'),
                    root_wallet_card_visible: visible('economy-root-wallet-management-card'),
                    msg: text('economy-msg'),
                    chain_status: text('economy-chain-status'),
                    title: text('economy-page-title'),
                };
            }"""
        )
        screenshot = ""
        if visible["economy_visible"]:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            screenshot = str(screenshot_dir / f"points_chain_modularity_{int(time.time())}.png")
            page.screenshot(path=screenshot, full_page=True)
        browser.close()
        return {"visible": visible, "browser_errors": errors, "screenshot": screenshot}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:54343")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    from scripts.testing.probe_credentials import add_manager_password_argument
    add_manager_password_argument(parser, "--admin-password")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    root = HttpClient(args.base_url)
    admin = HttpClient(args.base_url)
    checks: dict[str, dict] = {}
    restore_payload = {}
    try:
        assert_api(checks, "root_login", root.login("root", args.root_password), status=200, ok=True)
        settings_res = root.request("GET", "/api/admin/settings")
        assert_api(checks, "settings_read", settings_res, status=200, ok=True)
        settings = settings_res["body"].get("settings") or {}
        restore_payload = {
            "feature_economy_enabled": bool(settings.get("feature_economy_enabled", False)),
            "feature_points_chain_enabled": bool(settings.get("feature_points_chain_enabled", True)),
            "feature_trading_enabled": bool(settings.get("feature_trading_enabled", False)),
        }
        disable_res = root.request(
            "PUT",
            "/api/admin/settings",
            {
                "feature_economy_enabled": True,
                "feature_points_chain_enabled": False,
                "feature_trading_enabled": False,
            },
        )
        assert_api(checks, "settings_chain_disabled", disable_res, status=200, ok=True)
        site_config = root.request("GET", "/api/site-config")
        assert_api(checks, "site_config_chain_disabled", site_config, status=200, ok=True)
        cfg = site_config["body"].get("site_config") or {}
        if cfg.get("feature_economy_enabled") is not True or cfg.get("feature_points_chain_enabled") is not False:
            raise AssertionError(f"site config did not expose basic-only mode: {cfg}")

        assert_api(checks, "admin_login", admin.login("admin", args.admin_password), status=200, ok=True)
        assert_api(checks, "basic_wallet", admin.request("GET", "/api/points/wallet"), status=200, ok=True)
        assert_api(checks, "basic_ledger", admin.request("GET", "/api/points/ledger?limit=10"), status=200, ok=True)
        assert_api(checks, "basic_catalog", admin.request("GET", "/api/points/catalog"), status=200, ok=True)
        spend = admin.request(
            "POST",
            "/api/points/spend",
            {"item_key": "post_cost_standard", "quantity": 1, "request_uuid": f"basic-mode-live-{int(time.time())}"},
        )
        assert_api(checks, "basic_spend", spend, status=200, ok=True)

        for name, method, path in (
            ("chain_onboarding_disabled", "GET", "/api/points/wallet/onboarding"),
            ("chain_transactions_disabled", "GET", "/api/points/transactions?limit=10"),
            ("chain_fee_estimate_disabled", "GET", "/api/points/explorer/fee-estimate?fee_points=1"),
        ):
            assert_api(checks, name, admin.request(method, path), status=503, ok=False, code="points_chain_disabled")
        assert_api(checks, "root_report_disabled", root.request("GET", "/api/root/points/report"), status=503, ok=False, code="points_chain_disabled")
        assert_api(
            checks,
            "root_grant_disabled",
            root.request("POST", "/api/root/points/official-wallet/grant", {"destination_wallet_address": "pc1deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "amount": 1}),
            status=503,
            ok=False,
            code="points_chain_disabled",
        )
        playwright = run_playwright(args.base_url, args.root_password, Path(args.out).parent)
        checks["playwright_tabs_hidden"] = {
            "passed": bool(
                playwright["visible"]["economy_visible"]
                and playwright["visible"]["balance_tab_visible"]
                and not playwright["visible"]["transactions_tab_visible"]
                and not playwright["visible"]["explorer_tab_visible"]
                and not playwright["visible"]["chain_tab_visible"]
                and not playwright["visible"]["root_wallet_card_visible"]
                and not playwright["browser_errors"]
            ),
            **playwright,
        }
        if not checks["playwright_tabs_hidden"]["passed"]:
            raise AssertionError(f"Playwright basic-only UI failed: {checks['playwright_tabs_hidden']}")
    finally:
        if restore_payload:
            try:
                root.request("PUT", "/api/admin/settings", restore_payload)
            except Exception:
                pass

    payload = {"ok": all(item.get("passed") for item in checks.values()), "checks": checks, "restored": restore_payload}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
