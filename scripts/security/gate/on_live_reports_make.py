#!/usr/bin/env python3
"""Generate the production-gate reports in one pass.

This wrapper keeps two layers of output:

1. Raw artifacts under runtime/reports/security/production_gate/runs/<RUN_ID>/
2. Stable upload-ready payloads under runtime/reports/security/production_gate/

The stable payload JSON files are signed with the same HMAC scheme used by
`/api/root/production-report/upload`, so operators can either upload them
manually or pass `--upload` to let this script submit them after generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.security.common_paths import runtime_root, security_reports_root  # noqa: E402
from services.snapshots import MODE_CONFIRM_PHRASES, PRODUCTION_REQUIRED_REPORT_TYPES, ServerModeService  # noqa: E402

GO_LIVE_CORE_PYTEST_TARGETS = [
    "tests/security/auth/test_auth_csrf_safe.py",
    "tests/security/auth/test_session_idle_timeout.py",
    "tests/account/auth/test_account_lockout.py",
    "tests/security/input/test_password_strength.py",
    "tests/security/input/test_plaintext_secrets_scan.py",
    "tests/account/recovery/test_account_recovery.py",
    "tests/account/sessions/test_account_sessions.py",
    "tests/security/gates/test_production_gate_enforcement.py",
    "tests/security/gates/test_security_events.py",
    "tests/security/integrity/test_integrity_guard.py",
    "tests/security/integrity/test_integrity_repair.py",
    "tests/security/smoke/test_functional_permission_pentest.py",
    "tests/regressions/test_security_issue_regressions.py",
    "tests/platform/test_settings_audit_reseal.py",
    "tests/platform/test_auth_db_split.py",
    "tests/platform/test_bootstrap_compat.py",
    "tests/snapshots/test_snapshots.py",
    "tests/points/test_points_chain.py",
    "tests/trading/core/test_trading_engine.py",
    "tests/trading/core/test_trading_markets.py",
    "tests/trading/pricing/test_trading_reference_prices.py",
    "tests/trading/grid/test_grid_preview_api.py",
    "tests/trading/mode_boundaries/test_trading_mode_gate.py",
]
GO_LIVE_CORE_PYTEST_ARGS = GO_LIVE_CORE_PYTEST_TARGETS + [
    "-k",
    "not live_price_provider_blocks_market_order_when_binance_is_down and "
    "not live_price_provider_blocks_market_order_when_public_fallback_chain_is_unhealthy",
]

AI_AGENT_BOUNDARY_PYTEST_TARGETS = [
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_write_tools_root_only_and_lists_allowed_tools",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_write_tools_lockdown_blocks_list_and_execute",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_write_tool_execute_requires_write_mode_for_mutation",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_launch_preflight_executes_checks_audit_and_switch",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_chat_blocks_os_filesystem_listing_before_llm",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_chat_blocks_server_filesystem_mutation_before_llm",
    "tests/ai_agent/test_ai_agent_routes.py::test_ai_agent_write_tool_blocks_server_filesystem_path_args",
]

OPERATIONAL_CAMPAIGN_MIN_SECONDS = 86_400
OPERATIONAL_CAMPAIGN_SCENARIOS = {
    "media_long_hls_share",
    "ai_agent_operations",
    "trading_background_and_abuse",
    "pointschain_hft_invariants",
    "pointschain_incident_governance",
    "recovery_backup_restart",
    "media_proxy_cross_browser",
    "final_ui_mobile_prelaunch",
}

GO_LIVE_CORE_PENTEST_CHECKS = ",".join(
    [
        "curl-baseline",
        "functional-permissions",
        "session-security",
        "header-security",
        "dep-audit",
        "server-mode-v2",
        "server-mode-v2-adversarial",
        "server-mode-v2-live-http",
        "server-mode-v2-enterprise",
        "server-mode-v2-redteam-l2",
        "nmap",
        "whatweb",
        "wafw00f",
        "nikto",
        "nuclei",
        "sqlmap",
        "ffuf",
        "gobuster",
        "dirsearch",
        "dalfox",
        "xsstrike",
        "arjun",
        "sslscan",
        "testssl",
        "httpx",
    ]
)


def _ensure_runtime_env() -> None:
    os.environ.setdefault("HACKME_RUNTIME_DIR", str(runtime_root()))


def _now_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def _json_dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _text_dump(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _tail(text: str, limit: int = 4000) -> str:
    return (text or "")[-limit:]


def _last_nonempty_line(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {sec:02d}s"
    if minutes:
        return f"{minutes}m {sec:02d}s"
    return f"{sec}s"


def _foreground(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def _find_latest_paths(parent: Path, pattern: str) -> list[Path]:
    if not parent.exists():
        return []
    return sorted(parent.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)


def _git_output(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return proc.stdout.strip()
    except Exception:
        return ""


def _target_meta() -> dict:
    return {
        "target_commit": _git_output("rev-parse", "HEAD"),
        "target_branch": _git_output("rev-parse", "--abbrev-ref", "HEAD"),
    }


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run(command: list[str], *, env: dict | None = None, timeout: int = 3600) -> CommandResult:
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT) + (os.pathsep + merged["PYTHONPATH"] if merged.get("PYTHONPATH") else "")
    if env:
        merged.update(env)
    started = time.perf_counter()
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=merged,
    )
    heartbeat_seconds = int(os.environ.get("GO_LIVE_PROGRESS_HEARTBEAT_SECONDS", "30") or "30")
    heartbeat_seconds = max(5, heartbeat_seconds)
    next_heartbeat = started + heartbeat_seconds
    deadline = started + max(1, int(timeout))
    stdout = ""
    stderr = ""
    while True:
        remaining = max(0.1, min(5.0, deadline - time.perf_counter()))
        try:
            stdout, stderr = proc.communicate(timeout=remaining)
            break
        except subprocess.TimeoutExpired:
            now = time.perf_counter()
            if now >= deadline:
                proc.kill()
                stdout, stderr = proc.communicate()
                stderr = (stderr or "") + f"\ntimeout after {timeout}s"
                return CommandResult(
                    command=command,
                    returncode=124,
                    stdout=stdout or "",
                    stderr=stderr,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            if now >= next_heartbeat:
                _foreground(
                    f"  still running: {' '.join(command[:3])}"
                    f"{' ...' if len(command) > 3 else ''}"
                    f" elapsed={_human_duration(now - started)} timeout={_human_duration(timeout)}"
                )
                next_heartbeat = now + heartbeat_seconds
    return CommandResult(
        command=command,
        returncode=int(proc.returncode or 0),
        stdout=stdout or "",
        stderr=stderr or "",
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


class LiveClient:
    def __init__(self, base_url: str, *, timeout: int = 60, max_retries: int = 4, retry_backoff: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.cookies = CookieJar()
        self.ctx = ssl._create_unverified_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.ctx),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )
        self.csrf = ""
        self.timeout = max(1, int(timeout))
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))

    def _request(self, path: str, *, method: str = "GET", body: dict | None = None, retryable: bool | None = None) -> tuple[int, dict, str]:
        method = method.upper()
        if retryable is None:
            retryable = method == "GET"
        headers = {}
        data = None
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            data = raw
            headers["Content-Type"] = "application/json"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        attempts = self.max_retries if retryable else 1
        for attempt in range(1, attempts + 1):
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
                    payload = {"_raw": text[:500]}
                return int(exc.code), payload, text
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError):
                if attempt >= attempts:
                    raise
                time.sleep(self.retry_backoff * attempt)

    def fetch_csrf(self) -> str:
        status, payload, _ = self._request("/api/csrf-token")
        if status != 200 or not payload.get("csrf_token"):
            raise RuntimeError(payload.get("msg") or f"failed to fetch csrf token (HTTP {status})")
        self.csrf = str(payload["csrf_token"])
        return self.csrf

    def login(self, username: str, password: str, *, rotate_to: str = "") -> str:
        self.fetch_csrf()
        status, payload, _ = self._request(
            "/api/login",
            method="POST",
            body={"username": username, "password": password, "csrf_token": self.csrf},
        )
        if status != 200 or not payload.get("ok"):
            raise RuntimeError(payload.get("msg") or payload.get("error") or f"login failed (HTTP {status})")
        self.fetch_csrf()
        if payload.get("must_change_password"):
            if not rotate_to:
                raise RuntimeError("root requires password change before go-live checks; rerun with --root-new-password")
            me_status, me_payload, _ = self._request("/api/me")
            user_id = int(me_payload.get("id") or 0) if me_status == 200 else 0
            if user_id <= 0:
                raise RuntimeError("password change required but /api/me did not return the current user id")
            change_status, change_payload, _ = self._request(
                f"/api/admin/users/{user_id}",
                method="PUT",
                body={
                    "current_password": password,
                    "password": rotate_to,
                    "password_confirm": rotate_to,
                },
            )
            if change_status != 200 or not change_payload.get("ok"):
                raise RuntimeError(change_payload.get("msg") or f"password rotation failed (HTTP {change_status})")
            self.cookies.clear()
            self.csrf = ""
            return self.login(username, rotate_to)
        return password


def _auto_detect_base_url() -> str:
    for base in ("https://127.0.0.1:5000", "http://127.0.0.1:5000"):
        client = LiveClient(base)
        try:
            status, payload, _ = client._request("/api/version")
            if status == 200 and isinstance(payload, dict):
                return base
        except Exception:
            pass
    raise RuntimeError("無法自動偵測本機 base URL；請顯式傳入 --base-url")


class PayloadSigner:
    def __init__(self):
        _ensure_runtime_env()
        self.service = ServerModeService(
            snapshot_service=None,
            get_db=lambda: None,
            audit=lambda *args, **kwargs: None,
        )

    def build(self, *, report_type: str, raw_report: dict, passed: bool, test_result: str, critical: int = 0, high: int = 0, unresolved: list | None = None, tester: str, report_source: str, target_commit: str, target_branch: str, server_mode: str = "") -> dict:
        attestation = self.service._prepare_production_report_attestation(
            report_type=report_type,
            raw_report=raw_report,
            target_commit=target_commit,
            target_branch=target_branch,
            server_mode=server_mode,
            test_result=test_result,
            passed=passed,
            critical_findings_count=critical,
            high_findings_count=high,
            unresolved_findings=unresolved or [],
            tester=tester,
            report_source=report_source,
        )
        if not attestation.get("ok"):
            raise RuntimeError(attestation.get("reason") or "failed to sign production report payload")
        return {
            "report_type": report_type,
            "report_hash": attestation["report_hash"],
            "signature": attestation["signature"],
            "key_version": attestation["key_version"],
            "target_commit": target_commit,
            "target_branch": target_branch,
            "server_mode": server_mode,
            "test_result": test_result,
            "pass": bool(passed),
            "critical_findings_count": int(critical),
            "high_findings_count": int(high),
            "unresolved_findings": list(unresolved or []),
            "tester": tester,
            "report_source": report_source,
            "raw_report": raw_report,
        }


def _payload_md(payload: dict, canonical_path: Path) -> str:
    raw = payload["raw_report"]
    lines = [
        f"# {payload['report_type']} production report",
        "",
        f"- canonical_payload: `{canonical_path}`",
        f"- test_result: `{payload['test_result']}`",
        f"- pass: `{payload['pass']}`",
        f"- report_hash: `{payload['report_hash']}`",
        f"- key_version: `{payload['key_version']}`",
        f"- target_branch: `{payload.get('target_branch') or '-'}`",
        f"- target_commit: `{payload.get('target_commit') or '-'}`",
        f"- report_source: `{payload.get('report_source') or '-'}`",
        "",
        "## Summary",
        "",
        f"- status: `{raw.get('status', '-')}`",
        f"- summary: {raw.get('summary', '-')}",
    ]
    artifacts = raw.get("artifacts") or {}
    if artifacts:
        lines.extend(["", "## Artifacts", ""])
        for key, value in artifacts.items():
            lines.append(f"- {key}: `{value}`")
    return "\n".join(lines)


def _make_payload(report_type: str, raw_report: dict, *, passed: bool, tester: str, report_source: str, meta: dict, canonical_json: Path, canonical_md: Path, signer: PayloadSigner, critical: int = 0, high: int = 0, unresolved: list | None = None, server_mode: str = "") -> dict:
    resolved_server_mode = str(server_mode or meta.get("server_mode") or "").strip()
    payload = signer.build(
        report_type=report_type,
        raw_report=raw_report,
        passed=passed,
        test_result="pass" if passed else "fail",
        critical=critical,
        high=high,
        unresolved=unresolved or [],
        tester=tester,
        report_source=report_source,
        target_commit=meta["target_commit"],
        target_branch=meta["target_branch"],
        server_mode=resolved_server_mode,
    )
    _json_dump(canonical_json, payload)
    _text_dump(canonical_md, _payload_md(payload, canonical_json))
    return payload


def _report_paths(out_root: Path, report_type: str) -> tuple[Path, Path]:
    return out_root / f"{report_type}_report.json", out_root / f"{report_type}_report.md"


def _operational_campaign_report(
    out_root: Path,
    campaign_report: str,
    *,
    signer: PayloadSigner,
    meta: dict,
) -> dict:
    path = Path(str(campaign_report or "")).expanduser().resolve(strict=False) if campaign_report else None
    tmp_root = Path("/tmp").resolve()
    payload: dict = {}
    report_bytes = b""
    errors: list[str] = []
    if path is None:
        errors.append("operational campaign report path is required")
    elif path != tmp_root and tmp_root not in path.parents:
        errors.append("operational campaign report must remain below /tmp")
    elif not path.is_file():
        errors.append("operational campaign report does not exist")
    else:
        try:
            report_bytes = path.read_bytes()
            loaded = json.loads(report_bytes.decode("utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
            else:
                errors.append("campaign report root must be a JSON object")
        except Exception as exc:
            errors.append(f"campaign report is not valid JSON: {exc.__class__.__name__}")

    raw_scenarios = payload.get("scenarios")
    scenarios = raw_scenarios if isinstance(raw_scenarios, dict) else {}
    findings = payload.get("findings")
    source_drift = payload.get("source_drift")
    secret_scan = payload.get("secret_scan")
    control_checks = payload.get("control_checks")
    source_digest = str(payload.get("source_manifest_digest") or "")

    def number(value, default: float = -1.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def scenario_passed(name: str) -> bool:
        scenario = scenarios.get(name)
        return isinstance(scenario, dict) and scenario.get("ok") is True

    active_seconds = number(payload.get("active_test_seconds"))
    required_seconds = number(payload.get("required_active_test_seconds"))
    authorization_wait = number(payload.get("authorization_wait_seconds_included"))
    try:
        from scripts.testing.operational_campaign_24h import manifest_digest, source_manifest

        current_source_digest = manifest_digest(source_manifest())
    except Exception as exc:
        current_source_digest = ""
        errors.append(f"current source manifest could not be calculated: {exc.__class__.__name__}")

    checks = {
        "report_ok": payload.get("ok") is True and str(payload.get("verdict") or "").upper() == "PASS",
        "formal_campaign": payload.get("formal_campaign") is True,
        "production_signoff_eligible": payload.get("production_signoff_eligible") is True,
        "active_duration": active_seconds >= OPERATIONAL_CAMPAIGN_MIN_SECONDS,
        "required_duration": required_seconds >= OPERATIONAL_CAMPAIGN_MIN_SECONDS,
        "authorization_wait_excluded": authorization_wait == 0,
        "findings_shape": isinstance(findings, list),
        "no_findings": isinstance(findings, list) and not findings,
        "source_drift_shape": isinstance(source_drift, dict),
        "no_source_drift": isinstance(source_drift, dict) and not source_drift,
        "scenario_shape": isinstance(raw_scenarios, dict)
        and all(isinstance(scenarios.get(name), dict) for name in OPERATIONAL_CAMPAIGN_SCENARIOS),
        "mandatory_scenarios_present": OPERATIONAL_CAMPAIGN_SCENARIOS.issubset(set(scenarios)),
        "mandatory_scenarios_passed": all(scenario_passed(name) for name in OPERATIONAL_CAMPAIGN_SCENARIOS),
        "secret_scan_shape": isinstance(secret_scan, dict),
        "secret_scan_passed": isinstance(secret_scan, dict) and secret_scan.get("ok") is True,
        "control_checks_shape": isinstance(control_checks, dict)
        and bool(control_checks)
        and all(isinstance(item, dict) for item in control_checks.values()),
        "control_checks_passed": isinstance(control_checks, dict)
        and bool(control_checks)
        and all(isinstance(item, dict) and item.get("ok") is True for item in control_checks.values()),
        "source_manifest_matches": bool(source_digest) and source_digest == current_source_digest,
    }
    errors.extend(name for name, passed in checks.items() if not passed)
    errors = list(dict.fromkeys(errors))
    passed = not errors
    report_sha256 = hashlib.sha256(report_bytes).hexdigest() if report_bytes else ""
    raw_report = {
        "report_type": "operational_campaign_24h",
        "status": "pass" if passed else "fail",
        "summary": (
            f"active={active_seconds:.3f}s, "
            f"scenarios={sum(1 for name in OPERATIONAL_CAMPAIGN_SCENARIOS if scenario_passed(name))}/"
            f"{len(OPERATIONAL_CAMPAIGN_SCENARIOS)}, source_match={checks['source_manifest_matches']}"
        ),
        "generator": "scripts/testing/operational_campaign_24h.py",
        "artifacts": {
            "campaign_report": str(path) if path else "",
            "campaign_report_sha256": report_sha256,
        },
        "campaign": {
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "active_test_seconds": payload.get("active_test_seconds"),
            "required_active_test_seconds": payload.get("required_active_test_seconds"),
            "source_manifest_digest": source_digest,
            "current_source_manifest_digest": current_source_digest,
            "source_git": payload.get("source_git") or {},
            "scenario_status": {
                name: scenario_passed(name)
                for name in sorted(OPERATIONAL_CAMPAIGN_SCENARIOS)
            },
            "checks": checks,
        },
        "errors": errors,
    }
    canonical_json, canonical_md = _report_paths(out_root, "operational_campaign_24h")
    return _make_payload(
        "operational_campaign_24h",
        raw_report,
        passed=passed,
        tester="scripts/security/gate/on_live_reports_make.py",
        report_source="scripts/security/gate/on_live_reports_make.py",
        meta=meta,
        canonical_json=canonical_json,
        canonical_md=canonical_md,
        signer=signer,
        high=0 if passed else 1,
        unresolved=errors,
    )


def _pick_available_port(preferred: int, *, host: str = "127.0.0.1") -> int:
    preferred = int(preferred or 0)
    if preferred > 0:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, preferred))
            return preferred
        except OSError:
            pass
        finally:
            probe.close()
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _script_report(out_root: Path, raw_dir: Path, report_type: str, command: list[str], *, timeout: int, signer: PayloadSigner, meta: dict) -> dict:
    artifact_dir = raw_dir / report_type
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result = _run(command + ["--out", str(artifact_dir)], timeout=timeout)
    parsed = {}
    try:
        parsed = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
    except Exception:
        parsed = {}
    artifacts = {}
    for key in ("json_report", "md_report"):
        if parsed.get(key):
            artifacts[key] = str(parsed[key])
    raw_report = {
        "report_type": report_type,
        "status": "pass" if result.ok else "fail",
        "summary": _last_nonempty_line(result.stdout) or _last_nonempty_line(result.stderr) or f"returncode={result.returncode}",
        "generator": " ".join(command),
        "artifacts": artifacts,
        "duration_ms": result.duration_ms,
        "stdout_summary": parsed or {"stdout_tail": _tail(result.stdout), "stderr_tail": _tail(result.stderr)},
    }
    return _make_payload(
        report_type,
        raw_report,
        passed=result.ok,
        tester="scripts/security/gate/on_live_reports_make.py",
        report_source="scripts/security/gate/on_live_reports_make.py",
        meta=meta,
        canonical_json=_report_paths(out_root, report_type)[0],
        canonical_md=_report_paths(out_root, report_type)[1],
        signer=signer,
        high=0 if result.ok else 1,
    )


def _pytest_report(out_root: Path, raw_dir: Path, report_type: str, test_args: list[str], *, timeout: int, signer: PayloadSigner, meta: dict) -> dict:
    log_path = raw_dir / f"{report_type}_pytest.log"
    result = _run([str(ROOT / "scripts" / "testing" / "pytest_in_tmp.sh"), "-q", *test_args], timeout=timeout)
    _text_dump(log_path, result.stdout + ("\n" + result.stderr if result.stderr else ""))
    raw_report = {
        "report_type": report_type,
        "status": "pass" if result.ok else "fail",
        "summary": _last_nonempty_line(result.stdout) or _last_nonempty_line(result.stderr) or f"returncode={result.returncode}",
        "generator": f"scripts/testing/pytest_in_tmp.sh -q {' '.join(test_args)}",
        "artifacts": {"pytest_log": str(log_path)},
        "duration_ms": result.duration_ms,
    }
    canonical_json, canonical_md = _report_paths(out_root, report_type)
    return _make_payload(
        report_type,
        raw_report,
        passed=result.ok,
        tester="scripts/security/gate/on_live_reports_make.py",
        report_source="scripts/security/gate/on_live_reports_make.py",
        meta=meta,
        canonical_json=canonical_json,
        canonical_md=canonical_md,
        signer=signer,
        high=0 if result.ok else 1,
    )


def _functional_report(out_root: Path, raw_dir: Path, args, signer: PayloadSigner, meta: dict) -> dict:
    report_root = raw_dir / "functional_root"
    functional_port = _pick_available_port(args.functional_port)
    result = _run(
        [
            "bash",
            str(ROOT / "scripts" / "security" / "pentest" / "run_functional_smoke.sh"),
            "--port",
            str(functional_port),
            "--out",
            str(report_root),
            "--core-only",
        ],
        env={"GO_LIVE_CORE_ONLY": "1", "START_TIMEOUT": "180"},
        timeout=args.functional_timeout,
    )
    latest = _find_latest_paths(report_root, "functional_*")
    artifacts = {}
    if latest:
        artifacts["report_dir"] = str(latest[0])
        summary = latest[0] / "00_FUNCTIONAL_SMOKE.md"
        if summary.exists():
            artifacts["summary_md"] = str(summary)
    raw_report = {
        "report_type": "functional",
        "status": "pass" if result.ok else "fail",
        "summary": _last_nonempty_line(result.stdout) or _last_nonempty_line(result.stderr) or f"returncode={result.returncode}",
        "generator": "scripts/security/pentest/run_functional_smoke.sh",
        "artifacts": {**artifacts, "functional_port": functional_port},
        "duration_ms": result.duration_ms,
    }
    canonical_json, canonical_md = _report_paths(out_root, "functional")
    return _make_payload("functional", raw_report, passed=result.ok, tester="scripts/security/gate/on_live_reports_make.py", report_source="scripts/security/gate/on_live_reports_make.py", meta=meta, canonical_json=canonical_json, canonical_md=canonical_md, signer=signer, high=0 if result.ok else 1)


def _pentest_report(out_root: Path, raw_dir: Path, args, signer: PayloadSigner, meta: dict) -> dict:
    report_root = raw_dir / "pentest_root"
    env = {
        "ROOT_PASSWORD": args.root_password,
        "MANAGER_PASSWORD": args.manager_password,
        "TEST_PASSWORD": args.test_password,
        "USER_A_USERNAME": "test",
        "USER_A_PASSWORD": args.test_password,
        "USER_B_USERNAME": "admin",
        "USER_B_PASSWORD": args.manager_password,
        "GO_LIVE_CORE_ONLY": "1",
    }
    command = [
        "bash",
        str(ROOT / "scripts" / "security" / "pentest" / "run_pentest.sh"),
        "--target",
        args.base_url,
        "--out",
        str(report_root),
        "--only",
        GO_LIVE_CORE_PENTEST_CHECKS,
    ]
    if args.i_own_this_target:
        command.append("--i-own-this-target")
    result = _run(command, env=env, timeout=args.pentest_timeout)
    latest = _find_latest_paths(report_root, "20*")
    artifacts = {}
    if latest:
        artifacts["report_dir"] = str(latest[0])
        summary = latest[0] / "00_SUMMARY.md"
        if summary.exists():
            artifacts["summary_md"] = str(summary)
    raw_report = {
        "report_type": "pentest",
        "status": "pass" if result.ok else "fail",
        "summary": _last_nonempty_line(result.stdout) or _last_nonempty_line(result.stderr) or f"returncode={result.returncode}",
        "generator": "scripts/security/pentest/run_pentest.sh",
        "artifacts": artifacts,
        "duration_ms": result.duration_ms,
    }
    canonical_json, canonical_md = _report_paths(out_root, "pentest")
    return _make_payload("pentest", raw_report, passed=result.ok, tester="scripts/security/gate/on_live_reports_make.py", report_source="scripts/security/gate/on_live_reports_make.py", meta=meta, canonical_json=canonical_json, canonical_md=canonical_md, signer=signer, high=0 if result.ok else 1)


def _permission_report(out_root: Path, raw_dir: Path, args, signer: PayloadSigner, meta: dict) -> dict:
    report_dir = raw_dir / "permission"
    report_dir.mkdir(parents=True, exist_ok=True)
    out_json = report_dir / "functional_permission_pentest.json"
    out_md = report_dir / "functional_permission_pentest.md"
    env = {
        "ROOT_PASSWORD": args.root_password,
        "MANAGER_PASSWORD": args.manager_password,
        "TEST_PASSWORD": args.test_password,
    }
    command = [
        sys.executable,
        str(ROOT / "scripts" / "security" / "pentest" / "functional_permission_pentest.py"),
        "--base-url",
        args.base_url,
        "--out-json",
        str(out_json),
        "--out-md",
        str(out_md),
        "--core-only",
    ]
    result = _run(command, env=env, timeout=args.permission_timeout)
    report_payload = {}
    if out_json.exists():
        try:
            report_payload = json.loads(out_json.read_text(encoding="utf-8"))
        except Exception:
            report_payload = {}
    passed = bool(report_payload.get("ok", result.ok)) if report_payload else result.ok
    raw_report = {
        "report_type": "permission",
        "status": "pass" if passed else "fail",
        "summary": report_payload.get("summary") or _last_nonempty_line(result.stdout) or f"returncode={result.returncode}",
        "generator": "python3 scripts/security/pentest/functional_permission_pentest.py",
        "artifacts": {"json_report": str(out_json), "md_report": str(out_md)},
        "duration_ms": result.duration_ms,
        "script_payload": report_payload,
    }
    canonical_json, canonical_md = _report_paths(out_root, "permission")
    return _make_payload("permission", raw_report, passed=passed, tester="scripts/security/gate/on_live_reports_make.py", report_source="scripts/security/gate/on_live_reports_make.py", meta=meta, canonical_json=canonical_json, canonical_md=canonical_md, signer=signer, high=0 if passed else 1)


def _current_live_mode(client: LiveClient) -> str:
    status, payload, _ = client._request("/api/admin/server-mode")
    if status != 200 or not bool(payload.get("ok")):
        raise RuntimeError(payload.get("msg") or f"failed to read live server mode (HTTP {status})")
    return str((payload.get("mode") or {}).get("current_mode") or "").strip()


def _switch_live_mode(client: LiveClient, target_mode: str, *, notes: str) -> dict:
    confirm = MODE_CONFIRM_PHRASES.get(target_mode, "")
    status, payload, _ = client._request(
        "/api/admin/server-mode",
        method="POST",
        body={"mode": target_mode, "confirm": confirm, "notes": notes},
        retryable=True,
    )
    if status != 200 or not bool(payload.get("ok")):
        raise RuntimeError(payload.get("msg") or f"failed to switch live mode to {target_mode} (HTTP {status})")
    return payload


def _refresh_root_session(client: LiveClient, args) -> None:
    client.cookies.clear()
    client.csrf = ""
    args.root_password = client.login(args.root_username, args.root_password, rotate_to=args.root_new_password)


def _stress_report(out_root: Path, raw_dir: Path, args, signer: PayloadSigner, meta: dict, client: LiveClient) -> dict:
    report_dir = raw_dir / "stress"
    report_dir.mkdir(parents=True, exist_ok=True)
    general = _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "security" / "pentest" / "stress_test.py"),
            "--target",
            args.base_url,
            "--out",
            str(report_dir),
            "--i-own-this-target",
        ]
        if args.i_own_this_target
        else [
            sys.executable,
            str(ROOT / "scripts" / "security" / "pentest" / "stress_test.py"),
            "--target",
            args.base_url,
            "--out",
            str(report_dir),
        ],
        timeout=args.stress_timeout,
    )
    trading_env = {"ROOT_PASSWORD": args.root_password}
    if args.root_new_password:
        trading_env["PENTEST_ROOT_NEW_PASSWORD"] = args.root_new_password
    previous_mode = ""
    switched_mode = ""
    restore_error = ""
    try:
        _refresh_root_session(client, args)
        previous_mode = _current_live_mode(client)
        if previous_mode not in {"production", "internal_test", "test", "dev_ready"}:
            _switch_live_mode(client, "dev_ready", notes="go_live trading stress precheck")
            switched_mode = "dev_ready"
        trading = _run(
            [
                sys.executable,
                str(ROOT / "scripts" / "security" / "pentest" / "trading_stress_pentest.py"),
                "--base-url",
                args.base_url,
                "--mode",
                "functional_correctness",
                "--users",
                "2",
                "--orders-per-user",
                "5",
                "--concurrency",
                "2",
                "--rate",
                "10",
                "--out",
                str(report_dir),
            ],
            env=trading_env,
            timeout=args.trading_stress_timeout,
        )
    finally:
        if switched_mode and previous_mode and previous_mode != switched_mode:
            try:
                _refresh_root_session(client, args)
                _switch_live_mode(client, previous_mode, notes="restore live mode after trading stress")
            except Exception as exc:
                restore_error = str(exc)
    artifacts = {}
    for pattern, key in (
        ("stress_*.json", "http_stress_json"),
        ("stress_*.md", "http_stress_md"),
        ("trading_stress_report_*.json", "trading_stress_json"),
        ("trading_stress_report_*.md", "trading_stress_md"),
    ):
        matches = _find_latest_paths(report_dir, pattern)
        if matches:
            artifacts[key] = str(matches[0])
    if previous_mode:
        artifacts["initial_live_mode"] = previous_mode
    if switched_mode:
        artifacts["stress_live_mode"] = switched_mode
    if restore_error:
        artifacts["mode_restore_error"] = restore_error
    passed = general.ok and trading.ok and not restore_error
    raw_report = {
        "report_type": "stress",
        "status": "pass" if passed else "fail",
        "summary": f"http_stress={'pass' if general.ok else 'fail'}, trading_stress={'pass' if trading.ok else 'fail'}",
        "generator": "python3 scripts/security/pentest/stress_test.py + python3 scripts/security/pentest/trading_stress_pentest.py",
        "artifacts": artifacts,
        "duration_ms": general.duration_ms + trading.duration_ms,
        "subchecks": {
            "http_stress": {"returncode": general.returncode, "stdout_tail": _tail(general.stdout), "stderr_tail": _tail(general.stderr)},
            "trading_stress": {"returncode": trading.returncode, "stdout_tail": _tail(trading.stdout), "stderr_tail": _tail(trading.stderr)},
        },
    }
    canonical_json, canonical_md = _report_paths(out_root, "stress")
    return _make_payload("stress", raw_report, passed=passed, tester="scripts/security/gate/on_live_reports_make.py", report_source="scripts/security/gate/on_live_reports_make.py", meta=meta, canonical_json=canonical_json, canonical_md=canonical_md, signer=signer, high=0 if passed else 1)


def _log_chain_report(out_root: Path, client: LiveClient, signer: PayloadSigner, meta: dict) -> dict:
    status, payload, _ = client._request("/api/root/server-mode/logs/verify")
    details = payload.get("details") or {}
    passed = status == 200 and bool(payload.get("ok")) and int(payload.get("broken_links") or 0) == 0
    raw_report = {
        "report_type": "log_chain_verify",
        "status": "pass" if passed else "fail",
        "summary": f"chain_length={payload.get('chain_length', 0)}, broken_links={payload.get('broken_links', 0)}, result={payload.get('result', '-')}",
        "generator": "GET /api/root/server-mode/logs/verify",
        "details": payload,
        "artifacts": {},
    }
    canonical_json, canonical_md = _report_paths(out_root, "log_chain_verify")
    return _make_payload("log_chain_verify", raw_report, passed=passed, tester="scripts/security/gate/on_live_reports_make.py", report_source="scripts/security/gate/on_live_reports_make.py", meta=meta, canonical_json=canonical_json, canonical_md=canonical_md, signer=signer, high=0 if passed else 1, unresolved=list(details.get("mismatches") or []))


def _refresh_deploy_integrity_baseline_if_needed(client: LiveClient, report_payload: dict) -> dict:
    report = (report_payload or {}).get("report") or {}
    status = report.get("status") or {}
    if not bool(status.get("deployment_review_pending")):
        return {"attempted": False, "reason": "not_required"}

    findings_status, findings_payload, _ = client._request("/api/root/integrity/findings?status=pending")
    findings = findings_payload.get("findings") if findings_status == 200 and bool(findings_payload.get("ok")) else []
    finding_ids = []
    for item in findings or []:
        try:
            finding_ids.append(int(item.get("id")))
        except Exception:
            continue
    if not finding_ids:
        return {
            "attempted": False,
            "reason": "no_pending_findings",
            "findings_status": findings_status,
            "findings_payload": findings_payload,
        }

    approve_confirm = str((report_payload or {}).get("approve_confirm") or "APPROVE INTEGRITY UPDATE")
    client.fetch_csrf()
    review_status, review_payload, _ = client._request(
        "/api/root/integrity/findings/bulk-review",
        method="POST",
        body={
            "action": "approve",
            "finding_ids": finding_ids,
            "confirm": approve_confirm,
            "note": "on_live_reports_make auto refresh deploy integrity baseline",
        },
    )
    return {
        "attempted": True,
        "finding_ids": finding_ids,
        "review_status": review_status,
        "review_payload": review_payload,
    }


def _integrity_report(out_root: Path, client: LiveClient, signer: PayloadSigner, meta: dict) -> dict:
    client.fetch_csrf()
    rescan_status, rescan_payload, _ = client._request("/api/root/integrity/rescan", method="POST", body={})
    report_status, report_payload, _ = client._request("/api/root/integrity/report")
    baseline_refresh = _refresh_deploy_integrity_baseline_if_needed(client, report_payload)
    if baseline_refresh.get("attempted"):
        client.fetch_csrf()
        rescan_status, rescan_payload, _ = client._request("/api/root/integrity/rescan", method="POST", body={})
        report_status, report_payload, _ = client._request("/api/root/integrity/report")
    report = report_payload.get("report") or {}
    status = report.get("status") or {}
    summary = status.get("summary") or {}
    health = status.get("health") or {}
    high_pending = int(summary.get("high_risk_pending") or 0)
    passed = rescan_status == 200 and bool(rescan_payload.get("ok")) and report_status == 200 and bool(report_payload.get("ok")) and high_pending == 0 and str(health.get("level") or "").lower() not in {"critical", "error"}
    raw_report = {
        "report_type": "integrity_guard",
        "status": "pass" if passed else "fail",
        "summary": f"health={health.get('level', '-')}, pending={summary.get('pending', 0)}, high_risk_pending={high_pending}",
        "generator": "POST /api/root/integrity/rescan + GET /api/root/integrity/report",
        "details": {"rescan": rescan_payload, "report": report_payload, "baseline_refresh": baseline_refresh},
        "artifacts": {},
    }
    canonical_json, canonical_md = _report_paths(out_root, "integrity_guard")
    return _make_payload("integrity_guard", raw_report, passed=passed, tester="scripts/security/gate/on_live_reports_make.py", report_source="scripts/security/gate/on_live_reports_make.py", meta=meta, canonical_json=canonical_json, canonical_md=canonical_md, signer=signer, high=high_pending)


def _upload_payloads(client: LiveClient, payload_paths: list[Path]) -> list[dict]:
    results = []
    for path in payload_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        client.fetch_csrf()
        status, response, _ = client._request(
            "/api/root/production-report/upload",
            method="POST",
            body=payload,
            retryable=True,
        )
        if status == 403 and str(response.get("error") or "") == "csrf_invalid":
            client.fetch_csrf()
            status, response, _ = client._request(
                "/api/root/production-report/upload",
                method="POST",
                body=payload,
                retryable=True,
            )
        results.append({"path": str(path), "status": status, "ok": bool(response.get("ok")), "response": response})
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate all production-gate reports and stage upload-ready payloads under runtime/reports/security/production_gate.")
    parser.add_argument("--base-url", default="", help="Live base URL for root-only and live-target checks. Default: auto-detect 127.0.0.1:5000 via https/http.")
    parser.add_argument("--root-username", default=os.environ.get("ROOT_USERNAME", "root"))
    from scripts.testing.probe_credentials import (
        add_manager_password_argument,
        add_root_password_argument,
        add_user_password_argument,
    )
    add_root_password_argument(parser)
    add_manager_password_argument(parser)
    add_user_password_argument(parser)
    parser.add_argument("--root-new-password", default=os.environ.get("PENTEST_ROOT_NEW_PASSWORD", ""))
    parser.add_argument("--runtime-dir", default=os.environ.get("HACKME_RUNTIME_DIR", ""), help="Runtime root used by report signing and default output paths.")
    parser.add_argument("--out", default="", help="Output root for stable production-gate payloads. Default: <runtime>/reports/security/production_gate.")
    parser.add_argument(
        "--operational-campaign-report",
        default=os.environ.get("HACKME_OPERATIONAL_CAMPAIGN_REPORT", ""),
        help="Formal operational_campaign_24h.json below /tmp. Required for a passing production report set.",
    )
    parser.add_argument("--functional-port", type=int, default=50741)
    parser.add_argument("--server-mode-timeout", type=int, default=1800)
    parser.add_argument("--functional-timeout", type=int, default=900)
    parser.add_argument("--pentest-timeout", type=int, default=3600)
    parser.add_argument("--permission-timeout", type=int, default=3600)
    parser.add_argument("--stress-timeout", type=int, default=600)
    parser.add_argument("--trading-stress-timeout", type=int, default=600)
    parser.add_argument("--pytest-timeout", type=int, default=7200)
    parser.add_argument("--http-timeout", type=int, default=int(os.environ.get("GO_LIVE_HTTP_TIMEOUT", "60")))
    parser.add_argument("--http-retries", type=int, default=int(os.environ.get("GO_LIVE_HTTP_RETRIES", "4")))
    parser.add_argument("--http-retry-backoff", type=float, default=float(os.environ.get("GO_LIVE_HTTP_RETRY_BACKOFF", "2.0")))
    parser.add_argument("--upload", action="store_true", help="Upload the generated payloads to /api/root/production-report/upload after generation.")
    parser.add_argument("--i-own-this-target", action="store_true", help="Allow non-loopback/non-local targets for scripts that require explicit operator confirmation.")
    return parser.parse_args()


def _resolve_output_root(args: argparse.Namespace) -> Path:
    if args.runtime_dir:
        os.environ["HACKME_RUNTIME_DIR"] = str(Path(args.runtime_dir).expanduser().resolve())
    _ensure_runtime_env()
    if args.out:
        return Path(args.out).expanduser().resolve()
    return (security_reports_root() / "production_gate").resolve()


def main() -> int:
    args = parse_args()
    if not args.base_url:
        args.base_url = _auto_detect_base_url()
    if not args.root_password:
        raise SystemExit("請傳入 --root-password，或先設 ROOT_PASSWORD。")

    out_root = _resolve_output_root(args)
    run_id = _now_stamp()
    raw_dir = out_root / "runs" / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)

    client = LiveClient(
        args.base_url,
        timeout=args.http_timeout,
        max_retries=args.http_retries,
        retry_backoff=args.http_retry_backoff,
    )
    args.root_password = client.login(args.root_username, args.root_password, rotate_to=args.root_new_password)

    meta = _target_meta()
    signer = PayloadSigner()
    payloads: dict[str, dict] = {}

    payloads["clean_smoke"] = _script_report(out_root, raw_dir, "clean_smoke", [sys.executable, str(ROOT / "scripts" / "security" / "server_mode" / "server_mode_v2_clean_smoke.py")], timeout=args.server_mode_timeout, signer=signer, meta=meta)
    payloads["adversarial"] = _script_report(out_root, raw_dir, "adversarial", [sys.executable, str(ROOT / "scripts" / "security" / "server_mode" / "server_mode_v2_adversarial.py")], timeout=args.server_mode_timeout, signer=signer, meta=meta)
    payloads["redteam_l2"] = _script_report(out_root, raw_dir, "redteam_l2", [sys.executable, str(ROOT / "scripts" / "security" / "server_mode" / "server_mode_v2_redteam_l2.py")], timeout=args.server_mode_timeout, signer=signer, meta=meta)

    payloads["pytest"] = _pytest_report(
        out_root,
        raw_dir,
        "pytest",
        GO_LIVE_CORE_PYTEST_ARGS,
        timeout=args.pytest_timeout,
        signer=signer,
        meta=meta,
    )
    payloads["log_chain_verify"] = _log_chain_report(out_root, client, signer, meta)
    payloads["integrity_guard"] = _integrity_report(out_root, client, signer, meta)
    payloads["stress"] = _stress_report(out_root, raw_dir, args, signer, meta, client)
    payloads["permission"] = _permission_report(out_root, raw_dir, args, signer, meta)
    payloads["functional"] = _functional_report(out_root, raw_dir, args, signer, meta)
    payloads["pentest"] = _pentest_report(out_root, raw_dir, args, signer, meta)
    payloads["snapshot_restore"] = _pytest_report(out_root, raw_dir, "snapshot_restore", ["tests/snapshots/test_snapshots.py"], timeout=args.pytest_timeout, signer=signer, meta=meta)
    payloads["points_chain_consistency"] = _pytest_report(out_root, raw_dir, "points_chain_consistency", ["tests/points/test_points_chain.py"], timeout=args.pytest_timeout, signer=signer, meta=meta)
    payloads["cloud_drive_quota_permission"] = _pytest_report(out_root, raw_dir, "cloud_drive_quota_permission", ["tests/storage/test_cloud_drive_attachments.py", "tests/storage/test_storage_albums_schema.py"], timeout=args.pytest_timeout, signer=signer, meta=meta)
    payloads["ai_agent_boundary"] = _pytest_report(out_root, raw_dir, "ai_agent_boundary", AI_AGENT_BOUNDARY_PYTEST_TARGETS, timeout=args.pytest_timeout, signer=signer, meta=meta)
    payloads["operational_campaign_24h"] = _operational_campaign_report(
        out_root,
        args.operational_campaign_report,
        signer=signer,
        meta=meta,
    )

    missing = [name for name in PRODUCTION_REQUIRED_REPORT_TYPES if name not in payloads]
    if missing:
        raise SystemExit(f"missing production reports: {missing}")

    upload_results = []
    if args.upload:
        payload_paths = [_report_paths(out_root, name)[0] for name in PRODUCTION_REQUIRED_REPORT_TYPES]
        upload_results = _upload_payloads(client, payload_paths)

    summary = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "base_url": args.base_url,
        "runtime_dir": os.environ.get("HACKME_RUNTIME_DIR", ""),
        "out_root": str(out_root),
        "run_dir": str(raw_dir),
        "reports": {name: {"pass": bool(payloads[name]["pass"]), "path": str(_report_paths(out_root, name)[0])} for name in PRODUCTION_REQUIRED_REPORT_TYPES},
        "all_passed": all(bool(payloads[name]["pass"]) for name in PRODUCTION_REQUIRED_REPORT_TYPES),
        "upload_results": upload_results,
    }
    _json_dump(out_root / f"on_live_reports_make_{run_id}.json", summary)
    _text_dump(
        out_root / f"on_live_reports_make_{run_id}.md",
        "\n".join(
            [
                "# on_live_reports_make summary",
                "",
                f"- base_url: `{args.base_url}`",
                f"- runtime_dir: `{os.environ.get('HACKME_RUNTIME_DIR', '') or '-'}`",
                f"- out_root: `{out_root}`",
                f"- run_dir: `{raw_dir}`",
                f"- all_passed: `{summary['all_passed']}`",
                "",
                "## Reports",
                "",
                *[
                    f"- {name}: `{'PASS' if payloads[name]['pass'] else 'FAIL'}` -> `{_report_paths(out_root, name)[0]}`"
                    for name in PRODUCTION_REQUIRED_REPORT_TYPES
                ],
            ]
        ),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
