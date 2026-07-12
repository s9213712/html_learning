from __future__ import annotations

import re
from pathlib import Path

from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


WATCHED_ROOTS = (
    Path("scripts/admin"),
    Path("scripts/comfyui"),
    Path("scripts/games"),
    Path("scripts/media"),
    Path("scripts/on_live_reports"),
    Path("scripts/ops"),
    Path("scripts/qa"),
    Path("scripts/security"),
    Path("scripts/storage"),
    Path("scripts/testing"),
    Path("scripts/trading"),
)
SCRIPT_SUFFIXES = {".py", ".sh"}
HELPER_NAMES = {
    "__init__.py",
    "common_paths.py",
    "probe_credentials.py",
    "operation_coverage.py",
    "comfyui_run_in_linux.template.sh",
}
INDEX_SCRIPT_RE = re.compile(r"`(scripts/[^`\n]+\.(?:py|sh))`")


def iter_index_required_scripts(repo_root: Path):
    for watched in WATCHED_ROOTS:
        root = repo_root / watched
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() and not path.is_symlink():
                continue
            if path.name in HELPER_NAMES or path.suffix not in SCRIPT_SUFFIXES:
                continue
            yield path


def run(ctx: PrepushContext) -> CheckResult:
    index_path = ctx.repo_root / "scripts" / "INDEX.md"
    if not index_path.exists():
        return CheckResult.fail(
            "scripts index",
            "scripts/INDEX.md is missing",
            severity="high",
            remediation="Create scripts/INDEX.md before adding QA/security scripts.",
        )
    index_text = index_path.read_text(encoding="utf-8")
    missing = []
    checked = 0
    for path in iter_index_required_scripts(ctx.repo_root):
        checked += 1
        rel = path.relative_to(ctx.repo_root).as_posix()
        if rel not in index_text:
            missing.append({"script": rel})
    stale = []
    for rel in sorted(set(INDEX_SCRIPT_RE.findall(index_text))):
        path = ctx.repo_root / rel
        if path.is_symlink() and not path.resolve().exists():
            stale.append({"script": rel, "reason": "broken_symlink_target"})
        elif not path.exists():
            stale.append({"script": rel, "reason": "index_target_missing"})
    if missing or stale:
        return CheckResult.fail(
            "scripts index",
            f"scripts index has {len(missing)} unregistered and {len(stale)} stale script(s)",
            severity="high",
            details=(missing + stale)[:80],
            remediation="Register each live script and remove or repair stale INDEX/symlink targets.",
        )
    return CheckResult.pass_("scripts index", f"registered {checked} QA/security script(s); all indexed targets exist")
