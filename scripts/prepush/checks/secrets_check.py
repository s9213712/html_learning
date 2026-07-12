from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from scripts.prepush import utils
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


SECRET_PATTERNS = {
    "PASSWORD_ASSIGNMENT": re.compile(
        r"(?i)(?:(?<![\w-])(?:[A-Za-z0-9_]+_)?(?:passwd|password)\b\s*=|(?:^|[,{])\s*['\"](?:passwd|password)['\"]\s*:)\s*"
        r"(?:['\"][A-Za-z0-9 ./+=:@!#$%^&*()_<>-]{4,}['\"]|(?=[A-Za-z0-9./+=:@-]{8,}\b)(?=[A-Za-z0-9./+=:@-]*\d)[A-Za-z0-9./+=:@-]+)"
    ),
    "SECRET_ASSIGNMENT": re.compile(
        r"(?i)(?:(?<![\w-])(?:[A-Za-z0-9_]+_)?(?:secret|token|api_key)\b\s*=|(?:^|[,{])\s*['\"](?:secret|token|api_key)['\"]\s*:)\s*"
        r"(?:['\"][A-Za-z0-9 ./+=:@!#$%^&*()_<>-]{4,}['\"]|(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[bp]-[A-Za-z0-9-]{12,}))"
    ),
    "PRIVATE_KEY": re.compile(r"(?i)BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
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
GITLEAKS_MAX_TARGET_BYTES = 2 * 1024 * 1024
BULK_GENERATED_PREFIXES = (
    "docs/AGENTS/reports/",
    "output/",
)
FIXTURE_PREFIXES = (
    "tests/",
    "scripts/testing/",
    "scripts/security/pentest/",
    "scripts/trading/validation/",
    "docs/AGENTS/skills/hackme-web-qa/scripts/",
)


def gitleaks_timeout_seconds() -> int:
    try:
        requested = int(os.environ.get("PREPUSH_GITLEAKS_TIMEOUT_SECONDS", str(DEFAULT_GITLEAKS_TIMEOUT_SECONDS)).strip())
    except (TypeError, ValueError):
        requested = DEFAULT_GITLEAKS_TIMEOUT_SECONDS
    return max(30, min(1800, requested))


def line_allowed(line: str) -> bool:
    lowered = line.lower()
    if any(hint.lower() in lowered for hint in ALLOW_HINTS):
        return True
    return bool(re.search(r"<[^>\r\n]*(?:password|secret|token|key)[^>\r\n]*>", lowered))


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
            if rel.startswith(FIXTURE_PREFIXES) and name in {
                "PASSWORD_ASSIGNMENT",
                "SECRET_ASSIGNMENT",
                "PRIVATE_KEY",
            }:
                continue
            match = pattern.search(line)
            if match:
                if any(marker in match.group(0) for marker in ("$", "{{", "}}")):
                    continue
                findings.append(
                    {
                        "file": rel,
                        "line": line_no,
                        "pattern": name,
                        "evidence": utils.redact_secret(line),
                    }
                )
    return findings


def _normalized_relpath(value: str) -> str:
    normalized = str(value or "").replace("\\", "/")
    return normalized[2:] if normalized.startswith("./") else normalized


def gitleaks_candidate_paths(ctx: PrepushContext) -> list[str]:
    delta = {
        _normalized_relpath(rel)
        for rel in (*ctx.staged_files, *ctx.changed_files, *ctx.untracked_files)
        if rel
    }
    selected = set(delta)
    selected.update(SCAN_EXTRA)
    if ctx.mode == "full":
        selected.update(_normalized_relpath(rel) for rel in ctx.tracked_files if rel)

    candidates: list[str] = []
    repo_root = ctx.repo_root.resolve()
    for rel in sorted(selected):
        if not rel or rel.startswith("../") or rel.startswith("/"):
            continue
        if any(rel.startswith(prefix) for prefix in BULK_GENERATED_PREFIXES) and rel not in delta:
            continue
        source = repo_root / rel
        if source.is_symlink() or not source.is_file():
            continue
        try:
            source.resolve().relative_to(repo_root)
            if source.stat().st_size > GITLEAKS_MAX_TARGET_BYTES:
                continue
        except (OSError, ValueError):
            continue
        if utils.is_text_file(source):
            candidates.append(rel)
    return candidates


def materialize_gitleaks_source(ctx: PrepushContext, scan_root: Path, candidates: list[str]) -> int:
    count = 0
    for rel in candidates:
        source = ctx.repo_root / rel
        destination = scan_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
        count += 1
    return count


def run(ctx: PrepushContext) -> CheckResult:
    targets = sorted(set(ctx.staged_files + ctx.changed_files + ctx.untracked_files + list(SCAN_EXTRA)))
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

    timeout_seconds = gitleaks_timeout_seconds()
    candidates = gitleaks_candidate_paths(ctx)
    try:
        with tempfile.TemporaryDirectory(prefix="hackme_gitleaks_", dir="/tmp") as temp_dir:
            scan_root = Path(temp_dir) / "source"
            scan_root.mkdir()
            scanned_count = materialize_gitleaks_source(ctx, scan_root, candidates)
            proc = utils.run_command(
                [
                    "gitleaks",
                    "detect",
                    "--source",
                    str(scan_root),
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
                "Inspect the candidate file set and gitleaks rules, or set "
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
    return CheckResult.pass_(
        "secrets scan",
        f"custom scanner and gitleaks passed for {scanned_count} candidate file(s)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bounded pre-push secret scanner.")
    parser.add_argument("--mode", choices=("quick", "full"), default="quick")
    parser.add_argument("--strict", action="store_true", help="Fail when gitleaks is unavailable.")
    parser.add_argument("--report-json", default="")
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[3]
    ctx = PrepushContext.build(repo_root=repo_root, mode=args.mode, is_ci=args.strict)
    started = time.perf_counter()
    result = run(ctx)
    result.elapsed_seconds = round(time.perf_counter() - started, 3)
    payload = result.to_json()
    if args.report_json:
        report_path = Path(args.report_json).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 1 if result.status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
