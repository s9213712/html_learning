#!/usr/bin/env python3
"""Run the real production-gate generators with per-report live evidence."""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from scripts.security.gate import on_live_reports_make as gate_helpers  # noqa: E402
from services.snapshots import MODE_CONFIRM_PHRASES, PRODUCTION_REQUIRED_REPORT_TYPES  # noqa: E402
from services.users.auth import hash_password  # noqa: E402


BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)

DEFAULT_BACKGROUND_REPORT_TYPES = (
    "pytest",
    "functional",
    "cloud_drive_quota_permission",
)

LIVE_MUTATING_BACKGROUND_REPORT_TYPES = ("pentest",)


@dataclass
class BackgroundReportJob:
    report_type: str
    future: concurrent.futures.Future
    started_at: float


def _human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {sec:02d}s"
    if minutes:
        return f"{minutes}m {sec:02d}s"
    return f"{sec}s"


def _say(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def _progress_bar(index: int, total: int, *, width: int = 20) -> str:
    total = max(1, int(total))
    index = max(0, min(int(index), total))
    filled = int(round(width * index / total))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + f"] {index}/{total}"


def _report_label(report_type: str) -> str:
    labels = {
        "clean_smoke": "clean smoke",
        "adversarial": "adversarial checks",
        "redteam_l2": "red-team L2",
        "pytest": "full pytest suite",
        "log_chain_verify": "audit log chain",
        "integrity_guard": "integrity guard",
        "stress": "HTTP + trading stress",
        "permission": "permission/RBAC pentest",
        "functional": "functional smoke",
        "pentest": "security pentest",
        "snapshot_restore": "snapshot boundary",
        "points_chain_consistency": "points-chain consistency",
        "cloud_drive_quota_permission": "cloud-drive quota/permission",
        "ai_agent_boundary": "AI Agent boundary",
        "operational_campaign_24h": "24-hour operational campaign",
    }
    return labels.get(report_type, report_type.replace("_", " "))


def _report_step(index: int, total: int, report_type: str, stage: str) -> None:
    _say(f"{_progress_bar(index, total)} {report_type}: {stage}")


def _can_prompt_password() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


def _prompt_root_password(args, *, reason: str) -> None:
    if not _can_prompt_password():
        raise SystemExit(
            f"{reason}; pass --root-password, set ROOT_PASSWORD, or rerun from an interactive terminal so the script can prompt."
        )
    _say(f"{reason}; enter the current root password.")
    password = getpass.getpass("Root password: ")
    if not password:
        raise SystemExit("root password prompt was empty")
    args.root_password = password


def _browser_login_with_prompt(
    browser: "BrowserClient",
    args,
    *,
    auto_root_new_password: bool,
    max_attempts: int = 3,
) -> tuple[int, dict, str, list[dict], list[dict]]:
    attempts: list[dict] = []
    for attempt in range(1, max_attempts + 1):
        browser.cookies.clear()
        browser.csrf = ""
        try:
            status, payload, final_password, events = browser.login_with_rotation(
                args.root_username,
                args.root_password,
                rotate_to=args.root_new_password,
            )
        except Exception as exc:
            status = 0
            payload = {"ok": False, "msg": str(exc)}
            final_password = args.root_password
            events = [{"step": "exception", "error": exc.__class__.__name__, "message": str(exc)}]
        attempts.append(
            {
                "attempt": attempt,
                "http_status": status,
                "ok": bool(payload.get("ok")),
                "message": payload.get("msg") or "",
                "prompted_after_failure": False,
            }
        )
        if status == 200 and payload.get("ok"):
            return status, payload, final_password, events, attempts
        if attempt >= max_attempts:
            return status, payload, final_password, events, attempts
        if not _can_prompt_password():
            return status, payload, final_password, events, attempts
        attempts[-1]["prompted_after_failure"] = True
        _prompt_root_password(args, reason=payload.get("msg") or f"root login failed (HTTP {status})")
        if auto_root_new_password:
            args.root_new_password = _derive_rotated_password(args.root_password, "pre_go_live")
    return 0, {"ok": False, "msg": "root login attempts exhausted"}, args.root_password, [], attempts


def _background_report_types(args) -> tuple[str, ...]:
    if args.no_parallel_long_generators:
        return ()
    selected = list(DEFAULT_BACKGROUND_REPORT_TYPES)
    if args.parallel_live_pentest:
        selected.extend(LIVE_MUTATING_BACKGROUND_REPORT_TYPES)
    required = set(PRODUCTION_REQUIRED_REPORT_TYPES)
    return tuple(report_type for report_type in selected if report_type in required)


def _background_status(job: BackgroundReportJob) -> str:
    elapsed = _human_duration(time.perf_counter() - job.started_at)
    if job.future.cancelled():
        return f"{job.report_type}=cancelled after {elapsed}"
    if not job.future.done():
        return f"{job.report_type}=running {elapsed}"
    exc = job.future.exception()
    if exc:
        return f"{job.report_type}=failed after {elapsed}: {exc.__class__.__name__}"
    return f"{job.report_type}=done in {elapsed}"


def _background_status_line(jobs: dict[str, BackgroundReportJob]) -> str:
    if not jobs:
        return "none"
    return "; ".join(_background_status(jobs[report_type]) for report_type in jobs)


def _wait_for_background_payload(
    *,
    job: BackgroundReportJob,
    jobs: dict[str, BackgroundReportJob],
    heartbeat_seconds: float,
) -> dict:
    heartbeat_seconds = max(5.0, float(heartbeat_seconds or 30.0))
    while True:
        try:
            return job.future.result(timeout=heartbeat_seconds)
        except concurrent.futures.TimeoutError:
            _say(f"Long background status: {_background_status_line(jobs)}")


def _run_background_generator(
    report_type: str,
    payload_root: Path,
    raw_root: Path,
    args_snapshot: SimpleNamespace,
    meta: dict,
) -> dict:
    os.environ["HACKME_RUNTIME_DIR"] = str(Path(args_snapshot.runtime_dir).expanduser().resolve())
    worker_client = gate_helpers.LiveClient(
        args_snapshot.base_url,
        timeout=args_snapshot.http_timeout,
        max_retries=args_snapshot.http_retries,
        retry_backoff=args_snapshot.http_retry_backoff,
    )
    worker_args = SimpleNamespace(**vars(args_snapshot))
    worker_args.root_password = worker_client.login(
        worker_args.root_username,
        worker_args.root_password,
        rotate_to=worker_args.root_new_password,
    )
    worker_signer = gate_helpers.PayloadSigner()
    generator = _generator_map()[report_type]
    return generator(payload_root, raw_root, worker_args, worker_signer, dict(meta), worker_client)


@dataclass
class BrowserClient:
    base_url: str
    timeout: int = 60

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.cookies = CookieJar()
        self.ctx = ssl._create_unverified_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.ctx),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )
        self.csrf = ""

    def request(self, path: str, *, method: str = "GET", body: dict | None = None) -> tuple[int, dict, str]:
        headers = {"User-Agent": BROWSER_UA}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace")
                payload = json.loads(text) if text else {}
                return int(resp.status), payload, text
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                payload = json.loads(text) if text else {}
            except Exception:
                payload = {"_raw": text[:2000]}
            return int(exc.code), payload, text

    def fetch_csrf(self) -> str:
        status, payload, _ = self.request("/api/csrf-token")
        if status != 200 or not payload.get("csrf_token"):
            raise RuntimeError(payload.get("msg") or f"failed to fetch csrf token (HTTP {status})")
        self.csrf = str(payload["csrf_token"])
        return self.csrf

    def login(self, username: str, password: str) -> tuple[int, dict]:
        self.fetch_csrf()
        status, payload, _ = self.request(
            "/api/login",
            method="POST",
            body={"username": username, "password": password, "csrf_token": self.csrf},
        )
        return status, payload

    def login_with_rotation(
        self,
        username: str,
        password: str,
        *,
        rotate_to: str = "",
    ) -> tuple[int, dict, str, list[dict]]:
        status, payload = self.login(username, password)
        events: list[dict] = [{"step": "login", "http_status": status, **payload}]
        if status != 200 or not payload.get("ok"):
            return status, payload, password, events
        if not payload.get("must_change_password"):
            return status, payload, password, events
        if not rotate_to:
            raise RuntimeError("root requires password change; rerun with --root-new-password or let the script auto-rotate it")
        self.fetch_csrf()
        me_status, me_payload, _ = self.request("/api/me")
        events.append({"step": "me_after_login", "http_status": me_status, **me_payload})
        user_id = int(me_payload.get("id") or 0) if me_status == 200 else 0
        if user_id <= 0:
            raise RuntimeError("password change required but /api/me did not return the current user id")
        change_status, change_payload, _ = self.request(
            f"/api/admin/users/{user_id}",
            method="PUT",
            body={
                "current_password": password,
                "password": rotate_to,
                "password_confirm": rotate_to,
                "csrf_token": self.csrf,
            },
        )
        events.append({"step": "password_change", "http_status": change_status, **change_payload})
        if change_status != 200 or not change_payload.get("ok"):
            raise RuntimeError(change_payload.get("msg") or f"password rotation failed (HTTP {change_status})")
        self.cookies.clear()
        self.csrf = ""
        relogin_status, relogin_payload, final_password, relogin_events = self.login_with_rotation(
            username,
            rotate_to,
        )
        events.extend(relogin_events)
        return relogin_status, relogin_payload, final_password, events


