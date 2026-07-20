from __future__ import annotations

from pathlib import PurePosixPath

from scripts.prepush import utils
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


FORBIDDEN_PATTERNS = (
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.db-journal",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite-journal",
    "*.sqlite3",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    "*.sqlite3-journal",
    "*.log",
    "*.pem",
    "*.key",
    ".csrfkey",
    ".fkey",
    ".filekey",
    ".integrity_key",
    ".chain_seed",
    ".server_mode_log_hmac_key",
    "integrity_manifest.json",
    "**/__pycache__/**",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
)


def is_forbidden(rel: str) -> bool:
    if rel.endswith("/.gitkeep") or rel == ".gitkeep":
        return False
    if rel.endswith(":Zone.Identifier"):
        return True
    root_artifact_prefixes = (
        "anchors",
        "chats",
        "reports",
        "security/audit_exports",
        "storage",
        "logs",
        "runtime",
        "hackme_web_runtime",
        "html_learning_storage",
        "output",
        "public/generated",
        "node_modules",
        "dist",
        "build",
    )
    if any(rel == prefix or rel.startswith(prefix + "/") for prefix in root_artifact_prefixes):
        return True
    path = PurePosixPath(rel)
    if path.parts[:3] == ("docs", "games", "evidence") and "_runtime" in path.parts[3:]:
        return True
    return any(path.match(pattern) for pattern in FORBIDDEN_PATTERNS)


def run(ctx: PrepushContext) -> CheckResult:
    violations = []
    staged_deletions = set(utils.git_lines(ctx.repo_root, "diff", "--cached", "--name-only", "--diff-filter=D"))
    for source, files in (("tracked", ctx.tracked_files), ("staged", ctx.staged_files)):
        for rel in files:
            if source == "staged" and rel in staged_deletions:
                continue
            if is_forbidden(rel):
                violations.append({"source": source, "file": rel})
    if violations:
        return CheckResult.fail(
            "forbidden runtime files",
            "runtime/cache/report/key artifacts are tracked or staged",
            severity="critical",
            details=violations[:80],
            remediation="Remove generated artifacts from git; keep only .gitkeep placeholders where needed.",
        )
    return CheckResult.pass_("forbidden runtime files", "no forbidden runtime artifacts are tracked or staged")
