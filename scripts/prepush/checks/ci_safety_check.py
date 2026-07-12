from __future__ import annotations

import re
from pathlib import Path

from scripts.prepush import utils
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


LOCAL_PATH_PATTERNS = {
    "WSL_DRIVE_PATH": "/mnt/d",
    "WINDOWS_USER_PATH": "C:\\Users\\",
}
_HOME_MARKER = str(Path.home()).replace("\\", "/").rstrip("/")
if _HOME_MARKER and _HOME_MARKER not in {"/", "."}:
    LOCAL_PATH_PATTERNS["LOCAL_HOME_PATH"] = _HOME_MARKER


def run(ctx: PrepushContext) -> CheckResult:
    findings = []
    scan = []
    for rel in ("tests", "scripts", "requirements.txt", "pyproject.toml", "package.json"):
        path = ctx.repo_root / rel
        if path.is_file():
            scan.append(rel)
        elif path.is_dir():
            scan.extend(item.relative_to(ctx.repo_root).as_posix() for item in path.rglob("*") if item.is_file())
    for path in utils.iter_repo_text_files(ctx.repo_root, scan):
        rel = ctx.relpath(path)
        if rel.startswith("scripts/prepush/"):
            continue
        if rel in {
            "tests/test_prepush_v2.py",
            "tests/scripts/prepush/test_prepush_v2.py",
            "tests/scripts/security/test_on_live_reports_make_script.py",
        }:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            lowered = line.lower()
            if "ci-safety: fixture-only" in lowered:
                continue
            if "localhost:5000" in line or "127.0.0.1:5000" in line:
                if (
                    "example" not in lowered
                    and "docs" not in rel
                    and "client." not in line
                    and '"target"' not in line
                    and "default=" not in line
                    and "default:" not in lowered
                    and not (
                        rel == "scripts/security/gate/on_live_reports_make.py"
                        and "for base in (" in line
                    )
                ):
                    findings.append({"file": rel, "line": line_no, "problem": "fixed port 5000"})
            for name, marker in LOCAL_PATH_PATTERNS.items():
                if marker in line and "sanitize" not in lowered and "not in" not in lowered and "local path" not in lowered:
                    findings.append({"file": rel, "line": line_no, "problem": name})

    if not utils.tool_exists("pytest"):
        findings.append({"tool": "pytest", "problem": "missing"})

    if findings:
        return CheckResult.fail(
            "CI safety",
            "CI portability risks found",
            severity="medium",
            details=findings[:80],
            remediation="Remove local paths/fixed ports and install declared test tools.",
        )
    return CheckResult.pass_("CI safety", "tests/scripts avoid obvious local-only CI assumptions")
