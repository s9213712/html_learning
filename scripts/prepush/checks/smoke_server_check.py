from __future__ import annotations

import os
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from scripts.prepush import utils
from scripts.prepush.checks.cleanup_check import cleanup_current_runtime
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


def probe(base_url: str) -> bool:
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(urllib.request.Request(base_url + "/api/csrf-token"), timeout=3, context=ctx) as response:
        return response.status == 200


def wait_for_server(base_urls: list[str], deadline: int = 25) -> str:
    started = time.time()
    last_error: Exception | None = None
    while time.time() - started < deadline:
        for base_url in base_urls:
            try:
                if probe(base_url):
                    return base_url
            except Exception as exc:  # noqa: BLE001 - diagnostic only
                last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"server did not become ready: {last_error}")


def start_server(ctx: PrepushContext, runtime_root: Path, port: int):
    env = os.environ.copy()
    env.update(
        {
            "HTML_LEARNING_DB_DIR": str(runtime_root / "database"),
            "HTML_LEARNING_LOG_DIR": str(runtime_root / "logs"),
            "HTML_LEARNING_CHAT_DIR": str(runtime_root / "chats"),
            "HTML_LEARNING_ANCHOR_DIR": str(runtime_root / "anchors"),
            "HTML_LEARNING_STORAGE_DIR": str(runtime_root / "storage"),
            "HTML_LEARNING_REPORTS_DIR": str(runtime_root / "reports"),
            "HTML_LEARNING_SECRET_DIR": str(runtime_root / "secrets"),
            "HTML_LEARNING_UPLOAD_DIR": str(runtime_root / "uploads"),
            "HTML_LEARNING_HOST": "0.0.0.0",
            "HTML_LEARNING_DISABLE_TRUSTED_HOSTS": "1",
            "HTML_LEARNING_PORT": str(port),
            "HTML_LEARNING_ROOT_PASSWORD": "root",
            "HTML_LEARNING_MANAGER_PASSWORD": "admin",
            "HTML_LEARNING_TEST_PASSWORD": "test",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(ctx.repo_root),
        }
    )
    for name in ("database", "logs", "chats", "anchors", "storage", "reports", "secrets", "uploads"):
        (runtime_root / name).mkdir(parents=True, exist_ok=True)
    log_path = runtime_root / "server.log"
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ctx.repo_root / "server.py")],
        cwd=str(ctx.repo_root),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return proc, log_file


def run(ctx: PrepushContext) -> CheckResult:
    runtime_root = ctx.ensure_temp_root()
    port = utils.find_free_port()
    proc = None
    log_file = None
    success = False
    try:
        proc, log_file = start_server(ctx, runtime_root, port)
        base_url = wait_for_server([f"https://127.0.0.1:{port}", f"http://127.0.0.1:{port}"])
        smoke = ctx.repo_root / "tests" / "security" / "smoke" / "smoke_suite.py"
        smoke_env = os.environ.copy()
        smoke_env.update({
            "ROOT_PASSWORD": "RootSmoke123!",
            "MANAGER_PASSWORD": "ManagerSmoke123!",
            "TEST_PASSWORD": "TestSmoke123!",
        })
        proc_smoke = utils.run_command(
            [sys.executable, str(smoke), "--base-url", base_url, "--suite", "all"],
            cwd=ctx.repo_root,
            timeout=180,
            env=smoke_env,
        )
        if proc_smoke.returncode != 0:
            output = "\n".join((proc_smoke.stdout + proc_smoke.stderr).splitlines()[-80:])
            return CheckResult.fail(
                "isolated server smoke",
                "smoke suite failed in isolated runtime",
                severity="high",
                details=[{"runtime": ctx.sanitize_path(runtime_root), "output": utils.sanitize_path(output)}],
                remediation="Inspect the kept temp runtime and server.log, then fix the failed smoke step.",
            )
        success = True
        return CheckResult.pass_("isolated server smoke", f"smoke suite passed on isolated port {port}", details=[{"runtime": ctx.sanitize_path(runtime_root)}])
    except Exception as exc:  # noqa: BLE001
        return CheckResult.fail(
            "isolated server smoke",
            f"failed to run isolated smoke: {ctx.sanitize_path(str(exc))}",
            severity="high",
            details=[{"runtime": ctx.sanitize_path(runtime_root)}],
            remediation="Start the isolated runtime manually with the printed sanitized temp directory if needed.",
        )
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log_file is not None:
            log_file.close()
        cleanup_current_runtime(runtime_root, success=success, ci=ctx.is_ci, keep_temp=ctx.keep_temp)
