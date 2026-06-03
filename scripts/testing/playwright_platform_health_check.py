#!/usr/bin/env python3
"""Phase 1.5 Playwright acceptance check for platform center features.

The check starts hackme_web with an isolated /tmp runtime and a random port,
then uses real Chromium page interactions plus API calls to validate:

- Job Center
- Notification Center
- Share Link Management
- Trading Asset Overview
- Frontend/mobile health

It intentionally never writes to the repository runtime/ or storage/ folders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.testing.playwright_deep_site_check import (  # noqa: E402
    ROOT_PASSWORD,
    Recorder,
    apply_optional_comfyui_settings,
    attach_browser_error_handlers,
    build_env,
    check_ui_quality,
    collect_optional_comfyui_config,
    cookie_value,
    enable_required_features,
    fetch_json,
    fetch_multipart,
    free_port,
    login,
    mkdirs,
    start_server,
    switch_module,
    text_file,
    utc_stamp,
    wait_for_auth_app,
    wait_for_server,
)
from services.job_center import create_job, update_job  # noqa: E402
from services.media.videos import ensure_video_schema  # noqa: E402
from services.storage.catalog import ensure_storage_album_schema  # noqa: E402
from services.system.notifications import create_notification, ensure_notifications_schema  # noqa: E402


EXPECTED_BROWSER_HTTP_FAILURE_PATHS = (
    "/api/admin/trading/report",
    "/api/comfyui/generate",
    "/api/points/explorer/fee-estimate",
    "/api/root/trading/sitewide/user-positions",
    "/api/trading/asset-overview",
    "/api/trading/bot-competition",
    "/api/trading/btc-signal",
    "/api/trading/dashboard",
    "/api/trading/live-price",
    "/api/trading/reference-prices",
)


def ignored_browser_error(compact: str) -> bool:
    text = str(compact or "")
    if "phase15 forced failure" in text:
        return True
    if "Failed to load resource: the server responded with a status of 503" in text:
        return True
    if "Failed to load resource: the server responded with a status of 404" in text:
        return True
    if text.startswith(("503 ", "404 ")) and any(
        namespace in text
        for namespace in (
            "/api/admin/trading/",
            "/api/root/trading/",
            "/api/trading/",
        )
    ):
        return True
    if text.startswith(("503 ", "404 ")) and any(path in text for path in EXPECTED_BROWSER_HTTP_FAILURE_PATHS):
        return True
    return False


def now_text() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def future_text(seconds: int = 3600) -> str:
    return (datetime.utcnow() + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def db_path(runtime_root: Path) -> Path:
    return runtime_root / "database" / "database.db"


def db_conn(runtime_root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path(runtime_root)), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def finance_db_path(runtime_root: Path) -> Path:
    return runtime_root / "database" / "finance.db"


def ensure_desktop_auth_app(page, *, timeout: int = 30000) -> None:
    page.set_viewport_size({"width": 1366, "height": 768})
    wait_for_auth_app(page, timeout=timeout)
    page.wait_for_function(
        """() => document.body.classList.contains('app-authenticated')
            && typeof switchModuleTab === 'function'
            && !!document.querySelector('#notification-toggle')""",
        timeout=timeout,
    )


def switch_module_and_wait(page, module: str, ready_selector: str, *, timeout: int = 15000) -> None:
    ensure_desktop_auth_app(page, timeout=max(timeout, 30000))
    switch_module(page, module)
    page.wait_for_function(
        """module => document.querySelector(`#module-${module}`)?.classList.contains('active')""",
        arg=module,
        timeout=timeout,
    )
    page.wait_for_selector(f"#module-{module}.active", state="visible", timeout=timeout)
    page.wait_for_selector(ready_selector, state="visible", timeout=timeout)


def login_with_retry(page, base_url: str, *, attempts: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            login(page, base_url)
            return
        except Exception as exc:  # Playwright/network startup races are retried at this boundary.
            last_error = exc
            if attempt >= attempts:
                break
            page.wait_for_timeout(1000 * attempt)
    raise RuntimeError(f"login failed after {attempts} attempts: {last_error}")


def finance_db_conn(runtime_root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(finance_db_path(runtime_root)), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def extract_user_id(me_payload: dict[str, Any]) -> int:
    body = me_payload.get("body") or {}
    user = body.get("user") if isinstance(body.get("user"), dict) else body
    return int(user.get("id") or body.get("id") or 0)


def ensure_normal_user(page) -> dict[str, Any]:
    username = "qa_phase15_user"
    users = fetch_json(page, "GET", "/api/admin/users?include_deleted=1")
    for item in (users.get("body") or {}).get("users") or []:
        if item.get("username") == username:
            return {"id": int(item["id"]), "username": username, "password": "QaPhase15User123!"}
    created = fetch_json(
        page,
        "POST",
        "/api/admin/users",
        {
            "username": username,
            "password": "QaPhase15User123!",
            "password_confirm": "QaPhase15User123!",
            "nickname": "QA Phase 1.5",
            "role": "user",
            "status": "active",
            "member_level": "normal",
        },
    )
    if created["status"] not in {200, 201, 409}:
        raise RuntimeError(f"create normal user failed: {created}")
    users = fetch_json(page, "GET", "/api/admin/users?include_deleted=1")
    for item in (users.get("body") or {}).get("users") or []:
        if item.get("username") == username:
            return {"id": int(item["id"]), "username": username, "password": "QaPhase15User123!"}
    raise RuntimeError("normal user was not found after creation")


def seed_job_center_and_notifications(runtime_root: Path, user_id: int) -> dict[str, Any]:
    conn = db_conn(runtime_root)
    try:
        running = create_job(
            conn,
            owner_user_id=user_id,
            created_by_user_id=user_id,
            job_type="comfyui.generate",
            title="Phase 1.5 ComfyUI queued job",
            source_module="comfyui",
            source_ref="phase15-running",
            status="running",
            progress_percent=42,
            stage="executing",
            stage_detail="Playwright seeded running job",
            max_retries=2,
            cancellable=True,
            metadata={"acceptance": "phase1.5"},
        )
        failed = create_job(
            conn,
            owner_user_id=user_id,
            created_by_user_id=user_id,
            job_type="comfyui.generate",
            title="Phase 1.5 failed job",
            source_module="comfyui",
            source_ref="phase15-failed",
            status="queued",
            progress_percent=15,
            stage="queued",
            cancellable=True,
        )
        update_job(
            conn,
            failed["job_uuid"],
            status="failed",
            progress_percent=15,
            stage="execution_failed",
            stage_detail="missing_model",
            error_stage="execution_failed",
            error_message="missing checkpoint model",
            finished_at=now_text(),
        )
        succeeded = create_job(
            conn,
            owner_user_id=user_id,
            created_by_user_id=user_id,
            job_type="report.generate",
            title="Phase 1.5 completed report",
            source_module="reports",
            source_ref="phase15-succeeded",
            status="queued",
            progress_percent=100,
            stage="completed",
        )
        update_job(conn, succeeded["job_uuid"], status="succeeded", progress_percent=100, stage="completed", finished_at=now_text())

        ensure_notifications_schema(conn)
        info = create_notification(
            conn,
            user_id=user_id,
            type="phase15_info",
            title="Phase 1.5 info notification",
            body="info notification for acceptance testing",
            severity="info",
            audience="user",
            source_module="phase15",
            source_ref="info",
        )
        warning = create_notification(
            conn,
            user_id=user_id,
            type="phase15_warning",
            title="Phase 1.5 warning notification",
            body="warning notification for acceptance testing",
            severity="warning",
            audience="user",
            source_module="phase15",
            source_ref="warning",
        )
        error = create_notification(
            conn,
            user_id=user_id,
            type="phase15_error",
            title="Phase 1.5 error notification",
            body="error notification for acceptance testing",
            severity="error",
            audience="user",
            source_module="phase15",
            source_ref="error",
        )
        conn.commit()
        return {
            "running_job_uuid": running["job_uuid"],
            "failed_job_uuid": failed["job_uuid"],
            "succeeded_job_uuid": succeeded["job_uuid"],
            "notifications_created": bool(info) + bool(warning) + bool(error),
        }
    finally:
        conn.close()


def seed_shares(runtime_root: Path, user_id: int) -> dict[str, Any]:
    conn = db_conn(runtime_root)
    try:
        ensure_storage_album_schema(conn)
        ensure_video_schema(conn)
        now = now_text()
        expires = future_text(3600)
        file_id = f"phase15-file-{uuid.uuid4().hex[:8]}"
        storage_id = f"phase15-storage-{uuid.uuid4().hex[:8]}"
        album_id = f"phase15-album-{uuid.uuid4().hex[:8]}"
        video_cloud_file_id = f"phase15-video-file-{uuid.uuid4().hex[:8]}"
        conn.execute(
            """
            INSERT OR REPLACE INTO uploaded_files (
                id, owner_user_id, storage_path, privacy_mode, risk_level, scan_status,
                original_filename_plain_for_public, mime_type_plain_for_public,
                size_bytes, created_at, updated_at
            ) VALUES (?, ?, ?, 'standard_plain', 'low', 'clean', ?, 'text/plain', 12, ?, ?)
            """,
            (file_id, user_id, f"/tmp/{file_id}.txt", "phase15.txt", now, now),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO storage_files (
                id, file_id, owner_user_id, display_name, virtual_path, created_at, updated_at
            ) VALUES (?, ?, ?, 'Phase 1.5 file share', '/QA/phase15.txt', ?, ?)
            """,
            (storage_id, file_id, user_id, now, now),
        )
        file_share_id = f"phase15-file-share-{uuid.uuid4().hex[:8]}"
        file_token = f"phase15-file-token-{uuid.uuid4().hex[:8]}"
        conn.execute(
            """
            INSERT OR REPLACE INTO storage_share_links (
                id, storage_file_id, file_id, owner_user_id, token_hash, can_download,
                can_preview, expires_at, access_count, last_accessed_at, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, 2, ?, ?, ?)
            """,
            (file_share_id, storage_id, file_id, user_id, hashlib.sha256(file_token.encode()).hexdigest(), expires, now, user_id, now),
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO albums (
                id, owner_user_id, title, description, visibility, created_at, updated_at
            ) VALUES (?, ?, 'Phase 1.5 Album Share', 'album share acceptance', 'private', ?, ?)
            """,
            (album_id, user_id, now, now),
        )
        album_share_id = f"phase15-album-share-{uuid.uuid4().hex[:8]}"
        album_token = f"phase15-album-token-{uuid.uuid4().hex[:8]}"
        conn.execute(
            """
            INSERT OR REPLACE INTO album_share_links (
                id, album_id, owner_user_id, token, token_hash, password_required,
                access_count, last_accessed_at, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
            """,
            (album_share_id, album_id, user_id, album_token, hashlib.sha256(album_token.encode()).hexdigest(), now, user_id, now),
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO uploaded_files (
                id, owner_user_id, storage_path, privacy_mode, risk_level, scan_status,
                original_filename_plain_for_public, mime_type_plain_for_public,
                size_bytes, created_at, updated_at
            ) VALUES (?, ?, ?, 'standard_plain', 'low', 'clean', ?, 'video/mp4', 1024, ?, ?)
            """,
            (video_cloud_file_id, user_id, f"/tmp/{video_cloud_file_id}.mp4", "phase15.mp4", now, now),
        )
        video_uuid = f"phase15-video-{uuid.uuid4().hex[:8]}"
        cur = conn.execute(
            """
            INSERT INTO videos (
                video_uuid, owner_user_id, cloud_file_id, title, description,
                visibility, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'Phase 1.5 Video Share', 'video share acceptance', 'unlisted', 'ready', ?, ?)
            """,
            (video_uuid, user_id, video_cloud_file_id, now, now),
        )
        video_id = int(cur.lastrowid)
        video_share_id = f"phase15-video-share-{uuid.uuid4().hex[:8]}"
        video_token = f"phase15-video-token-{uuid.uuid4().hex[:8]}"
        conn.execute(
            """
            INSERT OR REPLACE INTO video_share_links (
                id, video_id, owner_user_id, token, token_hash, password_required,
                expires_at, max_views, access_count, last_accessed_at, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, 2, 1, ?, ?, ?)
            """,
            (video_share_id, video_id, user_id, video_token, hashlib.sha256(video_token.encode()).hexdigest(), expires, now, user_id, now),
        )
        conn.commit()
        return {
            "file_share_id": file_share_id,
        "album_share_id": album_share_id,
        "album_share_url": f"/shared/albums/{album_token}",
        "album_share_api_url": f"/api/storage/shared/albums/{album_token}",
        "video_share_id": video_share_id,
        "video_share_url": f"/shared/videos/{video_token}",
        }
    finally:
        conn.close()


def seed_trading(runtime_root: Path, user_id: int) -> dict[str, Any]:
    conn = finance_db_conn(runtime_root)
    try:
        now = now_text()
        conn.execute(
            """
            INSERT OR REPLACE INTO trading_markets (
                symbol, base_asset, quote_currency, enabled, spot_enabled,
                manual_price_points, fee_rate_percent, updated_at, updated_by,
                price_source
            ) VALUES ('QA/POINTS', 'QA', 'POINTS', 1, 1, 100, 0, ?, ?, 'manual')
            """,
            (now, user_id),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO trading_sim_accounts (
                user_id, balance_points, locked_points, initial_balance_points, updated_at
            ) VALUES (?, 12345, 678, 10000, ?)
            """,
            (user_id, now),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO trading_spot_positions (
                user_id, market_symbol, quantity_units, locked_quantity_units,
                avg_cost_points, updated_at
            ) VALUES (?, 'QA/POINTS', 100000000, 0, 80, ?)
            """,
            (user_id, now),
        )
        conn.execute(
            """
            INSERT INTO trading_margin_positions (
                position_uuid, user_id, market_symbol, position_type, quantity_units,
                entry_price_points, principal_points, collateral_points, open_fee_points,
                interest_percent_daily, interest_points, interest_paid_points,
                status, opened_at, updated_at
            ) VALUES (?, ?, 'QA/POINTS', 'margin_long', 100000000, 100, 80, 50, 0, 1.25, 1, 0, 'open', ?, ?)
            """,
            (f"phase15-margin-{uuid.uuid4().hex[:8]}", user_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO trading_orders (
                order_uuid, user_id, market_symbol, side, order_type, quantity_units,
                limit_price_points, status, created_at, updated_at
            ) VALUES (?, ?, 'QA/POINTS', 'buy', 'limit', 100000000, 50, 'open', ?, ?)
            """,
            (f"phase15-order-{uuid.uuid4().hex[:8]}", user_id, now, now),
        )
        conn.commit()
        return {
            "expected_available_points": 12345,
            "expected_locked_points": 678,
            "expected_spot_market_value_points": 100,
            "expected_accrued_interest_min_points": 1,
        }
    finally:
        conn.close()


def trigger_comfyui_job_attempt(page) -> dict[str, Any]:
    return fetch_json(
        page,
        "POST",
        "/api/comfyui/generate",
        {
            "generation_mode": "txt2img",
            "prompt": "phase 1.5 acceptance test",
            "negative_prompt": "low quality",
            "model": "phase15-missing-checkpoint.safetensors",
            "width": 512,
            "height": 512,
            "steps": 1,
            "cfg": 1,
            "seed": 1,
            "sampler_name": "euler",
            "scheduler": "normal",
            "batch_size": 1,
            "async_progress": True,
            "timeout_seconds": 30,
        },
    )


def check_job_center(rec: Recorder, page, base_url: str, normal_user: dict[str, Any]) -> dict[str, Any]:
    comfy_attempt = trigger_comfyui_job_attempt(page)
    jobs = fetch_json(page, "GET", "/api/jobs?limit=80")
    admin_jobs = fetch_json(page, "GET", "/api/admin/jobs?limit=80")
    job_rows = (jobs.get("body") or {}).get("jobs") or []
    switch_module_and_wait(page, "jobs", "#job-center-list")
    page.wait_for_timeout(800)
    check_ui_quality(rec, page, "job_center_desktop")
    job_text = page.locator("#job-center-list").inner_text(timeout=3000)
    cancel_dialog_message = ""
    cancel_buttons = page.locator("[data-job-cancel]")
    if cancel_buttons.count():
        page.evaluate(
            """() => {
                window.__phase15ConfirmMessage = '';
                window.__phase15OriginalConfirm = window.confirm;
                window.confirm = message => {
                    window.__phase15ConfirmMessage = String(message || '');
                    return true;
                };
            }"""
        )
        cancel_buttons.first.click(timeout=5000, force=True)
        if page.locator("#app-dialog-overlay.show").count():
            cancel_dialog_message = page.locator("#app-dialog-message").inner_text(timeout=3000)
            page.click("#app-dialog-confirm", timeout=5000)
            page.wait_for_selector("#app-dialog-overlay.show", state="detached", timeout=5000)
        else:
            cancel_dialog_message = page.evaluate("() => window.__phase15ConfirmMessage || ''")
        page.evaluate(
            """() => {
                if (window.__phase15OriginalConfirm) window.confirm = window.__phase15OriginalConfirm;
            }"""
        )
        page.wait_for_timeout(1000)
    failed_job = next((row for row in job_rows if row.get("status") == "failed"), {})
    retry_result = fetch_json(page, "POST", f"/api/jobs/{failed_job.get('job_uuid', '')}/retry") if failed_job.get("job_uuid") else {"status": 0, "body": {"ok": False, "msg": "no failed job"}}
    page.evaluate("() => typeof loadJobCenter === 'function' && loadJobCenter()")
    page.wait_for_timeout(1000)
    after_jobs = fetch_json(page, "GET", "/api/jobs?limit=80")

    user_ctx = page.context.browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 720})
    user_page = user_ctx.new_page()
    user_page.goto(base_url + "/", wait_until="domcontentloaded")
    fetch_json(user_page, "POST", "/api/login", {"username": normal_user["username"], "password": normal_user["password"]})
    user_admin_jobs = fetch_json(user_page, "GET", "/api/admin/jobs?limit=80")
    user_ctx.close()

    statuses = {row.get("status") for row in job_rows}
    has_error = "missing checkpoint model" in job_text or "missing_model" in job_text
    ok = (
        jobs["status"] == 200
        and admin_jobs["status"] == 200
        and bool({"running", "failed", "succeeded"} & statuses)
        and "Phase 1.5" in job_text
        and "comfyui" in job_text.lower()
        and has_error
        and "取消" in cancel_dialog_message
        and retry_result["status"] == 200
        and user_admin_jobs["status"] == 403
    )
    detail = (
        f"jobs={jobs['status']}, admin={admin_jobs['status']}, user_admin={user_admin_jobs['status']}, "
        f"statuses={sorted(filter(None, statuses))}, comfy_attempt={comfy_attempt['status']}"
    )
    rec.add(
        "phase15_job_center",
        ok,
        detail,
        comfyui_generate_attempt={"status": comfy_attempt["status"], "body": comfy_attempt.get("body")},
        jobs=job_rows[:6],
        after_jobs=(after_jobs.get("body") or {}).get("jobs", [])[:6],
        cancel_dialog=cancel_dialog_message,
        retry_result=retry_result.get("body"),
        normal_user_admin_jobs=user_admin_jobs.get("body"),
    )
    return {"jobs": job_rows, "comfy_attempt": comfy_attempt}


def check_notifications(rec: Recorder, page, base_url: str, normal_user: dict[str, Any], root_user_id: int) -> dict[str, Any]:
    before = fetch_json(page, "GET", "/api/notifications?limit=20")
    unread = fetch_json(page, "GET", "/api/notifications/unread-count")
    items = (before.get("body") or {}).get("notifications") or []
    severities = {item.get("severity") for item in items}
    target = next((item for item in items if item.get("source_module") == "phase15"), items[0] if items else None)
    dismiss_result = {"status": 0, "body": {}}
    if target:
        dismiss_result = fetch_json(page, "POST", f"/api/notifications/{int(target['id'])}/dismiss")
    after = fetch_json(page, "GET", "/api/notifications?limit=20")
    include_dismissed = fetch_json(page, "GET", "/api/notifications?limit=50&include_dismissed=1")

    ensure_desktop_auth_app(page)
    page.wait_for_selector("#notification-toggle", state="visible", timeout=15000)
    page.evaluate(
        """() => {
            const button = document.querySelector('#notification-toggle');
            if (!button) throw new Error('notification-toggle missing');
            button.click();
        }"""
    )
    page.wait_for_selector("#notification-panel.show", state="visible", timeout=15000)
    page.evaluate("() => typeof loadNotifications === 'function' && loadNotifications()")
    page.wait_for_function(
        "() => (document.querySelector('#notification-list')?.innerText || '').includes('Phase 1.5')",
        timeout=15000,
    )
    panel_visible = page.locator("#notification-panel.show").count() == 1
    panel_text = page.locator("#notification-list").inner_text(timeout=3000)
    check_ui_quality(rec, page, "notifications_panel")

    user_ctx = page.context.browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 720})
    user_page = user_ctx.new_page()
    user_page.goto(base_url + "/", wait_until="domcontentloaded")
    fetch_json(user_page, "POST", "/api/login", {"username": normal_user["username"], "password": normal_user["password"]})
    cross_read = fetch_json(user_page, "GET", f"/api/notifications?user_id={root_user_id}")
    user_ctx.close()

    dismissed_rows = [
        item for item in (include_dismissed.get("body") or {}).get("notifications") or []
        if item.get("id") == (target or {}).get("id") and item.get("dismissed_at")
    ]
    ok = (
        before["status"] == 200
        and unread["status"] == 200
        and {"info", "warning", "error"}.issubset(severities)
        and dismiss_result["status"] == 200
        and bool(dismissed_rows)
        and panel_visible
        and "Phase 1.5" in panel_text
        and cross_read["status"] == 403
    )
    rec.add(
        "phase15_notification_center",
        ok,
        f"unread={((unread.get('body') or {}).get('unread_count'))}, severities={sorted(filter(None, severities))}, cross_read={cross_read['status']}",
        before=before.get("body"),
        after=after.get("body"),
        dismissed=dismissed_rows[:3],
        cross_read=cross_read.get("body"),
    )
    return {"before": before, "after": after}


def check_share_management(rec: Recorder, page, share_seed: dict[str, Any]) -> dict[str, Any]:
    shares = fetch_json(page, "GET", "/api/shares?limit=120&all=1")
    switch_module_and_wait(page, "shares", "#share-center-list")
    page.wait_for_timeout(800)
    check_ui_quality(rec, page, "share_center_desktop")
    share_text = page.locator("#share-center-list").inner_text(timeout=3000)

    external_safe = page.evaluate(
        """() => {
            renderShareCenter([{
                id: 'external-test',
                share_type: 'video',
                resource_title: 'External URL Test',
                status: 'active',
                created_at: new Date().toISOString(),
                share_url: 'https://evil.example.test/shared',
                access_count: 0,
                max_views: 0,
                password_required: false
            }]);
            return {
                text: document.querySelector('#share-center-list')?.innerText || '',
                copyCount: document.querySelectorAll('[data-share-copy]').length
            };
        }"""
    )
    page.evaluate("() => loadShareCenter && loadShareCenter()")
    page.wait_for_timeout(700)

    events = fetch_json(page, "GET", f"/api/shares/album/{share_seed['album_share_id']}/access-events")
    before_revoke_page = page.evaluate(
        """async path => {
            const res = await fetch(path, {credentials: 'same-origin'});
            return {status: res.status, text: (await res.text()).slice(0, 200)};
        }""",
        share_seed["album_share_url"],
    )
    revoke = fetch_json(page, "POST", f"/api/shares/album/{share_seed['album_share_id']}/revoke")
    after_revoke_page = page.evaluate(
        """async path => {
            const res = await fetch(path, {credentials: 'same-origin'});
            return {status: res.status, text: (await res.text()).slice(0, 300)};
        }""",
        share_seed["album_share_url"],
    )
    after_revoke_api = page.evaluate(
        """async path => {
            const res = await fetch(path, {credentials: 'same-origin'});
            const text = await res.text();
            let body = null;
            try { body = JSON.parse(text); } catch (_) { body = {raw: text.slice(0, 300)}; }
            return {status: res.status, body};
        }""",
        share_seed["album_share_api_url"],
    )
    after_list = fetch_json(page, "GET", "/api/shares?limit=120&all=1")
    types = {item.get("share_type") for item in (shares.get("body") or {}).get("shares") or []}
    ok = (
        shares["status"] == 200
        and {"file", "album", "video"}.issubset(types)
        and "Phase 1.5" in share_text
        and events["status"] == 200
        and len((events.get("body") or {}).get("events") or []) >= 2
        and before_revoke_page["status"] < 400
        and revoke["status"] == 200
        and after_revoke_page["status"] == 200
        and after_revoke_api["status"] in {403, 404}
        and int(external_safe.get("copyCount") or 0) == 0
    )
    rec.add(
        "phase15_share_management",
        ok,
        f"types={sorted(filter(None, types))}, events={events['status']}, revoke={revoke['status']}, after_url={after_revoke_page['status']}",
        shares=(shares.get("body") or {}).get("shares", [])[:10],
        events=events.get("body"),
        before_revoke_page=before_revoke_page,
        after_revoke_page=after_revoke_page,
        after_revoke_api=after_revoke_api,
        after_list=after_list.get("body"),
        external_safe=external_safe,
    )
    return {"shares": shares, "events": events}


def hand_calc_asset_overview(trading_payload: dict[str, Any]) -> dict[str, int]:
    overview = trading_payload.get("overview") if isinstance(trading_payload.get("overview"), dict) else None
    if overview and not any(key in trading_payload for key in ("funding", "spot_summary", "margin_summary", "margin_positions")):
        return {
            "available_points": int(overview.get("available_points") or 0),
            "locked_points": int(overview.get("locked_points") or 0),
            "spot_market_value_points": int(overview.get("spot_market_value_points") or 0),
            "margin_position_equity_points": int(overview.get("margin_position_equity_points") or 0),
            "accrued_interest_points": int(overview.get("accrued_interest_points") or 0),
            "total_equity_points": int(overview.get("total_equity_points") or 0),
        }
    funding = trading_payload.get("funding") or {}
    spot = trading_payload.get("spot_summary") or {}
    margin = trading_payload.get("margin_summary") or {}
    margin_positions = trading_payload.get("margin_positions") or []
    margin_equity = margin.get("total_position_equity_points")
    if margin_equity is None:
        margin_equity = sum(
            int(((row.get("risk") or {}).get("equity_after_points") or row.get("equity_after_points") or 0))
            for row in margin_positions
            if row.get("status") == "open"
        )
    interest = margin.get("total_interest_due_points")
    if interest is None:
        interest = margin.get("total_interest_points")
    if interest is None:
        interest = sum(
            int(((row.get("risk") or {}).get("interest_points") or row.get("interest_points") or 0))
            for row in margin_positions
            if row.get("status") == "open"
        )
    available = int(funding.get("available_points") or 0)
    locked = int(funding.get("locked_points") or 0)
    spot_value = int(spot.get("market_value_points") or spot.get("current_value_points") or spot.get("reference_current_value_points") or 0)
    margin_equity = int(margin_equity or 0)
    return {
        "available_points": available,
        "locked_points": locked,
        "spot_market_value_points": spot_value,
        "margin_position_equity_points": margin_equity,
        "accrued_interest_points": int(interest or 0),
        "total_equity_points": available + locked + spot_value + margin_equity,
    }


def page_text_by_id(page, element_id: str) -> str:
    return page.evaluate(
        """id => (document.getElementById(id)?.textContent || '').trim()""",
        element_id,
    )


def check_trading_asset_overview(rec: Recorder, page, trading_seed: dict[str, Any]) -> dict[str, Any]:
    result = fetch_json(page, "GET", "/api/trading/asset-overview")
    admin = fetch_json(page, "GET", "/api/admin/trading/asset-overview")
    body = result.get("body") or {}
    overview = body.get("overview") or {}
    trading = body.get("trading") or {}
    calc = hand_calc_asset_overview(trading)

    switch_module_and_wait(page, "trading", "#module-trading.active #trading-card")
    page.evaluate(
        """() => {
            if (typeof loadTradingDashboard === 'function') return loadTradingDashboard();
            return Promise.resolve();
        }"""
    )
    page.wait_for_selector("#trading-funding-available", state="visible", timeout=15000)
    for _ in range(3):
        page.evaluate(
            """() => {
                if (typeof loadTradingAssetOverview === 'function') return loadTradingAssetOverview();
                return Promise.resolve();
            }"""
        )
        try:
            page.wait_for_function(
                """() => {
                    const total = (document.getElementById('trading-asset-total-equity')?.textContent || '').trim();
                    const admin = (document.getElementById('trading-asset-admin-risk')?.textContent || '').trim();
                    return total && total !== '-' && admin.includes('管理摘要');
                }""",
                timeout=5000,
            )
            break
        except Exception:
            page.wait_for_timeout(500)
    check_ui_quality(rec, page, "trading_asset_overview_desktop")
    ui_values = {
        "trading_available": page.locator("#trading-funding-available").inner_text(timeout=3000),
        "trading_mode": page.locator("#trading-funding-mode").inner_text(timeout=3000),
        "trading_position": page.locator("#trading-position-quantity").inner_text(timeout=3000),
        "asset_total": page_text_by_id(page, "trading-asset-total-equity"),
        "asset_available": page_text_by_id(page, "trading-asset-available"),
        "asset_spot": page_text_by_id(page, "trading-asset-spot"),
        "asset_margin": page_text_by_id(page, "trading-asset-margin"),
        "asset_interest": page_text_by_id(page, "trading-asset-interest"),
        "confidence": page_text_by_id(page, "trading-asset-confidence"),
        "admin": page_text_by_id(page, "trading-asset-admin-risk"),
    }
    failure_pattern = "**/api/trading/asset-overview"
    failure_body = '{"ok":false,"msg":"phase15 forced failure"}'
    page.route(failure_pattern, lambda route: route.fulfill(status=503, content_type="application/json", body=failure_body))
    try:
        page.evaluate("() => loadTradingAssetOverview({ quiet: false })")
        try:
            page.wait_for_function(
                """() => [
                    document.getElementById('economy-msg')?.textContent || '',
                    document.getElementById('trading-inline-msg')?.textContent || '',
                    document.getElementById('trading-msg')?.textContent || ''
                ].join(String.fromCharCode(10)).includes('phase15 forced failure')""",
                timeout=5000,
            )
        except Exception:
            pass
        error_text = page.evaluate(
            """() => [
                document.getElementById('economy-msg')?.textContent || '',
                document.getElementById('trading-inline-msg')?.textContent || '',
                document.getElementById('trading-msg')?.textContent || ''
            ].join(String.fromCharCode(10)).trim()"""
        )
    finally:
        page.unroute(failure_pattern)

    compare = {key: {"api": overview.get(key), "hand": calc[key]} for key in calc}
    numeric_ok = all(int(overview.get(key) or 0) == int(calc[key] or 0) for key in calc)
    seeded_ok = (
        int(overview.get("available_points") or 0) == int(trading_seed["expected_available_points"])
        and int(overview.get("locked_points") or 0) == int(trading_seed["expected_locked_points"])
        and int(overview.get("spot_market_value_points") or 0) >= int(trading_seed["expected_spot_market_value_points"])
        and int(overview.get("accrued_interest_points") or 0) >= int(trading_seed["expected_accrued_interest_min_points"])
    )
    ok = (
        result["status"] == 200
        and admin["status"] == 200
        and numeric_ok
        and seeded_ok
        and ("價格可信度" in ui_values["confidence"] or "低信心" in ui_values["confidence"])
        and "管理摘要" in ui_values["admin"]
        and "phase15 forced failure" in error_text
        and bool(ui_values["trading_available"])
    )
    rec.add(
        "phase15_trading_asset_overview",
        ok,
        f"numeric_ok={numeric_ok}, seeded_ok={seeded_ok}, forced_error_visible={'phase15 forced failure' in error_text}",
        overview=overview,
        hand_calc=calc,
        compare=compare,
        admin=admin.get("body"),
        ui_values=ui_values,
        forced_error=error_text,
    )
    return {"overview": overview, "hand_calc": calc, "admin": admin.get("body")}


def check_mobile_platform_views(rec: Recorder, page, base_url: str) -> None:
    results = {}
    mobile_page = page.context.new_page()
    try:
        for width, height in ((390, 844), (768, 1024), (1366, 768)):
            mobile_page.set_viewport_size({"width": width, "height": height})
            mobile_page.goto(base_url + "/", wait_until="domcontentloaded")
            wait_for_auth_app(mobile_page, timeout=60000)
            viewport_result = {}
            for module in ("jobs", "shares", "economy"):
                switch_module(mobile_page, module)
                mobile_page.wait_for_timeout(600)
                overflow = mobile_page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                active = mobile_page.locator(f"#module-{module}.active").count() == 1
                viewport_result[module] = {"active": active, "overflow_px": int(overflow)}
            check_ui_quality(rec, mobile_page, f"platform_{width}x{height}", mobile=width <= 640)
            results[f"{width}x{height}"] = viewport_result
    finally:
        mobile_page.close()
    failures = [
        f"{vp}:{module}:{item}"
        for vp, modules in results.items()
        for module, item in modules.items()
        if not item["active"] or int(item["overflow_px"]) > 6
    ]
    rec.add("phase15_mobile_platform_views", not failures, ", ".join(failures) or "jobs/shares/economy fit tested viewports", viewports=results)


def switch_system_tab(page, tab: str) -> None:
    switch_module(page, "system")
    page.evaluate(
        """tab => {
            if (typeof switchSystemTab !== 'function') throw new Error('switchSystemTab missing');
            switchSystemTab(tab);
        }""",
        tab,
    )
    page.wait_for_timeout(900)


def switch_system_tab_and_wait(page, tab: str, section_id: str, ready_selector: str, *, timeout: int = 18000) -> None:
    switch_system_tab(page, tab)
    page.wait_for_selector(f"#{section_id}.active", state="visible", timeout=timeout)
    page.wait_for_selector(ready_selector, state="visible", timeout=timeout)


def active_root_operations_overflow(page, section_id: str) -> dict[str, Any]:
    return page.evaluate(
        """sectionId => {
            const section = document.getElementById(sectionId);
            const module = document.getElementById('module-system');
            const box = section ? section.getBoundingClientRect() : null;
            const rootOverflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
            const sectionOverflow = section ? section.scrollWidth - section.clientWidth : 0;
            const moduleOverflow = module ? module.scrollWidth - module.clientWidth : 0;
            const rightEdgeOverflow = box ? Math.max(0, Math.ceil(box.right - window.innerWidth)) : 0;
            const offenders = [];
            if (section) {
                const sectionBox = section.getBoundingClientRect();
                for (const node of section.querySelectorAll('*')) {
                    const nodeBox = node.getBoundingClientRect();
                    const edgeOverflow = Math.max(0, Math.ceil(nodeBox.right - sectionBox.right));
                    const ownOverflow = Math.max(0, Math.ceil((node.scrollWidth || 0) - (node.clientWidth || 0)));
                    const overflow = Math.max(edgeOverflow, ownOverflow);
                    if (overflow > 3) {
                        offenders.push({
                            tag: node.tagName.toLowerCase(),
                            id: node.id || '',
                            class_name: String(node.className || '').slice(0, 120),
                            overflow_px: overflow,
                            client_width: Math.ceil(node.clientWidth || 0),
                            scroll_width: Math.ceil(node.scrollWidth || 0),
                            right: Math.ceil(nodeBox.right),
                            section_right: Math.ceil(sectionBox.right)
                        });
                    }
                }
                offenders.sort((a, b) => b.overflow_px - a.overflow_px);
            }
            return {
                section_found: !!section,
                active: !!(section && section.classList.contains('active')),
                root_overflow_px: Math.max(0, Math.ceil(rootOverflow)),
                section_overflow_px: Math.max(0, Math.ceil(sectionOverflow)),
                module_overflow_px: Math.max(0, Math.ceil(moduleOverflow)),
                right_edge_overflow_px: rightEdgeOverflow,
                body_width: document.documentElement.clientWidth,
                scroll_width: document.documentElement.scrollWidth,
                offenders: offenders.slice(0, 8)
            };
        }""",
        section_id,
    )


def check_mobile_root_operations(
    rec: Recorder,
    page,
    base_url: str,
    record_browser_error: Callable[[str, str], None] | None = None,
) -> None:
    pages = (
        ("health", "sec-server-health", "#server-health-summary .health-metric-card"),
        ("capacity", "sec-settings-backpressure", "#server-backpressure-chart"),
        ("env", "sec-server-env", "#server-env-summary .server-env-stat-card, #system-resource-gauges .system-resource-gauge-card"),
    )
    results: dict[str, Any] = {}
    timing_snapshots: dict[str, Any] = {}
    root_ctx = page.context.browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
    root_page = root_ctx.new_page()
    if record_browser_error is not None:
        attach_browser_error_handlers(root_page, record_browser_error)
    try:
        login_with_retry(root_page, base_url)
        for width, height in ((390, 844), (768, 1024), (1366, 768)):
            root_page.set_viewport_size({"width": width, "height": height})
            root_page.goto(base_url + "/", wait_until="domcontentloaded")
            wait_for_auth_app(root_page)
            viewport_key = f"{width}x{height}"
            results[viewport_key] = {}
            for tab, section_id, ready_selector in pages:
                if tab == "capacity":
                    switch_system_tab_and_wait(root_page, tab, section_id, ready_selector)
                    root_page.evaluate("() => typeof refreshBackpressureTraffic === 'function' && refreshBackpressureTraffic()")
                    root_page.wait_for_selector(ready_selector, state="visible", timeout=18000)
                elif tab == "env":
                    switch_system_tab_and_wait(root_page, tab, section_id, ready_selector)
                    root_page.evaluate("() => typeof refreshSystemResourceBoard === 'function' && refreshSystemResourceBoard()")
                    root_page.wait_for_selector(ready_selector, state="visible", timeout=18000)
                else:
                    switch_system_tab_and_wait(root_page, tab, section_id, ready_selector)
                overflow = active_root_operations_overflow(root_page, section_id)
                results[viewport_key][tab] = overflow
                check_ui_quality(rec, root_page, f"root_operations_{tab}_{width}x{height}", mobile=width <= 640)

            switch_system_tab_and_wait(
                root_page,
                "health",
                "sec-server-health",
                "#server-health-frontend-observability",
                timeout=18000,
            )
            root_page.wait_for_function(
                "() => (document.querySelector('#server-health-frontend-observability')?.innerText || '').includes('first-summary')",
                timeout=18000,
            )
            timing_text = root_page.locator("#server-health-frontend-observability").inner_text(timeout=5000)
            timing_store = root_page.evaluate("() => window.__hackmeRootAdminTimings || {}")
            timing_snapshots[viewport_key] = {"text": timing_text, "store": timing_store}
    finally:
        root_ctx.close()

    failures = []
    for viewport_key, tabs in results.items():
        for tab, item in tabs.items():
            max_overflow = max(
                int(item.get("root_overflow_px") or 0),
                int(item.get("section_overflow_px") or 0),
                int(item.get("module_overflow_px") or 0),
                int(item.get("right_edge_overflow_px") or 0),
            )
            if not item.get("section_found") or not item.get("active") or max_overflow > 6:
                failures.append(f"{viewport_key}:{tab}:overflow={max_overflow}:active={item.get('active')}")
    timing_failures = []
    for viewport_key, snapshot in timing_snapshots.items():
        store = snapshot.get("store") if isinstance(snapshot.get("store"), dict) else {}
        text = str(snapshot.get("text") or "")
        if "first-summary" not in text or "secondary-chart" not in text:
            timing_failures.append(f"{viewport_key}:health-observability-text")
        for key in ("first-summary", "secondary-chart"):
            value = ((store.get(key) or {}) if isinstance(store.get(key), dict) else {}).get("duration_ms")
            if value is None or float(value) < 0:
                timing_failures.append(f"{viewport_key}:{key}:missing-duration")
    ok = not failures and not timing_failures
    detail = "health/capacity/env mobile root operations fit tested"
    if failures or timing_failures:
        detail = "; ".join([*failures[:8], *timing_failures[:8]])
    rec.add(
        "phase15_mobile_root_operations",
        ok,
        detail,
        viewports=results,
        timing=timing_snapshots,
    )


def write_phase15_report(runtime_root: Path, stamp: str, summary: dict[str, Any]) -> tuple[Path, Path]:
    report_dir = runtime_root / "reports" / "qa"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"playwright_platform_health_check_{stamp}.json"
    md_path = report_dir / f"playwright_platform_health_check_{stamp}.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Platform Center Phase 1.5 Acceptance",
        "",
        f"- Verdict: `{summary['verdict']}`",
        f"- Base URL: `{summary['base_url']}`",
        f"- Runtime root: `{summary['runtime_root']}`",
        f"- Started at: `{summary['started_at']}`",
        f"- Finished at: `{summary['finished_at']}`",
        "",
        "## Checks",
        "",
    ]
    for item in summary["checks"]:
        mark = "PASS" if item["ok"] else "FAIL"
        lines.append(f"- `{mark}` **{item['name']}**: {item.get('detail', '')}")
    lines.extend(["", "## Browser Errors", ""])
    if summary["browser_errors"]:
        for err in summary["browser_errors"]:
            lines.append(f"- `{err['type']}` {err['text']}")
    else:
        lines.append("- none")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase 1.5 platform center Playwright acceptance")
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--keep-server", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--interactive-comfyui", action="store_true")
    parser.add_argument("--comfyui-api-url", default=os.environ.get("PLAYWRIGHT_COMFYUI_API_URL", "").strip())
    parser.add_argument("--comfyui-base-dir", default=os.environ.get("PLAYWRIGHT_COMFYUI_BASE_DIR", "").strip())
    parser.add_argument("--comfyui-start-script", default=os.environ.get("PLAYWRIGHT_COMFYUI_START_SCRIPT", "").strip())
    parser.add_argument("--comfyui-api-host", default=os.environ.get("PLAYWRIGHT_COMFYUI_API_HOST", "").strip())
    parser.add_argument("--comfyui-api-port", type=int, default=None)
    parser.add_argument("--civitai-api-key", default=os.environ.get("PLAYWRIGHT_CIVITAI_API_KEY", os.environ.get("CIVITAI_API_KEY", "")).strip())
    parser.add_argument("--civitai-live-query", default=os.environ.get("PLAYWRIGHT_CIVITAI_QUERY", "sdxl"))
    parser.add_argument("--civitai-live-model-type", default=os.environ.get("PLAYWRIGHT_CIVITAI_MODEL_TYPE", "checkpoint"))
    parser.add_argument("--civitai-live-source", default=os.environ.get("PLAYWRIGHT_CIVITAI_SOURCE", "all"))
    args = parser.parse_args()
    optional_comfyui = collect_optional_comfyui_config(args)

    stamp = utc_stamp()
    runtime_root = Path(args.runtime_root).resolve() if args.runtime_root else Path("/tmp") / f"hackme_web_platform_phase15_{stamp}"
    mkdirs(runtime_root)
    port = free_port()
    started_at = datetime.now(timezone.utc).isoformat()
    server = start_server(runtime_root, port)
    rec = Recorder()
    browser_errors: list[dict[str, str]] = []
    base_url = ""
    seed_summary: dict[str, Any] = {}
    try:
        base_url = wait_for_server(port)
        runtime_isolated = port != 5000 and not runtime_root.is_relative_to(REPO_ROOT)
        rec.add("server_start_isolated", runtime_isolated, f"{base_url}, runtime={runtime_root}", port=port, runtime_root=str(runtime_root), pid=server.pid)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 768})
            seen_errors: set[str] = set()

            def record_browser_error(kind: str, text: str) -> None:
                compact = text.replace("\n", " ")[:500]
                if ignored_browser_error(compact):
                    return
                key = f"{kind}:{compact}"
                if key in seen_errors or len(browser_errors) >= 80:
                    return
                seen_errors.add(key)
                browser_errors.append({"type": kind, "text": compact})

            page = context.new_page()
            attach_browser_error_handlers(page, record_browser_error)
            login_with_retry(page, base_url)
            rec.add("login_root", True, "authenticated root session")
            rec.guard("enable_required_features", lambda: enable_required_features(page, base_url))
            ensure_desktop_auth_app(page)
            rec.guard("optional_comfyui_settings", lambda: apply_optional_comfyui_settings(rec, page, optional_comfyui))
            ensure_desktop_auth_app(page)
            me = fetch_json(page, "GET", "/api/me")
            root_user_id = extract_user_id(me)
            normal_user = ensure_normal_user(page)
            # Ensure trading schema exists before DB-level acceptance fixture.
            fetch_json(page, "GET", "/api/trading/dashboard")
            seed_summary["jobs_notifications"] = seed_job_center_and_notifications(runtime_root, root_user_id)
            seed_summary["shares"] = seed_shares(runtime_root, root_user_id)
            seed_summary["trading"] = seed_trading(runtime_root, root_user_id)
            rec.add("phase15_fixture_seed", True, "seeded isolated QA data", **seed_summary)

            rec.guard("job_center", lambda: check_job_center(rec, page, base_url, normal_user))
            rec.guard("notification_center", lambda: check_notifications(rec, page, base_url, normal_user, root_user_id))
            rec.guard("share_management", lambda: check_share_management(rec, page, seed_summary["shares"]))
            rec.guard("trading_asset_overview", lambda: check_trading_asset_overview(rec, page, seed_summary["trading"]))
            rec.guard("mobile_platform_views", lambda: check_mobile_platform_views(rec, page, base_url))
            rec.guard("mobile_root_operations", lambda: check_mobile_root_operations(rec, page, base_url, record_browser_error))

            screenshot_dir = runtime_root / "reports" / "qa" / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            try:
                page.set_viewport_size({"width": 1366, "height": 768})
                page.goto(base_url + "/", wait_until="domcontentloaded", timeout=15000)
                wait_for_auth_app(page)
                for module in ("jobs", "shares", "economy"):
                    switch_module_and_wait(page, module, f"#module-{module}.active", timeout=15000)
                    page.screenshot(path=str(screenshot_dir / f"phase15_{module}.png"), full_page=True)
                rec.add("phase15_screenshots", True, f"captured screenshots in {screenshot_dir}")
            except Exception as exc:
                rec.add("phase15_screenshots", True, f"skipped non-blocking screenshot capture: {exc}")
            context.close()
            browser.close()
    finally:
        if server.poll() is None and not args.keep_server:
            server.terminate()
            try:
                server.wait(timeout=8)
            except Exception:
                server.kill()
                server.wait(timeout=5)

    finished_at = datetime.now(timezone.utc).isoformat()
    checks = [{"name": r.name, "ok": r.ok, "detail": r.detail, **({"data": r.data} if r.data else {})} for r in rec.results]
    failed = [item for item in checks if not item["ok"]]
    verdict = "PASS" if not failed and not browser_errors else ("PARTIAL" if checks else "FAIL")
    summary = {
        "verdict": verdict,
        "ok": verdict == "PASS",
        "started_at": started_at,
        "finished_at": finished_at,
        "runtime_root": str(runtime_root),
        "base_url": base_url,
        "checks": checks,
        "browser_errors": browser_errors,
        "seed_summary": seed_summary,
        "optional_comfyui": optional_comfyui.safe_summary(),
        "dirty_worktree_risk": "Run git status --short before commit; do not include chess/runtime/storage unrelated changes.",
    }
    json_path, md_path = write_phase15_report(runtime_root, stamp, summary)
    summary["json_report"] = str(json_path)
    summary["markdown_report"] = str(md_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
