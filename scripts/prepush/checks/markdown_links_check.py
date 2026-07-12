from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


LINK_RE = re.compile(r'!?\[[^\]]*\]\(([^)]+)\)')
IGNORE_PREFIXES = ("http://", "https://", "mailto:", "app://", "tel:")
EXCLUDED_PREFIXES = (
    "docs/archive/",
    "docs/AGENTS/research/",
    "docs/AGENTS/reports/",
    "docs/games/archive/",
    "docs/games/chess_debug/",
    "docs/games/evidence/",
    "docs/games/experiments/",
    "docs/games/reports/",
)


def _iter_targets(ctx: PrepushContext) -> list[str]:
    targets: list[str] = ["README.md", "SECURITY.md"]
    docs_root = ctx.repo_root / "docs"
    if docs_root.exists():
        targets.extend(
            path.relative_to(ctx.repo_root).as_posix()
            for path in docs_root.rglob("*.md")
        )
    return sorted(set(targets))


def _extract_links(text: str) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for target in LINK_RE.findall(line):
            links.append((line_no, target.strip()))
    return links


def _resolve_link(path: Path, raw_target: str, repo_root: Path) -> Path | None:
    target = raw_target
    if not target or target.startswith(IGNORE_PREFIXES) or target.startswith("#"):
        return None
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = unquote(target)
    target = target.split("#", 1)[0].strip()
    if not target or target.startswith("/"):
        return None
    resolved = (path.parent / target).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        return None
    return resolved


def run(ctx: PrepushContext) -> CheckResult:
    findings: list[dict[str, object]] = []
    for rel in _iter_targets(ctx):
        if rel.startswith(EXCLUDED_PREFIXES):
            continue
        path = ctx.repo_root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, raw_target in _extract_links(text):
            resolved = _resolve_link(path, raw_target, ctx.repo_root)
            if resolved is None:
                continue
            if not resolved.exists():
                findings.append(
                    {
                        "file": rel,
                        "line": line_no,
                        "link": raw_target,
                    }
                )
    if findings:
        return CheckResult.fail(
            "markdown links",
            "broken repo-local markdown links found",
            severity="medium",
            details=findings[:80],
            remediation="Fix the relative link targets or move deep references to the canonical docs index.",
        )
    return CheckResult.pass_("markdown links", "repo-local markdown links resolved")
