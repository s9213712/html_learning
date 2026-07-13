#!/usr/bin/env python3
"""Run long-tail economy, private-chain, and full-site probes in one runtime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.playwright_deep_site_check import (  # noqa: E402
    ROOT_PASSWORD,
    TEST_PASSWORD,
    free_port,
    mkdirs,
    start_server,
    utc_stamp,
    wait_for_server,
)
from services.platform.settings import FEATURE_FLAG_KEYS  # noqa: E402


def api_json(session: requests.Session, method: str, url: str, *, csrf: str = "", **kwargs) -> dict[str, Any]:
    headers = dict(kwargs.pop("headers", {}) or {})
    if csrf and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        headers.setdefault("X-CSRF-Token", csrf)
    started = time.perf_counter()
    try:
        response = session.request(method.upper(), url, headers=headers, timeout=20, verify=False, **kwargs)
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:500]}
        if not isinstance(body, dict):
            body = {"body": body}
        body.setdefault("ok", response.ok)
        return {
            "ok": response.ok and bool(body.get("ok", True)),
            "status": int(response.status_code),
            "body": body,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "body": {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"},
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def root_session(base_url: str, root_password: str) -> tuple[requests.Session, str, dict[str, Any]]:
    session = requests.Session()
    session.verify = False
    csrf_result = api_json(session, "GET", f"{base_url}/api/csrf-token")
    csrf = str((csrf_result.get("body") or {}).get("csrf_token") or session.cookies.get("csrf_token") or "")
    login_result = api_json(
        session,
        "POST",
        f"{base_url}/api/login",
        csrf=csrf,
        json={"username": "root", "password": root_password},
    )
    csrf = str(session.cookies.get("csrf_token") or csrf)
    return session, csrf, {"csrf": csrf_result, "login": login_result}


def enable_probe_features(base_url: str, root_password: str) -> dict[str, Any]:
    session, csrf, auth = root_session(base_url, root_password)
    feature_payload = {key: True for key in FEATURE_FLAG_KEYS}
    feature_result = {"ok": False, "status": 0, "body": {"ok": False, "error": "login_failed"}}
    if auth["login"].get("ok"):
        feature_result = api_json(
            session,
            "PUT",
            f"{base_url}/api/admin/features",
            csrf=csrf,
            json=feature_payload,
        )
    return {
        "ok": bool(auth["csrf"].get("ok") and auth["login"].get("ok") and feature_result.get("ok")),
        **auth,
        "features": feature_result,
        "feature_count": len(feature_payload),
    }


def provision_probe_accounts(
    base_url: str,
    root_password: str,
    *,
    prefix: str,
    count: int,
    password: str,
) -> dict[str, Any]:
    session, csrf, auth = root_session(base_url, root_password)
    if not auth["login"].get("ok"):
        return {"ok": False, **auth, "accounts": [], "error": "root_login_failed"}
    accounts = []
    for index in range(1, max(2, int(count)) + 1):
        username = f"{prefix}{index:02d}"
        lookup = api_json(session, "GET", f"{base_url}/api/admin/users?q={username}&page_size=100")
        users = (lookup.get("body") or {}).get("users") or []
        created = None
        if not any(str(item.get("username") or "") == username for item in users):
            created = api_json(
                session,
                "POST",
                f"{base_url}/api/admin/users",
                csrf=csrf,
                json={
                    "username": username,
                    "password": password,
                    "password_confirm": password,
                    "nickname": f"Long Needle {index:02d}",
                    "role": "user",
                    "status": "active",
                },
            )
            if int(created.get("status") or 0) not in {200, 201, 409}:
                return {
                    "ok": False,
                    **auth,
                    "accounts": [item[0] for item in accounts],
                    "error": f"account_create_failed:{username}:{created.get('status')}",
                }
        member = requests.Session()
        member.verify = False
        member_csrf = api_json(member, "GET", f"{base_url}/api/csrf-token")
        member_token = str((member_csrf.get("body") or {}).get("csrf_token") or member.cookies.get("csrf_token") or "")
        login = api_json(
            member,
            "POST",
            f"{base_url}/api/login",
            csrf=member_token,
            json={"username": username, "password": password},
        )
        if not login.get("ok"):
            return {
                "ok": False,
                **auth,
                "accounts": [item[0] for item in accounts],
                "error": f"account_login_failed:{username}:{login.get('status')}",
            }
        accounts.append((username, password))
    return {"ok": True, **auth, "accounts": accounts}


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        return {"ok": False, "error": f"could not read {path}: {exc}"}


def profile_defaults(profile: str) -> dict[str, int]:
    if profile == "long":
        return {
            "accounts": 12,
            "transfer_ops": 80,
            "direct_transfer_ops": 500,
            "trading_ops": 60,
            "points_concurrency": 12,
            "system_ops": 1200,
            "system_logical_users": 1200,
            "system_concurrency": 96,
            "session_pool": 24,
        }
    if profile == "medium":
        return {
            "accounts": 8,
            "transfer_ops": 36,
            "direct_transfer_ops": 120,
            "trading_ops": 24,
            "points_concurrency": 8,
            "system_ops": 320,
            "system_logical_users": 320,
            "system_concurrency": 32,
            "session_pool": 10,
        }
    return {
        "accounts": 4,
        "transfer_ops": 12,
        "direct_transfer_ops": 24,
        "trading_ops": 8,
        "points_concurrency": 4,
            # Rotation must complete at least one full pass over every
            # operation.  Keep quick runs bounded, but leave enough work for
            # slow/failed requests not to truncate the coverage proof.
            "system_ops": 420,
            "system_logical_users": 420,
        "system_concurrency": 12,
        "session_pool": 4,
    }


def run_child(label: str, command: list[str], *, stdout_path: Path, timeout: int, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout:
            proc = subprocess.run(
                command,
                cwd=str(ROOT),
                env={**os.environ, **(env or {})},
                stdout=stdout,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        with stdout_path.open("a", encoding="utf-8") as stdout:
            stdout.write(f"\n[TIMEOUT] {label} exceeded {timeout}s\n")
        returncode = 124
    return {
        "label": label,
        "returncode": returncode,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "stdout": str(stdout_path),
        "command": sanitized_command(command),
    }


def sanitized_command(command: list[str]) -> list[str]:
    redacted = []
    hide_next = False
    for value in command:
        if hide_next:
            redacted.append("[redacted]")
            hide_next = False
            continue
        matched = next((flag for flag in {"--root-password", "--test-password", "--accounts"} if value.startswith(f"{flag}=")), "")
        if matched:
            redacted.append(f"{matched}=[redacted]")
            continue
        redacted.append(value)
        if value in {"--root-password", "--test-password", "--accounts"}:
            hide_next = True
    return redacted


def summarize_child(label: str, child: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    findings = []
    if int(child.get("returncode") or 0) != 0:
        findings.append({
            "severity": "high",
            "title": f"{label} probe exited non-zero",
            "returncode": child.get("returncode"),
            "stdout": child.get("stdout"),
        })
    for item in payload.get("findings") or []:
        if isinstance(item, dict):
            findings.append({"source": label, **item})
    for reason in payload.get("degraded_reasons") or []:
        findings.append({
            "source": label,
            "severity": "medium",
            "title": "probe reported degraded experience",
            "reason": reason,
        })
    if payload.get("ok") is False and not findings:
        findings.append({
            "source": label,
            "severity": "medium",
            "title": f"{label} payload ok=false",
            "error": payload.get("error") or payload.get("msg") or "",
        })
    return {
        "label": label,
        "ok": int(child.get("returncode") or 0) == 0 and payload.get("ok") is not False,
        "returncode": child.get("returncode"),
        "elapsed_seconds": child.get("elapsed_seconds"),
        "artifact": payload.get("_artifact_path"),
        "stdout": child.get("stdout"),
        "findings": findings,
    }


def write_markdown(out_path: Path, payload: dict[str, Any]) -> Path:
    md_path = out_path.with_suffix(".md")
    lines = [
        "# Long Needle Simulation Probe",
        "",
        f"- Verdict: `{payload['verdict']}`",
        f"- Profile: `{payload['profile']}`",
        f"- Base URL: `{payload['base_url']}`",
        f"- Runtime root: `{payload['runtime_root']}`",
        f"- Started at: `{payload['started_at']}`",
        f"- Finished at: `{payload['finished_at']}`",
        "",
        "## Probes",
    ]
    for item in payload.get("probes") or []:
        lines.extend([
            "",
            f"### {item['label']}",
            "",
            f"- OK: `{item['ok']}`",
            f"- Return code: `{item['returncode']}`",
            f"- Elapsed: `{item['elapsed_seconds']}s`",
            f"- Artifact: `{item.get('artifact') or '-'}`",
            f"- Stdout: `{item.get('stdout') or '-'}`",
        ])
    lines.extend(["", "## Findings"])
    findings = payload.get("findings") or []
    if not findings:
        lines.append("")
        lines.append("No high-signal latent issue was confirmed in this run.")
    for finding in findings:
        lines.append("")
        lines.append(f"- `{finding.get('severity', 'unknown')}` {finding.get('title') or finding.get('reason') or finding}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run economy/private-chain/full-site long-tail simulation probes.")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--profile", choices=["quick", "medium", "long"], default="quick")
    parser.add_argument("--root-password", default=os.environ.get("HACKME_LONG_NEEDLE_ROOT_PASSWORD", ""))
    parser.add_argument("--test-password", default=os.environ.get("HACKME_LONG_NEEDLE_TEST_PASSWORD", ""))
    parser.add_argument("--keep-server", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.base_url and (not args.root_password or not args.test_password):
        parser.error(
            "existing --base-url targets require HACKME_LONG_NEEDLE_ROOT_PASSWORD/--root-password "
            "and HACKME_LONG_NEEDLE_TEST_PASSWORD/--test-password"
        )
    if not args.base_url:
        args.root_password = args.root_password or ROOT_PASSWORD
        args.test_password = args.test_password or TEST_PASSWORD
    requests.packages.urllib3.disable_warnings()

    stamp = utc_stamp()
    runtime_root = Path(args.runtime_root).resolve() if args.runtime_root else Path("/tmp") / f"hackme_web_long_needle_{stamp}"
    mkdirs(runtime_root)
    report_dir = runtime_root / "reports" / "qa"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out).resolve() if args.out else report_dir / f"long_needle_simulation_{stamp}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    server = None
    base_url = args.base_url.rstrip("/")
    started_at = datetime.now(timezone.utc).isoformat()
    if not base_url:
        port = free_port()
        server = start_server(runtime_root, port)
        base_url = wait_for_server(port)

    defaults = profile_defaults(args.profile)
    account_prefix = f"needle{stamp.lower().replace('t', '').replace('z', '')[-10:]}"
    account_setup = provision_probe_accounts(
        base_url,
        args.root_password,
        prefix=account_prefix,
        count=defaults["accounts"],
        password=args.test_password,
    )
    setup = {
        "enable_features": enable_probe_features(base_url, args.root_password),
        "accounts": {
            "ok": account_setup.get("ok"),
            "count": len(account_setup.get("accounts") or []),
            "usernames": [item[0] for item in account_setup.get("accounts") or []],
            "error": account_setup.get("error") or "",
        },
    }
    account_spec = ",".join(f"{username}:{password}" for username, password in account_setup.get("accounts") or [])
    children: list[dict[str, Any]] = []
    payloads: dict[str, dict[str, Any]] = {}
    try:
        points_out = report_dir / f"long_needle_points_chain_{stamp}.json"
        children.append(run_child(
            "economy_private_chain",
            [
                sys.executable,
                "scripts/testing/points_chain_destructive_stress.py",
                "--base-url",
                base_url,
                "--runtime-root",
                str(runtime_root),
                "--out",
                str(points_out),
                "--accounts",
                str(defaults["accounts"]),
                "--transfer-ops",
                str(defaults["transfer_ops"]),
                "--direct-transfer-ops",
                str(defaults["direct_transfer_ops"]),
                "--trading-ops",
                str(defaults["trading_ops"]),
                "--concurrency",
                str(defaults["points_concurrency"]),
                "--server-pids",
                str(server.pid if server else ""),
            ],
            stdout_path=report_dir / f"long_needle_points_chain_{stamp}.stdout",
            timeout=args.timeout_seconds,
            env={
                "HACKME_POINTS_STRESS_ROOT_PASSWORD": args.root_password,
                "HACKME_SERVER_PIDS": str(server.pid if server else ""),
            },
        ))
        payloads["economy_private_chain"] = {**load_json(points_out), "_artifact_path": str(points_out)}

        system_out = report_dir / f"long_needle_full_feature_{stamp}.json"
        children.append(run_child(
            "full_feature",
            [
                sys.executable,
                "scripts/testing/system_stress_probe.py",
                "--base-url",
                base_url,
                "--runtime-root",
                str(runtime_root),
                "--out",
                str(system_out),
                "--session-mode",
                "clone",
                "--session-pool",
                str(defaults["session_pool"]),
                "--logical-users",
                str(defaults["system_logical_users"]),
                "--ops",
                str(defaults["system_ops"]),
                "--concurrency",
                str(defaults["system_concurrency"]),
                "--allow-server-busy",
                "--max-server-busy-rate",
                "0.25" if args.profile == "quick" else ("0.15" if args.profile == "medium" else "0.10"),
                "--operation-mode",
                "rotation",
                "--require-all-accounts",
                "--require-operation-coverage",
                "--server-pids",
                str(server.pid if server else ""),
            ],
            stdout_path=report_dir / f"long_needle_full_feature_{stamp}.stdout",
            timeout=args.timeout_seconds,
            env={
                "HACKME_STRESS_ACCOUNTS": account_spec,
                "HACKME_STRESS_TEST_PASSWORD": args.test_password,
                "HACKME_SERVER_PIDS": str(server.pid if server else ""),
            },
        ))
        payloads["full_feature"] = {**load_json(system_out), "_artifact_path": str(system_out)}
    finally:
        if server and server.poll() is None and not args.keep_server:
            server.terminate()
            try:
                server.wait(timeout=8)
            except Exception:
                server.kill()
                server.wait(timeout=5)

    probes = [summarize_child(child["label"], child, payloads.get(child["label"], {})) for child in children]
    findings = [finding for item in probes for finding in item.get("findings") or []]
    if not (setup.get("enable_features") or {}).get("ok"):
        findings.insert(0, {
            "severity": "high",
            "title": "long needle feature setup failed",
            "setup": setup.get("enable_features"),
        })
    if not (setup.get("accounts") or {}).get("ok"):
        findings.insert(0, {
            "severity": "high",
            "title": "long needle account provisioning failed",
            "setup": setup.get("accounts"),
        })
    payload = {
        "ok": not findings and all(item.get("ok") for item in probes),
        "verdict": "PASS" if not findings and all(item.get("ok") for item in probes) else "FAIL",
        "profile": args.profile,
        "base_url": base_url,
        "runtime_root": str(runtime_root),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "setup": setup,
        "probes": probes,
        "findings": findings,
        "raw": payloads,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_path = write_markdown(out_path, payload)
    payload["markdown_report"] = str(md_path)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
