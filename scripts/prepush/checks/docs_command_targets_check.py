from __future__ import annotations

import ast
import re
from pathlib import Path

from scripts.prepush.context import PrepushContext
from scripts.prepush.result import CheckResult


COMMAND_TARGET_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"((?:scripts/[A-Za-z0-9_./-]+\.(?:py|sh))|"
    r"(?:(?:\./)?(?:server\.py|test_for_develop\.sh|hooks/[A-Za-z0-9_./-]+\.sh)))"
)
ROOT_GUIDES = {
    "README.md",
    "SECURITY.md",
    "deploy/README.md",
}
EXCLUDED_DIRECTORY_NAMES = {"archive", "chess_debug", "evidence", "experiments", "reports", "research", "skills"}
PROJECT_IMPORT_ROOTS = {"routes", "scripts", "security", "services"}


def canonical_docs(repo_root: Path) -> list[Path]:
    paths = [repo_root / rel for rel in ROOT_GUIDES]
    docs_root = repo_root / "docs"
    if docs_root.exists():
        paths.extend(
            path
            for path in sorted(docs_root.rglob("*.md"))
            if not EXCLUDED_DIRECTORY_NAMES.intersection(path.relative_to(docs_root).parts)
        )
    return sorted({path for path in paths if path.is_file()})


def _fenced_script_targets(text: str) -> list[tuple[int, str]]:
    targets = []
    in_fence = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        for target in COMMAND_TARGET_RE.findall(line):
            targets.append((line_number, target.removeprefix("./")))
    return targets


def _python_entrypoint_bootstrap_issue(path: Path) -> str:
    if path.suffix != ".py" or "scripts" not in path.parts:
        return ""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return "python_entrypoint_syntax_error"
    project_import_lines: list[int] = []
    bootstrap_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            root = str(node.module or "").split(".", 1)[0]
            if root in PROJECT_IMPORT_ROOTS:
                project_import_lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(alias.name.split(".", 1)[0] in PROJECT_IMPORT_ROOTS for alias in node.names):
                project_import_lines.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in {"insert", "append"}:
                continue
            owner = node.func.value
            is_sys_path = (
                isinstance(owner, ast.Attribute)
                and owner.attr == "path"
                and (
                    isinstance(owner.value, ast.Name)
                    and owner.value.id == "sys"
                    or isinstance(owner.value, ast.Attribute)
                    and owner.value.attr == "sys"
                    and isinstance(owner.value.value, ast.Name)
                    and owner.value.value.id == "os"
                )
            )
            if is_sys_path:
                bootstrap_lines.append(node.lineno)
    if not project_import_lines:
        return ""
    if not bootstrap_lines or min(bootstrap_lines) >= min(project_import_lines):
        return "project_import_before_repo_bootstrap"
    return ""


def run(ctx: PrepushContext) -> CheckResult:
    findings = []
    checked_commands = 0
    entrypoint_issues: dict[Path, str] = {}
    docs = canonical_docs(ctx.repo_root)
    for path in docs:
        rel = ctx.relpath(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, target in _fenced_script_targets(text):
            checked_commands += 1
            resolved = ctx.repo_root / target
            if resolved.is_symlink() and not resolved.resolve().exists():
                reason = "broken_symlink_target"
            elif not resolved.is_file():
                reason = "command_target_missing"
            else:
                if resolved not in entrypoint_issues:
                    entrypoint_issues[resolved] = _python_entrypoint_bootstrap_issue(resolved)
                reason = entrypoint_issues[resolved]
                if reason:
                    findings.append({"file": rel, "line": line_number, "target": target, "reason": reason})
                continue
            findings.append({"file": rel, "line": line_number, "target": target, "reason": reason})
    if findings:
        return CheckResult.fail(
            "docs command targets",
            "canonical operator commands are missing or not independently executable",
            severity="high",
            details=findings[:80],
            remediation=(
                "Update missing commands, or add repo-root import bootstrap before project imports; "
                "move historical/future material out of canonical operator docs."
            ),
        )
    return CheckResult.pass_(
        "docs command targets",
        f"resolved {checked_commands} fenced script command(s) across {len(docs)} canonical doc(s)",
    )
