from __future__ import annotations

import os
import subprocess

from scripts.prepush import utils
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


QUICK_TESTS = [
    "tests/security/auth/test_auth_csrf_safe.py",
    "tests/security/auth/test_access_controls.py",
    "tests/scripts/prepush/test_prepush_v2.py",
    "tests/frontend/admin/test_frontend_account_admin.py",
    "tests/account/sessions/test_account_sessions.py",
    "tests/users/test_sanction_notices.py",
    "tests/trading/core/test_trading_engine.py::test_trading_asset_overview_uses_lightweight_read_model",
    "tests/trading/core/test_trading_engine.py::test_trading_asset_overview_uses_current_legacy_wallet_columns_without_identity_balance",
    "tests/services/test_management_plane.py",
    "tests/regressions/test_security_issue_regressions.py",
]
FULL_EXTRA_TESTS = [
    "tests/comfyui/generation/test_comfyui_generation.py",
    "tests/scripts/testing/test_operational_campaign_24h.py",
    "tests/scripts/testing/test_probe_credentials.py",
    "tests/scripts/testing/test_pytest_in_tmp_wrapper.py",
    "tests/scripts/testing/test_video_hls_quality_stress.py",
    "tests/storage/test_cloud_drive_attachments.py",
    "tests/storage/test_remote_downloads.py",
    "tests/video/api/test_video_publish.py",
    "tests/users/test_user_csv_exports.py",
    "tests/trading/core/test_trading_engine.py",
]
DEFAULT_QUICK_PYTEST_TIMEOUT_SECONDS = 900
DEFAULT_FULL_PYTEST_TIMEOUT_SECONDS = 3600
try:
    QUICK_TIMEOUT_SECONDS = max(
        30,
        min(
            3600,
            int(os.environ.get("PREPUSH_PYTEST_TIMEOUT_SECONDS", str(DEFAULT_QUICK_PYTEST_TIMEOUT_SECONDS)).strip()),
        ),
    )
except Exception:
    QUICK_TIMEOUT_SECONDS = DEFAULT_QUICK_PYTEST_TIMEOUT_SECONDS
QUICK_PYTEST_TIMEOUT_SECONDS = QUICK_TIMEOUT_SECONDS
try:
    FULL_PYTEST_TIMEOUT_SECONDS = max(
        QUICK_PYTEST_TIMEOUT_SECONDS,
        min(
            7200,
            int(os.environ.get("PREPUSH_FULL_PYTEST_TIMEOUT_SECONDS", str(DEFAULT_FULL_PYTEST_TIMEOUT_SECONDS)).strip()),
        ),
    )
except Exception:
    FULL_PYTEST_TIMEOUT_SECONDS = DEFAULT_FULL_PYTEST_TIMEOUT_SECONDS


def run(ctx: PrepushContext) -> CheckResult:
    if not utils.tool_exists("pytest"):
        return CheckResult.fail("quick pytest", "pytest is not installed", severity="high", remediation="Install test dependencies with pip.")
    if ctx.mode == "full":
        full_paths = set(FULL_EXTRA_TESTS)
        configured_tests = [
            rel for rel in QUICK_TESTS
            if rel.split("::", 1)[0] not in full_paths
        ] + FULL_EXTRA_TESTS
    else:
        configured_tests = list(QUICK_TESTS)
    tests = [rel for rel in configured_tests if (ctx.repo_root / rel).exists()]
    if not tests:
        return CheckResult.skip("quick pytest", "quick pytest target files are missing")
    env = utils.env_without_local_runtime()
    env["PYTHONPATH"] = str(ctx.repo_root)
    env["HTML_LEARNING_TEST_RUNTIME"] = "1"
    wrapper = ctx.repo_root / "scripts" / "testing" / "pytest_in_tmp.sh"
    if not wrapper.is_file():
        return CheckResult.fail(
            "quick pytest",
            "scripts/testing/pytest_in_tmp.sh is missing",
            severity="high",
            remediation="Restore the isolated pytest wrapper before running pre-push checks.",
        )
    timeout_seconds = FULL_PYTEST_TIMEOUT_SECONDS if ctx.mode == "full" else QUICK_PYTEST_TIMEOUT_SECONDS
    try:
        proc = utils.run_command(
            [str(wrapper), "-q", *tests],
            cwd=ctx.repo_root,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raw_output = exc.stdout or ""
        if isinstance(raw_output, bytes):
            raw_output = raw_output.decode("utf-8", errors="replace")
        output = "\n".join(str(raw_output).splitlines()[-40:])
        return CheckResult.fail(
            "quick pytest" if ctx.mode != "full" else "full pre-push pytest",
            f"isolated pytest exceeded the {timeout_seconds}s {ctx.mode} budget",
            severity="high",
            details=[{"output": utils.sanitize_path(output)}] if output else [],
            remediation="Run the listed pytest targets with --durations=30, then move slow integration coverage to the full gate or fix the blocking test.",
        )
    if proc.returncode != 0:
        output = "\n".join((proc.stdout + proc.stderr).splitlines()[-80:])
        return CheckResult.fail(
            "quick pytest",
            "selected quick tests failed",
            severity="high",
            details=[{"output": utils.sanitize_path(output)}],
            remediation="Run scripts/testing/pytest_in_tmp.sh with the listed tests and fix failures.",
        )
    label = "full pre-push pytest" if ctx.mode == "full" else "quick pytest"
    return CheckResult.pass_(label, f"passed {len(tests)} {ctx.mode} test file(s)")
