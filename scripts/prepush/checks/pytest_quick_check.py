from __future__ import annotations

import os
import sys

from scripts.prepush import utils
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


QUICK_TESTS = [
    "tests/security/auth/test_auth_csrf_safe.py",
    "tests/security/auth/test_access_controls.py",
    "tests/scripts/prepush/test_prepush_v2.py",
    "tests/frontend/admin/test_frontend_account_admin.py",
    "tests/account/sessions/test_account_sessions.py",
    "tests/comfyui/generation/test_comfyui_generation.py",
    "tests/storage/test_cloud_drive_attachments.py",
    "tests/users/test_sanction_notices.py",
    "tests/trading/core/test_trading_engine.py",
    "tests/storage/test_remote_downloads.py",
    "tests/video/api/test_video_publish.py",
    "tests/services/test_management_plane.py",
    "tests/regressions/test_security_issue_regressions.py",
    "tests/users/test_user_csv_exports.py",
]
DEFAULT_QUICK_PYTEST_TIMEOUT_SECONDS = 1800
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


def run(ctx: PrepushContext) -> CheckResult:
    if not utils.tool_exists("pytest"):
        return CheckResult.fail("quick pytest", "pytest is not installed", severity="high", remediation="Install test dependencies with pip.")
    tests = [rel for rel in QUICK_TESTS if (ctx.repo_root / rel).exists()]
    if not tests:
        return CheckResult.skip("quick pytest", "quick pytest target files are missing")
    env = utils.env_without_local_runtime()
    env["PYTHONPATH"] = str(ctx.repo_root)
    env["HTML_LEARNING_TEST_RUNTIME"] = "1"
    proc = utils.run_command(
        [sys.executable, "-m", "pytest", "-q", *tests],
        cwd=ctx.repo_root,
        timeout=QUICK_TIMEOUT_SECONDS,
        env=env,
    )
    if proc.returncode != 0:
        output = "\n".join((proc.stdout + proc.stderr).splitlines()[-80:])
        return CheckResult.fail(
            "quick pytest",
            "selected quick tests failed",
            severity="high",
            details=[{"output": utils.sanitize_path(output)}],
            remediation="Run the listed pytest command locally and fix failing tests.",
        )
    return CheckResult.pass_("quick pytest", f"passed {len(tests)} quick test file(s)")
