from __future__ import annotations

import os
import re
import subprocess

from scripts.prepush import utils
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


SECRET_PATTERNS = {
    "PASSWORD_ASSIGNMENT": re.compile(r"(?i)\b(passwd|password)\b\s*[:=]\s*(?:['\"][^'\"]{4,}['\"]|\b[A-Za-z0-9./+=:@-]{8,}\b)"),
    "SECRET_ASSIGNMENT": re.compile(r"(?i)\b(secret|token|api_key)\b\s*[:=]\s*(?:['\"][^'\"]{4,}['\"]|\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[bp]-[A-Za-z0-9-]{12,}|[A-Za-z0-9./+=:@-]{20,})\b)"),
    "PRIVATE_KEY": re.compile(r"(?i)private_key|BEGIN (RSA )?PRIVATE KEY"),
    "BEARER_TOKEN": re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9_.-]+", re.I),
    "OPENAI_STYLE_KEY": re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    "GITHUB_TOKEN": re.compile(r"\b(ghp_|github_pat_)[A-Za-z0-9_]{12,}"),
    "SLACK_TOKEN": re.compile(r"\bxox[bp]-[A-Za-z0-9-]{12,}"),
}
ALLOW_HINTS = ("example", "dummy", "fake", "test-only", "placeholder", "changeme", "allowlist", "redacted", "masked", "Admin@1234")
SCAN_EXTRA = (
    "README.md",
    "docs/README.zh-TW.md",
    "docs/00_START_HERE.md",
    "docs/01_DEPLOY_QUICKSTART.md",
    "docs/02_DEPLOY_PRODUCTION.md",
    "docs/03_ADMIN_GUIDE.md",
    "docs/04_USER_GUIDE.md",
    "docs/05_FEATURES_OVERVIEW.md",
    "docs/11_QA_TESTING.md",
    "docs/12_TROUBLESHOOTING.md",
    "docs/For_developer.md",
    "docs/UPDATE_SUMMARY.md",
)
DEFAULT_GITLEAKS_TIMEOUT_SECONDS = 300


def gitleaks_timeout_seconds() -> int:
    try:
        requested = int(os.environ.get("PREPUSH_GITLEAKS_TIMEOUT_SECONDS", str(DEFAULT_GITLEAKS_TIMEOUT_SECONDS)).strip())
    except (TypeError, ValueError):
        requested = DEFAULT_GITLEAKS_TIMEOUT_SECONDS
    return max(30, min(1800, requested))


def line_allowed(line: str) -> bool:
    lowered = line.lower()
    return any(hint.lower() in lowered for hint in ALLOW_HINTS)


def scan_text(rel: str, text: str) -> list[dict[str, object]]:
    findings = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line_allowed(line):
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if rel == "scripts/security/gate/scan_plaintext_secrets.py" and name == "PRIVATE_KEY":
                continue
            if rel == "tests/security/input/test_plaintext_secrets_scan.py" and name in {"PASSWORD_ASSIGNMENT", "SECRET_ASSIGNMENT", "PRIVATE_KEY"}:
                continue
            if rel in {
                "tests/test_prepush_v2.py",
                "tests/scripts/prepush/test_prepush_v2.py",
                "tests/test_snapshots.py",
            } and name in {"SECRET_ASSIGNMENT", "OPENAI_STYLE_KEY"}:
                continue
            match = pattern.search(line)
            if match:
                findings.append(
                    {
                        "file": rel,
                        "line": line_no,
                        "pattern": name,
                        "evidence": utils.redact_secret(line),
                    }
                )
    return findings


def run(ctx: PrepushContext) -> CheckResult:
    targets = sorted(set(ctx.staged_files + list(SCAN_EXTRA)))
    findings = []
    for path in utils.iter_repo_text_files(ctx.repo_root, targets):
        rel = ctx.relpath(path)
        if rel in {"scripts/prepush/checks/secrets_check.py"}:
            continue
        findings.extend(scan_text(rel, path.read_text(encoding="utf-8", errors="replace")))

    if findings:
        return CheckResult.fail(
            "secrets scan",
            "potential secret-like values found",
            severity="critical",
            details=findings[:80],
            remediation="Move real secrets to env/local key files and use explicit placeholders in docs/tests.",
        )

    if not utils.tool_exists("gitleaks"):
        if ctx.is_ci and not bool(__import__("os").environ.get("ALLOW_MISSING_GITLEAKS")):
            return CheckResult.fail(
                "gitleaks availability",
                "gitleaks is missing in CI",
                severity="high",
                remediation="Install gitleaks in CI or set ALLOW_MISSING_GITLEAKS=1 only for trusted fallback runs.",
            )
        return CheckResult.warn("gitleaks availability", "gitleaks is not installed; custom secrets scan passed")

    # Run from the repo root with a relative source so `.gitleaks.toml` path
    # allowlists such as `^runtime/` match generated local runtime trees.
    timeout_seconds = gitleaks_timeout_seconds()
    try:
        proc = utils.run_command(
            [
                "gitleaks",
                "detect",
                "--source",
                ".",
                "--no-git",
                "--redact",
                "--max-target-megabytes",
                "2",
                "--no-banner",
                "--log-level",
                "error",
                "--config",
                str(ctx.repo_root / ".gitleaks.toml"),
            ],
            cwd=ctx.repo_root,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return CheckResult.fail(
            "gitleaks scan",
            f"gitleaks timed out after {timeout_seconds} seconds",
            severity="high",
            remediation=(
                "Inspect repository size and gitleaks exclusions, or set "
                "PREPUSH_GITLEAKS_TIMEOUT_SECONDS to a bounded value between 30 and 1800."
            ),
        )
    if proc.returncode != 0:
        return CheckResult.fail(
            "gitleaks scan",
            "gitleaks reported potential secrets",
            severity="critical",
            details=[{"output": utils.redact_secret(proc.stdout + proc.stderr)[-1600:]}],
            remediation="Review gitleaks output and remove or allowlist only fake test credentials.",
        )
    return CheckResult.pass_("secrets scan", "custom scanner and gitleaks passed")
