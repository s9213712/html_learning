#!/usr/bin/env python3
"""Read-only formal desktop/mobile UI sweep against an active campaign target."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.playwright_deep_site_check import login_as  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compact(value: object, limit: int = 500) -> str:
    return str(value or "").replace("\n", " ")[:limit]


def inspect_active_module(page, *, mobile: bool) -> dict[str, Any]:
    return page.evaluate(
        """({mobile}) => {
            const visible = el => {
                const style = getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && box.width > 0 && box.height > 0;
            };
            const active = document.querySelector('.module-section.active');
            const interactive = active
                ? Array.from(active.querySelectorAll('button, a[href], input, select, textarea, [role="button"], [tabindex]')).filter(visible)
                : [];
            const undersized = [];
            const outside = [];
            const clipped = [];
            const hiddenFocusable = [];
            for (const el of interactive) {
                const box = el.getBoundingClientRect();
                const name = String(el.id || el.getAttribute('aria-label') || el.textContent || el.name || el.tagName).trim().slice(0, 100);
                if (mobile && (box.width < 44 || box.height < 44)) {
                    undersized.push({name, width: Math.round(box.width), height: Math.round(box.height)});
                }
                if (box.left < -6 || box.right > window.innerWidth + 6) {
                    outside.push({name, left: Math.round(box.left), right: Math.round(box.right)});
                }
                if (el.tagName !== 'SELECT' && el.scrollWidth - el.clientWidth > 10 && box.width > 20) {
                    clipped.push({name, clientWidth: el.clientWidth, scrollWidth: el.scrollWidth});
                }
                if (el.getAttribute('aria-hidden') === 'true' || el.closest('[aria-hidden="true"]')) {
                    hiddenFocusable.push(name);
                }
            }
            const frontendFailures = Array.isArray(window.__hackmeFrontendFailures)
                ? window.__hackmeFrontendFailures.filter(item => !item?.expected).map(item => ({
                    scope: String(item?.scope || ''),
                    message: String(item?.message || '').slice(0, 300),
                }))
                : [];
            window.__hackmeFrontendFailures = [];
            return {
                activeModuleId: active?.id || '',
                rootOverflowPx: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                visibleInteractiveCount: interactive.length,
                undersized: undersized.slice(0, 100),
                outside: outside.slice(0, 100),
                clipped: clipped.slice(0, 100),
                hiddenFocusable: hiddenFocusable.slice(0, 100),
                frontendFailures: frontendFailures.slice(0, 100),
            };
        }""",
        {"mobile": mobile},
    )


def run_role(
    browser,
    *,
    base_url: str,
    username: str,
    password: str,
    role: str,
    viewport: dict[str, int],
    screenshot_dir: Path,
) -> dict[str, Any]:
    context = browser.new_context(ignore_https_errors=True, viewport=viewport)
    page = context.new_page()
    browser_errors: list[dict[str, Any]] = []
    failed_responses: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    seen: set[str] = set()

    def record(kind: str, text: object) -> None:
        value = compact(text)
        key = f"{kind}:{value}"
        if not value or key in seen or len(browser_errors) >= 200:
            return
        seen.add(key)
        browser_errors.append({"type": kind, "text": value})

    page.on("pageerror", lambda exc: record("pageerror", exc))
    page.on("console", lambda message: record(f"console.{message.type}", message.text) if message.type == "error" else None)
    page.on(
        "response",
        lambda response: failed_responses.append({
            "status": response.status,
            "method": response.request.method,
            "url": response.url.split("?", 1)[0],
        }) if response.status >= 500 and len(failed_responses) < 200 else None,
    )
    page.on(
        "requestfailed",
        lambda request: failed_requests.append({
            "method": request.method,
            "url": request.url.split("?", 1)[0],
            "failure": compact(request.failure),
        }) if len(failed_requests) < 200 else None,
    )
    module_rows: list[dict[str, Any]] = []
    screenshots: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        identity = login_as(page, base_url, username, password, load_app=True)
        visible_modules = page.eval_on_selector_all(
            "#module-main-tabs > .tab[id^='tab-module-']",
            """elements => elements
                .filter(el => !el.hidden && getComputedStyle(el).display !== 'none')
                .map(el => el.id.slice('tab-module-'.length))""",
        )
        screenshot_modules = set(visible_modules[:2])
        screenshot_modules.update(
            module for module in ("community", "videos", "economy", "server")
            if module in visible_modules
        )
        for module in visible_modules:
            navigation_started = time.perf_counter()
            page.evaluate(
                """module => {
                    if (typeof switchModuleTab !== 'function') throw new Error('switchModuleTab missing');
                    switchModuleTab(module);
                }""",
                module,
            )
            page.wait_for_selector(f"#module-{module}.active", state="visible", timeout=15000)
            page.wait_for_timeout(500)
            observation = inspect_active_module(page, mobile=viewport["width"] <= 768)
            navigation_ms = round((time.perf_counter() - navigation_started) * 1000, 3)
            module_ok = bool(
                observation.get("activeModuleId") == f"module-{module}"
                and int(observation.get("rootOverflowPx") or 0) <= 6
                and not observation.get("undersized")
                and not observation.get("outside")
                and not observation.get("clipped")
                and not observation.get("hiddenFocusable")
                and not observation.get("frontendFailures")
            )
            module_rows.append({
                "module": module,
                "navigation_ms": navigation_ms,
                "observation": observation,
                "passed": module_ok,
            })
            if module in screenshot_modules:
                screenshot_path = screenshot_dir / f"{role}_{viewport['width']}x{viewport['height']}_{module}.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                screenshots.append({
                    "role": role,
                    "module": module,
                    "viewport": dict(viewport),
                    "path": str(screenshot_path.resolve()),
                    "size_bytes": screenshot_path.stat().st_size,
                })
        role_ok = bool(
            visible_modules
            and module_rows
            and all(row.get("passed") is True for row in module_rows)
            and not browser_errors
            and not failed_responses
            and not failed_requests
            and screenshots
        )
        result = {
            "role": role,
            "username": username,
            "identity_role": str((identity.get("user") or {}).get("role") or ""),
            "viewport": dict(viewport),
            "visible_modules": visible_modules,
            "modules": module_rows,
            "screenshots": screenshots,
            "browser_errors": browser_errors,
            "failed_responses": failed_responses,
            "failed_requests": failed_requests,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "passed": role_ok,
            "context_closed": False,
        }
    finally:
        context.close()
        if result:
            result["context_closed"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--screenshot-dir", required=True)
    parser.add_argument("--root-password", default=os.environ.get("HACKME_QA_ROOT_PASSWORD", ""))
    parser.add_argument("--member-password", default=os.environ.get("HACKME_QA_TEST_PASSWORD", ""))
    args = parser.parse_args(argv)
    if not args.root_password or not args.member_password:
        parser.error("root and member credentials are required through the campaign environment")
    out_path = Path(args.out).expanduser().resolve()
    screenshot_dir = Path(args.screenshot_dir).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    roles: list[dict[str, Any]] = []
    browser_closed = False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                chromium_sandbox=False,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                roles.append(run_role(
                    browser,
                    base_url=args.base_url.rstrip("/"),
                    username="root",
                    password=args.root_password,
                    role="root_desktop",
                    viewport={"width": 1366, "height": 900},
                    screenshot_dir=screenshot_dir,
                ))
                roles.append(run_role(
                    browser,
                    base_url=args.base_url.rstrip("/"),
                    username="test",
                    password=args.member_password,
                    role="member_mobile",
                    viewport={"width": 390, "height": 844},
                    screenshot_dir=screenshot_dir,
                ))
            finally:
                browser.close()
                browser_closed = True
    except Exception as exc:
        roles.append({
            "role": "execution",
            "passed": False,
            "exception_type": exc.__class__.__name__,
            "exception_message": compact(exc),
        })
    screenshots = [item for role in roles for item in role.get("screenshots") or []]
    payload = {
        "schema_version": "hackme.campaign.formal-ui-sweep/v1",
        "started_at": started_at,
        "finished_at": utc_now(),
        "base_url": args.base_url.rstrip("/"),
        "roles": roles,
        "screenshots": screenshots,
        "browser_closed": browser_closed,
        "terminal_pass": bool(
            len(roles) == 2
            and all(role.get("passed") is True for role in roles)
            and browser_closed
            and len(screenshots) >= 4
        ),
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["terminal_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
