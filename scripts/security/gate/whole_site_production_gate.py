#!/usr/bin/env python3
"""Whole-site production gate for hackme_web.

Server Mode v2 has its own production readiness. This gate is stricter: it
collects the major module checks needed before the whole site may be marked
production-ready.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.security.common_paths import REPO_ROOT as ROOT, security_reports_root
DEFAULT_TIMEOUT = 240
DEFAULT_FULL_PYTEST_TIMEOUT = 7200
HARD_FAIL_SEVERITIES = {"CRITICAL", "HIGH"}


@dataclass
class Subcheck:
    name: str
    status: str
    severity: str = "LOW"
    command: list[str] | None = None
    duration_ms: int = 0
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    evidence: dict = field(default_factory=dict)
    required_followup: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "PASS"


@dataclass
class ModuleResult:
    name: str
    status: str = "PASS"
    subchecks: list[Subcheck] = field(default_factory=list)
    unresolved_risks: list[str] = field(default_factory=list)

    def add(self, subcheck: Subcheck) -> None:
        self.subchecks.append(subcheck)
        if not subcheck.ok:
            self.status = "FAIL"


def tail(text: str, limit: int = 3000) -> str:
    return (text or "")[-limit:]


def redacted_command(command: list[str]) -> list[str]:
    redacted = list(command)
    for index, value in enumerate(redacted[:-1]):
        if value in {"--password", "--root-password", "--manager-password"}:
            redacted[index + 1] = "<REDACTED>"
    return redacted


def run_command(command: list[str], *, timeout: int, env: dict | None = None) -> Subcheck:
    started = time.perf_counter()
    merged_env = os.environ.copy()
    existing_pythonpath = merged_env.get("PYTHONPATH", "")
    merged_env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    if env:
        merged_env.update(env)
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            env=merged_env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        safe_command = redacted_command(command)
        return Subcheck(
            name=" ".join(safe_command),
            status="PASS" if proc.returncode == 0 else "FAIL",
            severity="CRITICAL" if proc.returncode != 0 else "LOW",
            command=safe_command,
            duration_ms=duration_ms,
            exit_code=proc.returncode,
            stdout_tail=tail(proc.stdout),
            stderr_tail=tail(proc.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        safe_command = redacted_command(command)
        return Subcheck(
            name=" ".join(safe_command),
            status="FAIL",
            severity="HIGH",
            command=safe_command,
            duration_ms=duration_ms,
            exit_code=None,
            stdout_tail=tail(exc.stdout if isinstance(exc.stdout, str) else ""),
            stderr_tail=tail(exc.stderr if isinstance(exc.stderr, str) else f"timeout after {timeout}s"),
            required_followup="Increase timeout only after confirming the check is not hung.",
        )


def static_check(name: str, passed: bool, *, severity: str = "HIGH", evidence=None, followup="") -> Subcheck:
    return Subcheck(
        name=name,
        status="PASS" if passed else "FAIL",
        severity="LOW" if passed else severity,
        evidence=evidence or {},
        required_followup="" if passed else followup,
    )


def pytest_check(files: list[str], *, timeout: int) -> Subcheck:
    paths = [str(Path("tests") / file) for file in files]
    result = run_command([str(ROOT / "scripts" / "testing" / "pytest_in_tmp.sh"), "-q", *paths], timeout=timeout)
    result.name = "pytest " + ", ".join(files)
    return result


def script_check(script: str, args: list[str], *, timeout: int) -> Subcheck:
    result = run_command([sys.executable, str(ROOT / script), *args], timeout=timeout)
    result.name = f"{script} {' '.join(args)}".strip()
    return result


def shell_script_check(script: str, args: list[str], *, timeout: int) -> Subcheck:
    result = run_command(["bash", str(ROOT / script), *args], timeout=timeout)
    result.name = f"{script} {' '.join(args)}".strip()
    return result


def pick_available_port(preferred: int) -> int:
    preferred = int(preferred or 0)
    if preferred > 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def py_compile_all() -> Subcheck:
    started = time.perf_counter()
    excluded_parts = {".git", ".venv", "venv", "__pycache__", "storage", "database", "logs", "runtime"}
    failures = []
    checked = 0
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        rel_text = str(rel)
        if any(part in rel.parts for part in excluded_parts):
            continue
        checked += 1
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except (SyntaxError, UnicodeDecodeError) as exc:
            failures.append({"path": rel_text, "error": str(exc)})
    return Subcheck(
        name="py_compile all tracked Python sources",
        status="PASS" if not failures else "FAIL",
        severity="CRITICAL" if failures else "LOW",
        duration_ms=int((time.perf_counter() - started) * 1000),
        evidence={"checked_files": checked, "failures": failures[:20]},
        required_followup="Fix Python syntax/import compile failures." if failures else "",
    )


def git_diff_check() -> Subcheck:
    return run_command(["git", "diff", "--check"], timeout=60)


def reports_output_policy_check() -> Subcheck:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8") if (ROOT / ".gitignore").exists() else ""
    required = {
        "legacy_repo_security_reports_absent": not (ROOT / "security" / "reports").exists(),
        "runtime_dir_ignored": "runtime/" in gitignore,
        "security_reports_nested_under_runtime": str(security_reports_root()).endswith("/reports/security"),
    }
    return static_check(
        "security reports output policy keeps generated gate reports under runtime",
        all(required.values()),
        severity="MEDIUM",
        evidence=required,
        followup="Keep generated security reports under runtime/reports/security and out of git.",
    )


def grid_ui_check() -> Subcheck:
    js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
    required = {
        "expanded_state_set": "tradingGridExpandedBots" in js,
        "toggle_button": "data-grid-visual-toggle" in js,
        "stable_panel": "data-grid-visual-panel" in js,
        "fills_panel": "renderGridBotFills" in js,
        "no_details_auto_collapse": "<details class=\"grid-visual-details\"" not in js,
    }
    return static_check(
        "grid trading UI keeps manual expansion state and shows fills beside grid",
        all(required.values()),
        severity="MEDIUM",
        evidence=required,
        followup="Keep expanded state outside rerendered DOM and render fill history in the expanded panel.",
    )


def workflow_bot_ui_check() -> Subcheck:
    js = (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
    required = {
        "save_workflow_bot_action": "saveTradingBot" in js,
        "workflow_bot_save_button": "trading-auto-bot-save-btn" in js,
        "workflow_backtest_button": "trading-workflow-backtest-run-btn" in js,
        "workflow_bot_create_api": 'fetchTradingJson("/trading/bots"' in js,
        "workflow_bot_scan_api": 'fetchTradingJson("/trading/bots/scan"' in js,
        "workflow_template_loader": "loadTradingWorkflowTemplates" in js,
        "workflow_bot_detail_card": "Workflow 機器人" in js,
    }
    return static_check(
        "workflow bot UI keeps create / backtest / scan wiring intact",
        all(required.values()),
        severity="MEDIUM",
        evidence=required,
        followup="Keep the workflow bot editor wired to /api/trading/bots, /api/trading/bots/scan, and the workflow backtest action.",
    )


def build_modules(args) -> list[ModuleResult]:
    out_dir = Path(args.out)
    modules: list[ModuleResult] = []

    server_mode = ModuleResult("A. Server Mode v2")
    server_mode.add(script_check("scripts/security/server_mode/server_mode_v2_clean_smoke.py", ["--out", str(out_dir)], timeout=args.timeout))
    server_mode.add(script_check("scripts/security/server_mode/server_mode_v2_adversarial.py", ["--out", str(out_dir)], timeout=args.timeout))
    server_mode.add(script_check("scripts/security/server_mode/server_mode_v2_redteam_l2.py", ["--out", str(out_dir)], timeout=args.timeout))
    server_mode.add(script_check("scripts/security/server_mode/server_mode_v2_live_http_smoke.py", ["--out", str(out_dir)], timeout=max(args.timeout, 300)))
    server_mode.unresolved_risks.append("Server Mode v2 ready does not cover whole-site production readiness by itself.")
    modules.append(server_mode)

    auth = ModuleResult("B. Account / Auth / Session")
    auth.add(pytest_check([
        "tests/security/auth/test_auth_csrf_safe.py",
        "tests/security/auth/test_session_idle_timeout.py",
        "tests/account/auth/test_account_lockout.py",
        "tests/security/input/test_password_strength.py",
        "tests/account/recovery/test_account_recovery.py",
        "tests/account/sessions/test_account_sessions.py",
    ], timeout=args.timeout))
    modules.append(auth)

    functional = ModuleResult("C. Functional Live Smoke")
    functional_port = pick_available_port(args.functional_smoke_port)
    functional.add(shell_script_check(
        "scripts/security/pentest/run_functional_smoke.sh",
        ["--port", str(functional_port), "--out", str(out_dir)],
        timeout=max(args.timeout, 420),
    ))
    functional.unresolved_risks.append("Functional smoke validates major user/admin flows, but it is not a substitute for focused pentest or browser UX review.")
    modules.append(functional)

    rbac = ModuleResult("D. Permission / RBAC")
    rbac.add(pytest_check([
        "tests/community/test_community_permissions.py",
        "tests/community/test_chat_permissions.py",
        "tests/security/smoke/test_functional_permission_pentest.py",
        "tests/regressions/test_security_issue_regressions.py",
    ], timeout=args.timeout))
    if args.base_url:
        rbac.add(run_command([
            sys.executable, str(ROOT / "scripts" / "security" / "pentest" / "functional_permission_pentest.py"),
            "--base-url", args.base_url,
            "--out-json", str(out_dir / "whole_site_functional_permission.json"),
            "--out-md", str(out_dir / "whole_site_functional_permission.md"),
        ], timeout=args.timeout))
    modules.append(rbac)

    snapshot = ModuleResult("E. Snapshot / Restore")
    snapshot.add(pytest_check([
        "tests/snapshots/test_snapshots.py",
        "tests/frontend/admin/test_frontend_snapshot_actions.py",
    ], timeout=args.timeout))
    modules.append(snapshot)

    economy = ModuleResult("F. PointsChain / Economy")
    economy.add(pytest_check([
        "tests/points/test_points_chain.py",
        "tests/trading/core/test_trading_engine.py",
    ], timeout=args.timeout))
    economy.add(script_check("scripts/trading/validation/trading_exchange_validation.py", ["--out", str(out_dir)], timeout=args.timeout))
    modules.append(economy)

    drive = ModuleResult("G. Cloud Drive")
    drive.add(pytest_check([
        "tests/storage/test_storage_paths.py",
        "tests/storage/test_upload_security.py",
        "tests/storage/test_cloud_drive_attachments.py",
        "tests/storage/test_storage_maintenance.py",
        "tests/storage/test_storage_albums_schema.py",
        "tests/storage/test_remote_downloads.py",
    ], timeout=max(args.timeout, 420)))
    modules.append(drive)

    videos = ModuleResult("H. Video Platform")
    videos.add(pytest_check([
        "tests/video/api/test_video_publish.py",
        "tests/video/api/test_video_permission.py",
        "tests/video/api/test_video_tips.py",
        "tests/video/api/test_video_comments.py",
        "tests/video/security/test_video_security.py",
    ], timeout=args.timeout))
    videos.add(script_check("scripts/security/pentest/video_module_pentest.py", ["--out", str(out_dir)], timeout=args.timeout))
    modules.append(videos)

    trading = ModuleResult("I. Trading / Virtual Exchange")
    trading.add(pytest_check([
        "tests/trading/core/test_trading_engine.py",
        "tests/trading/pricing/test_trading_reference_prices.py",
        "tests/trading/workflow/test_trading_workflow_editor_ui.py",
        "tests/trading/workflow/test_workflow_files.py",
    ], timeout=args.timeout))
    trading.add(script_check("scripts/trading/validation/trading_exchange_validation.py", ["--out", str(out_dir)], timeout=args.timeout))
    trading.add(script_check(
        "scripts/trading/validation/trading_workflow_template_validation.py",
        ["--no-download", "--limit", "200", "--out", str(out_dir)],
        timeout=args.timeout,
    ))
    trading.add(grid_ui_check())
    trading.add(workflow_bot_ui_check())
    if args.base_url:
        if not args.root_password:
            trading.add(static_check(
                "live trading gate credentials configured",
                False,
                severity="HIGH",
                followup="Set WHOLE_SITE_ROOT_PASSWORD before running live production checks.",
            ))
        else:
            trading.add(run_command(
                [
                    sys.executable, str(ROOT / "scripts" / "security" / "pentest" / "trading_stress_pentest.py"),
                    "--base-url", args.base_url,
                    "--mode", "functional_correctness",
                    "--users", "2",
                    "--orders-per-user", "5",
                    "--concurrency", "2",
                    "--rate", "10",
                    "--out", str(out_dir),
                ],
                timeout=max(args.timeout, 360),
                env={"PENTEST_ROOT_PASSWORD": args.root_password},
            ))
    modules.append(trading)

    comfyui = ModuleResult("J. ComfyUI Local Connection")
    comfyui_args_present = bool(args.comfyui_local_base_dir and args.comfyui_local_script)
    if args.base_url and comfyui_args_present:
        if not args.root_password:
            comfyui.add(static_check(
                "ComfyUI local connection credentials configured",
                False,
                severity="HIGH",
                followup="Set WHOLE_SITE_ROOT_PASSWORD before running the ComfyUI production check.",
            ))
        else:
            command = [
                sys.executable, str(ROOT / "scripts" / "comfyui" / "local_connection_smoke.py"),
                "--base-url", args.base_url,
                "--username", args.root_username,
                "--comfyui-base-dir", args.comfyui_local_base_dir,
                "--comfyui-local-script", args.comfyui_local_script,
                "--comfyui-api-host", args.comfyui_api_host,
                "--comfyui-api-port", str(args.comfyui_api_port),
                "--out-json", str(out_dir / "whole_site_comfyui_local_connection_smoke.json"),
                "--out-md", str(out_dir / "whole_site_comfyui_local_connection_smoke.md"),
            ]
            if str(args.base_url).startswith("https://"):
                command.append("--insecure")
            comfyui.add(run_command(
                command,
                timeout=max(args.timeout, 240),
                env={"HACKME_ROOT_PASSWORD": args.root_password},
            ))
    else:
        comfyui.add(static_check(
            "optional ComfyUI local connection smoke not configured",
            True,
            severity="LOW",
            evidence={
                "configured": False,
                "base_url_present": bool(args.base_url),
                "comfyui_local_base_dir": bool(args.comfyui_local_base_dir),
                "comfyui_local_script": bool(args.comfyui_local_script),
            },
            followup="Pass --comfyui-local-base-dir and --comfyui-local-script when the target should verify a local ComfyUI startup path.",
        ))
    modules.append(comfyui)

    forum = ModuleResult("K. Forum / Community / Report")
    forum.add(pytest_check([
        "tests/community/test_community_permissions.py",
        "tests/community/test_reports_notifications.py",
        "tests/community/test_moderation_proposals.py",
        "tests/regressions/test_bug_reports.py",
        "tests/users/test_sanction_notices.py",
    ], timeout=args.timeout))
    modules.append(forum)

    integrity = ModuleResult("L. Integrity Guard")
    integrity.add(pytest_check([
        "tests/security/integrity/test_integrity_guard.py",
        "tests/security/integrity/test_integrity_repair.py",
        "tests/platform/test_settings_audit_reseal.py",
    ], timeout=args.timeout))
    modules.append(integrity)

    audit = ModuleResult("M. Audit / Logs")
    audit.add(pytest_check([
        "tests/security/gates/test_security_events.py",
        "tests/platform/test_settings_audit_reseal.py",
    ], timeout=args.timeout))
    audit.add(script_check("scripts/security/server_mode/server_mode_v2_adversarial.py", ["--out", str(out_dir)], timeout=args.timeout))
    modules.append(audit)

    stress = ModuleResult("N. Stress / Reliability")
    if args.base_url:
        stress.add(run_command([
            sys.executable, str(ROOT / "scripts" / "security" / "pentest" / "stress_test.py"),
            "--target", args.base_url,
            "--requests", str(args.stress_requests),
            "--concurrency", str(args.stress_concurrency),
            "--out", str(out_dir),
        ], timeout=max(args.timeout, 300)))
    else:
        stress.add(static_check(
            "live stress target provided",
            False,
            severity="HIGH",
            followup="Run with --base-url against localhost/staging to validate HTTP stress.",
        ))
    modules.append(stress)

    full_suite = ModuleResult("O. Full Test Suite")
    full_suite.add(py_compile_all())
    full_suite.add(git_diff_check())
    full_suite.add(reports_output_policy_check())
    if not args.skip_full_pytest:
        full_suite.add(run_command(
            [str(ROOT / "scripts" / "testing" / "pytest_in_tmp.sh"), "-q", "tests"],
            timeout=max(args.timeout, args.full_pytest_timeout),
        ))
    else:
        full_suite.add(static_check(
            "pytest full suite explicitly skipped",
            False,
            severity="HIGH",
            followup="Run without --skip-full-pytest before production sign-off.",
        ))
    modules.append(full_suite)

    return modules


def summarize(modules: list[ModuleResult]) -> dict:
    subchecks = [sub for module in modules for sub in module.subchecks]
    failed = [sub for sub in subchecks if not sub.ok]
    critical = [sub for sub in failed if sub.severity == "CRITICAL"]
    high = [sub for sub in failed if sub.severity == "HIGH"]
    medium = [sub for sub in failed if sub.severity == "MEDIUM"]
    unresolved = []
    followups = []
    for module in modules:
        unresolved.extend(module.unresolved_risks)
        for sub in module.subchecks:
            if sub.required_followup:
                followups.append(f"{module.name}: {sub.required_followup}")
    readiness = "YES" if not failed and not critical and not high else "NO"
    return {
        "result": "PASS" if readiness == "YES" else "FAIL",
        "production_readiness": readiness,
        "modules_total": len(modules),
        "modules_passed": sum(1 for module in modules if module.status == "PASS"),
        "modules_failed": sum(1 for module in modules if module.status != "PASS"),
        "critical_findings": len(critical),
        "high_findings": len(high),
        "medium_findings": len(medium),
        "unresolved_risks": unresolved,
        "required_followups": followups,
    }


def module_to_dict(module: ModuleResult) -> dict:
    return {
        "name": module.name,
        "status": module.status,
        "unresolved_risks": module.unresolved_risks,
        "subchecks": [
            {
                "name": sub.name,
                "status": sub.status,
                "severity": sub.severity,
                "command": sub.command,
                "duration_ms": sub.duration_ms,
                "exit_code": sub.exit_code,
                "stdout_tail": sub.stdout_tail,
                "stderr_tail": sub.stderr_tail,
                "evidence": sub.evidence,
                "required_followup": sub.required_followup,
            }
            for sub in module.subchecks
        ],
    }


def write_reports(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"whole_site_production_gate_{stamp}.json"
    md_path = out_dir / f"whole_site_production_gate_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    summary = report["WHOLE_SITE_PRODUCTION_GATE_SUMMARY"]
    lines = [
        "# Whole Site Production Gate",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- base_url: `{report.get('base_url') or '-'}`",
        f"- result: `{summary['result']}`",
        f"- production_readiness: `{summary['production_readiness']}`",
        "",
        "## WHOLE_SITE_PRODUCTION_GATE_SUMMARY",
        "",
    ]
    for key in (
        "result",
        "production_readiness",
        "modules_total",
        "modules_passed",
        "modules_failed",
        "critical_findings",
        "high_findings",
        "medium_findings",
    ):
        lines.append(f"- {key}: `{summary[key]}`")
    lines.extend(["", "### Unresolved Risks", ""])
    if summary["unresolved_risks"]:
        lines.extend(f"- {item}" for item in summary["unresolved_risks"])
    else:
        lines.append("- none")
    lines.extend(["", "### Required Followups", ""])
    if summary["required_followups"]:
        lines.extend(f"- {item}" for item in summary["required_followups"])
    else:
        lines.append("- none")
    lines.extend(["", "## Module Results", ""])
    for module in report["modules"]:
        lines.extend([f"### {module['name']}", "", f"- status: `{module['status']}`", ""])
        for sub in module["subchecks"]:
            lines.extend([
                f"#### {sub['name']}",
                "",
                f"- status: `{sub['status']}`",
                f"- severity: `{sub['severity']}`",
                f"- duration_ms: `{sub['duration_ms']}`",
                f"- exit_code: `{sub['exit_code']}`",
            ])
            if sub.get("required_followup"):
                lines.append(f"- followup: {sub['required_followup']}")
            if sub.get("stderr_tail"):
                lines.extend(["", "```text", sub["stderr_tail"], "```"])
            elif sub.get("stdout_tail") and sub["status"] != "PASS":
                lines.extend(["", "```text", sub["stdout_tail"], "```"])
            lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def parse_args():
    parser = argparse.ArgumentParser(description="Run whole-site production gate for hackme_web.")
    parser.add_argument("--base-url", default=os.environ.get("WHOLE_SITE_GATE_BASE_URL", "http://127.0.0.1:5000"))
    parser.add_argument("--out", default=str(security_reports_root()))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--full-pytest-timeout",
        type=int,
        default=int(os.environ.get("WHOLE_SITE_FULL_PYTEST_TIMEOUT", DEFAULT_FULL_PYTEST_TIMEOUT)),
        help="Timeout for the complete pytest suite; kept separate from individual module checks.",
    )
    parser.add_argument("--root-username", default=os.environ.get("WHOLE_SITE_ROOT_USERNAME", "root"))
    parser.add_argument(
        "--root-password",
        default=os.environ.get("WHOLE_SITE_ROOT_PASSWORD") or os.environ.get("HACKME_ROOT_PASSWORD", ""),
        help="Root password for live probes. Prefer WHOLE_SITE_ROOT_PASSWORD over a command-line value.",
    )
    parser.add_argument("--functional-smoke-port", type=int, default=50741)
    parser.add_argument("--stress-requests", type=int, default=60)
    parser.add_argument("--stress-concurrency", type=int, default=10)
    parser.add_argument("--comfyui-local-base-dir", default=os.environ.get("WHOLE_SITE_COMFYUI_LOCAL_BASE_DIR", ""))
    parser.add_argument("--comfyui-local-script", default=os.environ.get("WHOLE_SITE_COMFYUI_LOCAL_SCRIPT", ""))
    parser.add_argument("--comfyui-api-host", default=os.environ.get("WHOLE_SITE_COMFYUI_API_HOST", "127.0.0.1"))
    parser.add_argument("--comfyui-api-port", type=int, default=int(os.environ.get("WHOLE_SITE_COMFYUI_API_PORT", "8192")))
    parser.add_argument("--skip-full-pytest", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not shutil.which("git"):
        raise SystemExit("git is required")
    modules = build_modules(args)
    summary = summarize(modules)
    report = {
        "generated_at": datetime.now().isoformat(),
        "base_url": args.base_url,
        "WHOLE_SITE_PRODUCTION_GATE_SUMMARY": summary,
        "modules": [module_to_dict(module) for module in modules],
    }
    json_path, md_path = write_reports(report, Path(args.out))
    print(json.dumps({
        "ok": summary["production_readiness"] == "YES",
        "summary": summary,
        "json_report": str(json_path),
        "md_report": str(md_path),
    }, ensure_ascii=False, indent=2))
    return 0 if summary["production_readiness"] == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