def _now_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def _derive_rotated_password(current_password: str, label: str) -> str:
    base = str(current_password or "")
    if not base:
        raise ValueError("current password is required before deriving a rotated password")
    safe_label = "".join(ch for ch in str(label or "rotate") if ch.isalnum()) or "rotate"
    candidate = f"{base}.{safe_label}.Aa1!"
    if candidate == base:
        candidate = f"{base}.next.Aa1!"
    return candidate


def _json_dump(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _text_dump(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _git_output(repo_dir: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo_dir), *args], text=True, timeout=20).strip()
    except Exception:
        return ""


def _compute_target_meta(repo_dir: Path, server_mode: str) -> dict:
    return {
        "target_commit": _git_output(repo_dir, "rev-parse", "HEAD"),
        "target_branch": _git_output(repo_dir, "rev-parse", "--abbrev-ref", "HEAD"),
        "server_mode": server_mode,
    }


def _control_db_path(runtime_dir: Path) -> Path:
    return runtime_dir / "database" / "control.db"


def _main_db_path(runtime_dir: Path) -> Path:
    return runtime_dir / "database" / "database.db"


def _canonical_gate_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "reports" / "security" / "production_gate"


def _read_control_row(control_db: Path, report_type: str) -> dict | None:
    conn = sqlite3.connect(control_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM production_entry_reports
            WHERE report_type=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (report_type,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _clear_gate_state(runtime_dir: Path) -> None:
    control_db = _control_db_path(runtime_dir)
    conn = sqlite3.connect(control_db)
    try:
        conn.execute("DELETE FROM production_entry_reports")
        conn.commit()
    finally:
        conn.close()
    canonical_dir = _canonical_gate_dir(runtime_dir)
    canonical_dir.mkdir(parents=True, exist_ok=True)
    for report_type in PRODUCTION_REQUIRED_REPORT_TYPES:
        for suffix in (".json", ".md"):
            path = canonical_dir / f"{report_type}_report{suffix}"
            if path.exists():
                path.unlink()


def _reset_generator_accounts(runtime_dir: Path, *, manager_password: str = "", test_password: str = "") -> dict:
    main_db = _main_db_path(runtime_dir)
    conn = sqlite3.connect(main_db)
    try:
        now = datetime.now().isoformat()
        conn.execute(
            """
            UPDATE users
            SET status='active',
                must_change_password=0,
                is_default_password=0,
                failed_login_count=0,
                locked_until=NULL,
                blocked_until=NULL,
                updated_at=?
            WHERE username IN ('root', 'admin', 'test')
            """,
            (now,),
        )
        reset_passwords = {
            "admin": manager_password,
            "test": test_password,
        }
        for username, password in reset_passwords.items():
            password = str(password or "").strip()
            if not password:
                continue
            row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                continue
            user_id = int(row[0])
            conn.execute(
                "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
                (user_id, hash_password(password), now),
            )
            conn.execute(
                """
                UPDATE users
                SET must_change_password=0,
                    is_default_password=0,
                    failed_login_count=0,
                    locked_until=NULL,
                    blocked_until=NULL,
                    password_changed_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (now, now, user_id),
            )
            conn.execute(
                """
                DELETE FROM user_passwords
                WHERE user_id=? AND id NOT IN (
                    SELECT id FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT 5
                )
                """,
                (user_id, user_id),
            )
        conn.commit()
        rows = conn.execute(
            """
            SELECT username, status, must_change_password, is_default_password
            FROM users
            WHERE username IN ('root', 'admin', 'test')
            ORDER BY username
            """
        ).fetchall()
        return {
            "users": [
                {
                    "username": row[0],
                    "status": row[1],
                    "must_change_password": int(row[2] or 0),
                    "is_default_password": int(row[3] or 0),
                }
                for row in rows
            ]
        }
    finally:
        conn.close()


def _switch_mode(browser: BrowserClient, target_mode: str, *, notes: str) -> dict:
    browser.fetch_csrf()
    status, payload, _ = browser.request(
        "/api/admin/server-mode",
        method="POST",
        body={
            "mode": target_mode,
            "confirm": MODE_CONFIRM_PHRASES.get(target_mode, ""),
            "notes": notes,
            "csrf_token": browser.csrf,
        },
    )
    return {"http_status": status, "payload": payload}


def _current_mode(browser: BrowserClient) -> str:
    status, payload, _ = browser.request("/api/root/server-mode")
    if status != 200 or not payload.get("ok"):
        raise RuntimeError(payload.get("msg") or f"failed to read server mode (HTTP {status})")
    return str((payload.get("mode") or {}).get("current_mode") or "").strip() or "dev_ready"


def _requirements_snapshot(client: gate_helpers.LiveClient) -> dict:
    status, payload, _ = client._request("/api/root/server-mode/requirements")
    return {"http_status": status, **payload}


def _refresh_root_session(client: gate_helpers.LiveClient, args, *, reason: str) -> dict:
    client.cookies.clear()
    client.csrf = ""
    previous_password = args.root_password
    args.root_password = client.login(args.root_username, args.root_password, rotate_to=args.root_new_password)
    return {
        "reason": reason,
        "login_ok": True,
        "password_rotated": args.root_password != previous_password,
    }


def _requirements_snapshot_with_relogin(client: gate_helpers.LiveClient, args, *, reason: str) -> dict:
    status, payload, _ = client._request("/api/root/server-mode/requirements")
    if status in {401, 403}:
        relogin = _refresh_root_session(client, args, reason=f"{reason}:requirements")
        status, payload, _ = client._request("/api/root/server-mode/requirements")
        return {"http_status": status, "relogin": relogin, **payload}
    return {"http_status": status, **payload}


def _upload_payload_with_relogin(client: gate_helpers.LiveClient, args, payload: dict, *, reason: str) -> dict:
    client.fetch_csrf()
    status, upload_payload, _ = client._request(
        "/api/root/production-report/upload",
        method="POST",
        body=payload,
        retryable=True,
    )
    if status not in {401, 403}:
        return {"http_status": status, **upload_payload}
    relogin = _refresh_root_session(client, args, reason=f"{reason}:upload")
    client.fetch_csrf()
    status, upload_payload, _ = client._request(
        "/api/root/production-report/upload",
        method="POST",
        body=payload,
        retryable=True,
    )
    return {"http_status": status, "relogin": relogin, **upload_payload}


def _write_canonical_variant(path: Path, payload: dict | str, *, invalid_json: bool = False) -> None:
    if invalid_json:
        path.write_text("{ invalid json\n", encoding="utf-8")
        return
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
        return
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _report_row_from_requirements(snapshot: dict, report_type: str) -> dict | None:
    reports = snapshot.get("reports") if isinstance(snapshot, dict) else None
    if not isinstance(reports, dict):
        return None
    row = reports.get(report_type)
    return row if isinstance(row, dict) else None


def _build_variant_payload(
    signer: gate_helpers.PayloadSigner,
    *,
    report_type: str,
    raw_report: dict,
    meta: dict,
    target_commit: str,
    target_branch: str,
) -> dict:
    return signer.build(
        report_type=report_type,
        raw_report=raw_report,
        passed=True,
        test_result="pass",
        critical=0,
        high=0,
        unresolved=[],
        tester="scripts/security/gate/full_generator_live_validate.py",
        report_source="scripts/security/gate/full_generator_live_validate.py",
        target_commit=target_commit,
        target_branch=target_branch,
        server_mode=meta["server_mode"],
    )


def _generator_map():
    return {
        "clean_smoke": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._script_report(  # noqa: SLF001
            payload_root,
            raw_root,
            "clean_smoke",
            [os.sys.executable, str(ROOT / "scripts" / "security" / "server_mode" / "server_mode_v2_clean_smoke.py")],
            timeout=args.server_mode_timeout,
            signer=signer,
            meta=meta,
        ),
        "adversarial": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._script_report(  # noqa: SLF001
            payload_root,
            raw_root,
            "adversarial",
            [os.sys.executable, str(ROOT / "scripts" / "security" / "server_mode" / "server_mode_v2_adversarial.py")],
            timeout=args.server_mode_timeout,
            signer=signer,
            meta=meta,
        ),
        "redteam_l2": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._script_report(  # noqa: SLF001
            payload_root,
            raw_root,
            "redteam_l2",
            [os.sys.executable, str(ROOT / "scripts" / "security" / "server_mode" / "server_mode_v2_redteam_l2.py")],
            timeout=args.server_mode_timeout,
            signer=signer,
            meta=meta,
        ),
        "pytest": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._pytest_report(  # noqa: SLF001
            payload_root,
            raw_root,
            "pytest",
            gate_helpers.GO_LIVE_CORE_PYTEST_ARGS,
            timeout=args.pytest_timeout,
            signer=signer,
            meta=meta,
        ),
        "log_chain_verify": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._log_chain_report(  # noqa: SLF001
            payload_root,
            client,
            signer,
            meta,
        ),
        "integrity_guard": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._integrity_report(  # noqa: SLF001
            payload_root,
            client,
            signer,
            meta,
        ),
        "stress": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._stress_report(  # noqa: SLF001
            payload_root,
            raw_root,
            args,
            signer,
            meta,
            client,
        ),
        "permission": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._permission_report(  # noqa: SLF001
            payload_root,
            raw_root,
            args,
            signer,
            meta,
        ),
        "functional": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._functional_report(  # noqa: SLF001
            payload_root,
            raw_root,
            args,
            signer,
            meta,
        ),
        "pentest": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._pentest_report(  # noqa: SLF001
            payload_root,
            raw_root,
            args,
            signer,
            meta,
        ),
        "snapshot_restore": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._pytest_report(  # noqa: SLF001
            payload_root,
            raw_root,
            "snapshot_restore",
            ["tests/snapshots/test_snapshots.py"],
            timeout=args.pytest_timeout,
            signer=signer,
            meta=meta,
        ),
        "points_chain_consistency": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._pytest_report(  # noqa: SLF001
            payload_root,
            raw_root,
            "points_chain_consistency",
            ["tests/points/test_points_chain.py"],
            timeout=args.pytest_timeout,
            signer=signer,
            meta=meta,
        ),
        "cloud_drive_quota_permission": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._pytest_report(  # noqa: SLF001
            payload_root,
            raw_root,
            "cloud_drive_quota_permission",
            ["tests/storage/test_cloud_drive_attachments.py", "tests/storage/test_storage_albums_schema.py"],
            timeout=args.pytest_timeout,
            signer=signer,
            meta=meta,
        ),
        "ai_agent_boundary": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._pytest_report(  # noqa: SLF001
            payload_root,
            raw_root,
            "ai_agent_boundary",
            gate_helpers.AI_AGENT_BOUNDARY_PYTEST_TARGETS,
            timeout=args.pytest_timeout,
            signer=signer,
            meta=meta,
        ),
        "operational_campaign_24h": lambda payload_root, raw_root, args, signer, meta, client: gate_helpers._operational_campaign_report(  # noqa: SLF001
            payload_root,
            args.operational_campaign_report,
            signer=signer,
            meta=meta,
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the real production-gate generators with per-report live evidence.")
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--git-repo-dir", required=True, help="Real git repo used by the live server for current target detection.")
    parser.add_argument("--root-username", default="root")
    from scripts.testing.probe_credentials import (
        add_manager_password_argument,
        add_root_password_argument,
        add_user_password_argument,
    )
    add_root_password_argument(parser)
    add_manager_password_argument(parser)
    add_user_password_argument(parser)
    parser.add_argument("--root-new-password", default="")
    parser.add_argument(
        "--operational-campaign-report",
        default=os.environ.get("HACKME_OPERATIONAL_CAMPAIGN_REPORT", ""),
        help="Formal operational_campaign_24h.json below /tmp.",
    )
    parser.add_argument("--functional-port", type=int, default=50741)
    parser.add_argument("--server-mode-timeout", type=int, default=1800)
    parser.add_argument("--functional-timeout", type=int, default=1800)
    parser.add_argument("--pentest-timeout", type=int, default=3600)
    parser.add_argument("--permission-timeout", type=int, default=3600)
    parser.add_argument("--stress-timeout", type=int, default=900)
    parser.add_argument("--trading-stress-timeout", type=int, default=900)
    parser.add_argument("--pytest-timeout", type=int, default=7200)
    parser.add_argument("--http-timeout", type=int, default=60)
    parser.add_argument("--http-retries", type=int, default=5)
    parser.add_argument("--http-retry-backoff", type=float, default=1.0)
    parser.add_argument(
        "--no-parallel-long-generators",
        action="store_true",
        help="Run every report generator in the foreground, preserving the old sequential behavior.",
    )
    parser.add_argument(
        "--parallel-live-pentest",
        action="store_true",
        help="Also start the live-target pentest generator in the background. Off by default because it mutates the live target.",
    )
    parser.add_argument(
        "--parallel-progress-heartbeat",
        type=float,
        default=30.0,
        help="Seconds between foreground updates while waiting for background long-running generators.",
    )
    parser.add_argument("--i-own-this-target", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    git_repo_dir = Path(args.git_repo_dir).expanduser().resolve()
    os.environ["HACKME_RUNTIME_DIR"] = str(runtime_dir)

    auto_root_new_password = not bool(args.root_new_password)
    if not args.root_password:
        _prompt_root_password(args, reason="root password was not provided")
    run_id = _now_stamp()
    evidence_root = runtime_dir / "reports" / f"server_mode_gate_full_generators_{run_id}"
    payload_root = evidence_root / "normalized_payloads"
    raw_root = evidence_root / "raw_generators"
    canonical_dir = _canonical_gate_dir(runtime_dir)
    canonical_dir.mkdir(parents=True, exist_ok=True)

    if auto_root_new_password:
        args.root_new_password = _derive_rotated_password(args.root_password, "pre_go_live")

    total_reports = len(PRODUCTION_REQUIRED_REPORT_TYPES)
    run_started = time.perf_counter()
    _say("Production gate full-generator validation")
    _say(f"- target: {args.base_url}")
    _say(f"- reports: {total_reports}")
    _say(f"- evidence: {evidence_root}")
    _say("")
    _say("Preparing root session...")
    browser = BrowserClient(args.base_url, timeout=args.http_timeout)
    login_status, login_payload, args.root_password, login_events, login_attempts = _browser_login_with_prompt(
        browser,
        args,
        auto_root_new_password=auto_root_new_password,
    )
    _json_dump(
        evidence_root / "00_browser_login.json",
        {
            "http_status": login_status,
            "final_password_rotated": bool(login_events and any(item.get("step") == "password_change" for item in login_events)),
            "attempts": login_attempts,
            "events": login_events,
            "final_login": login_payload,
        },
    )
    if login_status != 200 or not login_payload.get("ok"):
        _say(f"Root login failed: {login_payload.get('msg') or f'HTTP {login_status}'}")
        raise SystemExit(login_payload.get("msg") or f"browser login failed (HTTP {login_status})")
    _say("Root login OK.")

    starting_mode = _current_mode(browser)
    _json_dump(evidence_root / "01_starting_mode.json", {"current_mode": starting_mode})
    if starting_mode != "dev_ready":
        _say(f"Switching server mode from {starting_mode} to dev_ready for validation setup...")
        switched = _switch_mode(browser, "dev_ready", notes="full generator production gate validation setup")
        _json_dump(evidence_root / "02_switch_to_dev_ready.json", switched)
        if switched["http_status"] != 200 or not switched["payload"].get("ok"):
            _say(f"Mode switch failed: {switched['payload'].get('msg') or switched['http_status']}")
            raise SystemExit(switched["payload"].get("msg") or "failed to switch to dev_ready")
        _say("Mode switch OK.")

    _say("Resetting generator accounts and clearing old production-gate state...")
    _json_dump(
        evidence_root / "03_account_reset.json",
        _reset_generator_accounts(
            runtime_dir,
            manager_password=args.manager_password,
            test_password=args.test_password,
        ),
    )
    _clear_gate_state(runtime_dir)
    _json_dump(
        evidence_root / "04_gate_state_reset.json",
        {
            "control_db": str(_control_db_path(runtime_dir)),
            "canonical_dir": str(canonical_dir),
            "required_reports": list(PRODUCTION_REQUIRED_REPORT_TYPES),
        },
    )
    run_meta = _compute_target_meta(git_repo_dir, "dev_ready")
    _json_dump(evidence_root / "05_run_target_meta.json", run_meta)
    _say(f"Target commit: {run_meta.get('target_commit') or '-'}")
    _say("")

    client = gate_helpers.LiveClient(
        args.base_url,
        timeout=args.http_timeout,
        max_retries=args.http_retries,
        retry_backoff=args.http_retry_backoff,
    )
    args.root_password = client.login(args.root_username, args.root_password, rotate_to=args.root_new_password)
    signer = gate_helpers.PayloadSigner()
    generators = _generator_map()
    old_commit = _git_output(git_repo_dir, "rev-parse", "HEAD^") or ("old-" + run_meta["target_commit"][:12])
    alt_report_type = {key: next(name for name in PRODUCTION_REQUIRED_REPORT_TYPES if name != key) for key in PRODUCTION_REQUIRED_REPORT_TYPES}
    report_summaries = []
    background_jobs: dict[str, BackgroundReportJob] = {}
    background_types = _background_report_types(args)
    executor: concurrent.futures.ThreadPoolExecutor | None = None

    if background_types:
        _say("Starting long-running generators in the background...")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(background_types))
        args_snapshot = SimpleNamespace(**vars(args))
        for report_type in background_types:
            job = BackgroundReportJob(
                report_type=report_type,
                future=executor.submit(
                    _run_background_generator,
                    report_type,
                    payload_root,
                    raw_root,
                    args_snapshot,
                    dict(run_meta),
                ),
                started_at=time.perf_counter(),
            )
            background_jobs[report_type] = job
            _say(f"- {report_type}: background started")
        if not args.parallel_live_pentest and "pentest" in PRODUCTION_REQUIRED_REPORT_TYPES:
            _say("- pentest: foreground later (live-target mutating)")
        _say("")

    foreground_report_types = [report_type for report_type in PRODUCTION_REQUIRED_REPORT_TYPES if report_type not in background_jobs]
    background_report_types = [report_type for report_type in PRODUCTION_REQUIRED_REPORT_TYPES if report_type in background_jobs]
    ordered_report_types = foreground_report_types + background_report_types

    for index, report_type in enumerate(ordered_report_types, start=1):
        report_dir = evidence_root / "reports" / report_type
        report_dir.mkdir(parents=True, exist_ok=True)
        meta = dict(run_meta)
        report_started = time.perf_counter()
        _report_step(index, total_reports, report_type, f"starting {_report_label(report_type)}")
        _json_dump(report_dir / "00_context.json", meta)
        _report_step(index, total_reports, report_type, "refreshing root session")
        _json_dump(report_dir / "00_session_refresh_before_generator.json", _refresh_root_session(client, args, reason=f"before:{report_type}"))
        generator = generators[report_type]
        exception_text = ""
        try:
            if report_type in background_jobs:
                _report_step(index, total_reports, report_type, "collecting background generator")
                _say(f"Long background status: {_background_status_line(background_jobs)}")
                payload = _wait_for_background_payload(
                    job=background_jobs[report_type],
                    jobs=background_jobs,
                    heartbeat_seconds=args.parallel_progress_heartbeat,
                )
            else:
                _report_step(index, total_reports, report_type, "running generator")
                payload = generator(payload_root, raw_root, args, signer, meta, client)
        except Exception:
            exception_text = traceback.format_exc()
            _report_step(index, total_reports, report_type, "generator raised; writing failure payload")
            raw_report = {
                "report_type": report_type,
                "status": "fail",
                "summary": "generator raised exception",
                "generator": f"{generator}",
                "details": {"traceback": exception_text},
                "artifacts": {},
            }
            payload = signer.build(
                report_type=report_type,
                raw_report=raw_report,
                passed=False,
                test_result="fail",
                critical=0,
                high=1,
                unresolved=[{"generator_exception": exception_text.splitlines()[-1] if exception_text else "unknown"}],
                tester="scripts/security/gate/full_generator_live_validate.py",
                report_source="scripts/security/gate/full_generator_live_validate.py",
                target_commit=meta["target_commit"],
                target_branch=meta["target_branch"],
                server_mode=meta["server_mode"],
            )
            _text_dump(report_dir / "00_generator_exception.txt", exception_text)

        _json_dump(report_dir / "01_raw_report.json", payload.get("raw_report") or {})
        _json_dump(report_dir / "02_normalized_payload.json", payload)
        _json_dump(
            report_dir / "03_signature_metadata.json",
            {
                "report_hash": payload.get("report_hash"),
                "signature": payload.get("signature"),
                "key_version": payload.get("key_version"),
                "target_commit": payload.get("target_commit"),
                "target_branch": payload.get("target_branch"),
                "server_mode": payload.get("server_mode"),
            },
        )

        canonical_path = canonical_dir / f"{report_type}_report.json"
        canonical_md = canonical_dir / f"{report_type}_report.md"

        _report_step(index, total_reports, report_type, "checking old-commit rejection")
        old_commit_payload = _build_variant_payload(
            signer,
            report_type=report_type,
            raw_report={**(payload.get("raw_report") or {}), "summary": "old commit mismatch scenario"},
            meta=meta,
            target_commit=old_commit,
            target_branch=meta["target_branch"],
        )
        _write_canonical_variant(canonical_path, old_commit_payload)
        _json_dump(
            report_dir / "04_preupload_old_commit_requirements.json",
            _requirements_snapshot_with_relogin(client, args, reason=f"preupload-old-commit:{report_type}"),
        )

        _report_step(index, total_reports, report_type, "checking report-type mismatch rejection")
        mismatch_payload = _build_variant_payload(
            signer,
            report_type=alt_report_type[report_type],
            raw_report={**(payload.get("raw_report") or {}), "summary": "report_type mismatch scenario"},
            meta=meta,
            target_commit=meta["target_commit"],
            target_branch=meta["target_branch"],
        )
        _write_canonical_variant(canonical_path, mismatch_payload)
        _json_dump(
            report_dir / "05_preupload_report_type_mismatch_requirements.json",
            _requirements_snapshot_with_relogin(client, args, reason=f"preupload-report-type-mismatch:{report_type}"),
        )

        _report_step(index, total_reports, report_type, "checking invalid JSON rejection")
        _write_canonical_variant(canonical_path, "", invalid_json=True)
        _json_dump(
            report_dir / "06_preupload_invalid_json_requirements.json",
            _requirements_snapshot_with_relogin(client, args, reason=f"preupload-invalid-json:{report_type}"),
        )

        _report_step(index, total_reports, report_type, "staging real signed payload")
        _write_canonical_variant(canonical_path, payload)
        if payload_root.joinpath(f"{report_type}_report.md").exists():
            shutil.copy2(payload_root / f"{report_type}_report.md", canonical_md)
        _json_dump(
            report_dir / "07_preupload_real_requirements.json",
            _requirements_snapshot_with_relogin(client, args, reason=f"preupload-real:{report_type}"),
        )

        _report_step(index, total_reports, report_type, "uploading verified report")
        upload_result = _upload_payload_with_relogin(client, args, payload, reason=report_type)
        upload_status = int(upload_result.get("http_status") or 0)
        upload_payload = dict(upload_result)
        upload_payload.pop("http_status", None)
        _json_dump(report_dir / "08_upload_response.json", upload_result)

        _report_step(index, total_reports, report_type, "verifying DB row and requirements")
        db_row = _read_control_row(_control_db_path(runtime_dir), report_type)
        _json_dump(report_dir / "09_db_row_verification.json", db_row or {})

        after_upload = _requirements_snapshot_with_relogin(client, args, reason=f"postupload-real:{report_type}")
        _json_dump(report_dir / "10_postupload_requirements.json", after_upload)

        _report_step(index, total_reports, report_type, "checking DB fallback over bad filesystem payloads")
        _write_canonical_variant(canonical_path, old_commit_payload)
        _json_dump(
            report_dir / "11_postupload_old_commit_requirements.json",
            _requirements_snapshot_with_relogin(client, args, reason=f"postupload-old-commit:{report_type}"),
        )

        _write_canonical_variant(canonical_path, mismatch_payload)
        _json_dump(
            report_dir / "12_postupload_report_type_mismatch_requirements.json",
            _requirements_snapshot_with_relogin(client, args, reason=f"postupload-report-type-mismatch:{report_type}"),
        )

        _write_canonical_variant(canonical_path, "", invalid_json=True)
        _json_dump(
            report_dir / "13_postupload_invalid_json_requirements.json",
            _requirements_snapshot_with_relogin(client, args, reason=f"postupload-invalid-json:{report_type}"),
        )

        _write_canonical_variant(canonical_path, payload)
        if payload_root.joinpath(f"{report_type}_report.md").exists():
            shutil.copy2(payload_root / f"{report_type}_report.md", canonical_md)

        selected_after_upload = _report_row_from_requirements(after_upload, report_type) or {}
        selected_after_invalid = _report_row_from_requirements(
            json.loads((report_dir / "13_postupload_invalid_json_requirements.json").read_text(encoding="utf-8")),
            report_type,
        ) or {}
        summary = {
            "report_type": report_type,
            "generator_passed": bool(payload.get("pass")),
            "upload_ok": upload_status == 200 and bool(upload_payload.get("ok")),
            "db_row_id": (db_row or {}).get("id"),
            "report_type_correct": selected_after_upload.get("report_type") == report_type,
            "target_commit_correct": selected_after_upload.get("target_commit") == meta["target_commit"],
            "target_branch_correct": selected_after_upload.get("target_branch") == meta["target_branch"],
            "server_mode_correct": selected_after_upload.get("server_mode") == meta["server_mode"],
            "signature_valid": bool(selected_after_upload.get("signature_valid")),
            "trust_level_verified": str(selected_after_upload.get("trust_level") or "").strip() == "verified",
            "db_wins_over_invalid_json": selected_after_invalid.get("id") == (db_row or {}).get("id"),
            "generator_exception": bool(exception_text),
        }
        _json_dump(report_dir / "14_validation_summary.json", summary)
        report_summaries.append(summary)
        status = "PASS" if summary["generator_passed"] and summary["upload_ok"] and summary["signature_valid"] and summary["trust_level_verified"] else "CHECK"
        _report_step(
            index,
            total_reports,
            report_type,
            f"{status} in {_human_duration(time.perf_counter() - report_started)}",
        )
        if background_jobs and report_type not in background_jobs:
            _say(f"Long background status: {_background_status_line(background_jobs)}")
        _say("")

    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)

    _say("Checking final production requirements before GO_LIVE...")
    final_requirements = _requirements_snapshot_with_relogin(client, args, reason="final")
    _json_dump(evidence_root / "90_final_requirements_before_go_live.json", final_requirements)
    _say(f"Final requirements ok: {bool(final_requirements.get('ok'))}")

    _say("Reauthenticating root before GO_LIVE...")
    browser = BrowserClient(args.base_url, timeout=args.http_timeout)
    relogin_status, relogin_payload, args.root_password, relogin_events, relogin_attempts = _browser_login_with_prompt(
        browser,
        args,
        auto_root_new_password=auto_root_new_password,
    )
    _json_dump(
        evidence_root / "91_browser_relogin_before_go_live.json",
        {
            "http_status": relogin_status,
            "final_password_rotated": bool(relogin_events and any(item.get("step") == "password_change" for item in relogin_events)),
            "attempts": relogin_attempts,
            "events": relogin_events,
            "final_login": relogin_payload,
        },
    )
    if relogin_status != 200 or not relogin_payload.get("ok"):
        _say(f"Root relogin failed: {relogin_payload.get('msg') or f'HTTP {relogin_status}'}")
        raise SystemExit(relogin_payload.get("msg") or f"browser relogin failed (HTTP {relogin_status})")

    _say("Submitting GO_LIVE request...")
    browser.fetch_csrf()
    enter_status, enter_payload, _ = browser.request(
        "/api/root/production/enter",
        method="POST",
        body={"confirm": "GO_LIVE", "reason": "full generator production gate validation", "csrf_token": browser.csrf},
    )
    _json_dump(evidence_root / "92_enter_production.json", {"http_status": enter_status, **enter_payload})
    _say(f"GO_LIVE result: HTTP {enter_status}, ok={bool(enter_payload.get('ok'))}")

    _say("Checking root login and security center after GO_LIVE...")
    post_go_live_password = _derive_rotated_password(args.root_password, "post_go_live")
    args.root_new_password = post_go_live_password
    post_login_status, post_login_payload, args.root_password, post_login_events, post_login_attempts = _browser_login_with_prompt(
        browser,
        args,
        auto_root_new_password=False,
    )
    _json_dump(
        evidence_root / "93_browser_login_after_go_live.json",
        {
            "http_status": post_login_status,
            "final_password_rotated": bool(post_login_events and any(item.get("step") == "password_change" for item in post_login_events)),
            "attempts": post_login_attempts,
            "events": post_login_events,
            "final_login": post_login_payload,
        },
    )
    if post_login_status != 200 or not post_login_payload.get("ok"):
        _say(f"Root login after GO_LIVE failed: {post_login_payload.get('msg') or f'HTTP {post_login_status}'}")
        raise SystemExit(post_login_payload.get("msg") or f"browser login after go-live failed (HTTP {post_login_status})")

    mode_status, mode_payload, _ = browser.request("/api/root/server-mode")
    security_status, security_payload, _ = browser.request("/api/admin/security-center")
    _json_dump(evidence_root / "94_mode_after_go_live.json", {"http_status": mode_status, **mode_payload})
    _json_dump(evidence_root / "95_security_center_after_go_live.json", {"http_status": security_status, **security_payload})
    final_target_meta = _compute_target_meta(git_repo_dir, "dev_ready")
    _json_dump(evidence_root / "97_final_target_meta.json", final_target_meta)

    main_conn = sqlite3.connect(_main_db_path(runtime_dir))
    try:
        db_settings = dict(
            main_conn.execute(
                """
                SELECT key, value
                FROM system_settings
                WHERE key IN (
                    'allow_register',
                    'captcha_mode',
                    'production_single_account_ip_lock_enabled',
                    'production_single_ip_account_lock_enabled',
                    'audit_chain_enabled',
                    'browser_only_mode_enabled',
                    'integrity_guard_strict_mode',
                    'server_ssl_enabled'
                )
                """
            ).fetchall()
        )
    finally:
        main_conn.close()
    _json_dump(evidence_root / "96_security_center_db_settings.json", db_settings)

    summary = {
        "base_url": args.base_url,
        "runtime_dir": str(runtime_dir),
        "git_repo_dir": str(git_repo_dir),
        "evidence_root": str(evidence_root),
        "run_target_meta": run_meta,
        "final_target_meta": final_target_meta,
        "target_meta_drifted_during_run": final_target_meta != run_meta,
        "full_generator_report_count": len(report_summaries),
        "parallel_background_reports": list(background_jobs.keys()),
        "parallel_background_status": [_background_status(job) for job in background_jobs.values()],
        "generators_passed": [item["report_type"] for item in report_summaries if item["generator_passed"]],
        "generators_failed": [item["report_type"] for item in report_summaries if not item["generator_passed"]],
        "final_requirements_ok": bool(final_requirements.get("ok")),
        "enter_production_http_status": enter_status,
        "enter_production_ok": bool(enter_payload.get("ok")),
        "post_go_live_mode_http_status": mode_status,
        "post_go_live_security_center_http_status": security_status,
        "security_center_has_missing_keys": [
            key
            for key in (
                "allow_register",
                "captcha_mode",
                "production_single_account_ip_lock_enabled",
                "production_single_ip_account_lock_enabled",
            )
            if key not in ((security_payload.get("security_center") or {}).get("settings") or {})
        ],
        "db_settings": db_settings,
        "report_summaries": report_summaries,
    }
    _json_dump(evidence_root / "SUMMARY.json", summary)
    _text_dump(
        evidence_root / "SUMMARY.md",
        "\n".join(
            [
                "# Full Generator Production Gate Validation",
                "",
                f"- base_url: `{args.base_url}`",
                f"- runtime_dir: `{runtime_dir}`",
                f"- git_repo_dir: `{git_repo_dir}`",
                f"- final_requirements_ok: `{summary['final_requirements_ok']}`",
                f"- enter_production_ok: `{summary['enter_production_ok']}`",
                f"- generators_failed: `{', '.join(summary['generators_failed']) or 'none'}`",
            ]
        ),
    )
    _say("")
    _say("Production gate full-generator validation complete.")
    _say(f"- reports generated: {summary['full_generator_report_count']}/{total_reports}")
    _say(f"- final requirements ok: {summary['final_requirements_ok']}")
    _say(f"- GO_LIVE ok: {summary['enter_production_ok']}")
    _say(f"- elapsed: {_human_duration(time.perf_counter() - run_started)}")
    _say(f"- summary: {evidence_root / 'SUMMARY.json'}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["final_requirements_ok"] and summary["enter_production_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
