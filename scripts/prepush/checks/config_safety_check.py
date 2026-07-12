from __future__ import annotations

import re

from scripts.prepush import utils
from scripts.prepush.checks.docs_command_targets_check import canonical_docs
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


UNSAFE = {
    "DEBUG=True": re.compile(r"\bDEBUG\s*=\s*True\b"),
    "FLASK_ENV=development": re.compile(r"\bFLASK_ENV\s*=\s*development\b"),
    "ALLOW_ALL_ORIGINS=True": re.compile(r"\bALLOW_ALL_ORIGINS\s*=\s*True\b"),
    "CORS_ALLOW_ALL=True": re.compile(r"\bCORS_ALLOW_ALL\s*=\s*True\b"),
    "DISABLE_CSRF=True": re.compile(r"\bDISABLE_CSRF\s*=\s*True\b"),
    "DISABLE_AUTH=True": re.compile(r"\bDISABLE_AUTH\s*=\s*True\b"),
    "BYPASS_AUTH=True": re.compile(r"\bBYPASS_AUTH\s*=\s*True\b"),
    "INTERNAL_API_PUBLIC=True": re.compile(r"\bINTERNAL_API_PUBLIC\s*=\s*True\b"),
    "COOKIE_SECURE=False": re.compile(r"\bCOOKIE_SECURE\s*=\s*False\b"),
    "SESSION_COOKIE_SECURE=False": re.compile(r"\bSESSION_COOKIE_SECURE\s*=\s*False\b"),
}
TARGETS = ("server.py", "routes", "services", "config", ".env.example", ".env.production.example")


def run(ctx: PrepushContext) -> CheckResult:
    paths: list[str] = []
    for target in TARGETS:
        path = ctx.repo_root / target
        if path.is_file():
            paths.append(target)
        elif path.is_dir():
            paths.extend(item.relative_to(ctx.repo_root).as_posix() for item in path.rglob("*") if item.is_file())
    paths.extend(path.relative_to(ctx.repo_root).as_posix() for path in canonical_docs(ctx.repo_root))
    findings = []
    for path in utils.iter_repo_text_files(ctx.repo_root, paths):
        rel = ctx.relpath(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            if rel.endswith(".md") and ("example" in lowered or "do not use" in lowered or "unsafe" in lowered):
                continue
            for name, pattern in UNSAFE.items():
                if pattern.search(line):
                    findings.append({"file": rel, "line": line_no, "setting": name})
    if findings:
        return CheckResult.fail(
            "config safety",
            "production-dangerous config defaults found",
            severity="high",
            details=findings[:80],
            remediation="Use safe defaults and document unsafe examples only as placeholders.",
        )
    return CheckResult.pass_("config safety", "no unsafe production defaults found")
