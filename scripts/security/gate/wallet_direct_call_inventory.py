#!/usr/bin/env python3
"""Inventory direct wallet and ledger write touchpoints.

This scanner is a PointsChain MVP RC1 guardrail. Runtime product code must not
write financial truth directly; it should enter through approved Economy /
PointsChain facades. Findings classified as ``blocker_product_bypass`` fail the
release gate when ``--fail-on-blocker`` is used.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.security.common_paths import REPO_ROOT as ROOT, timestamped_security_report_paths

TARGET_CALLS = {
    "_record_transaction",
    "record_transaction",
    "spend_points",
    "rollback_ledger",
}
BLOCKER_CLASSIFICATIONS = {"blocker_product_bypass"}
OFFICIAL_WALLET_TABLE = "points_wallets"
SHADOW_WALLET_TABLE = "test_shadow_wallets"
MUTATION_TABLES = {OFFICIAL_WALLET_TABLE, SHADOW_WALLET_TABLE}
BALANCE_COLUMNS = {
    "soft_balance",
    "hard_balance",
    "soft_frozen",
    "hard_frozen",
    "balance_points",
    "frozen_points",
    "total_soft_earned",
    "total_hard_earned",
    "total_soft_spent",
    "total_hard_spent",
    "total_points_earned",
    "total_points_spent",
}
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "cache",
    "node_modules",
    "reference_repos",
    "runtime",
    "storage",
    "venv",
}
DEFAULT_SCAN_ROOTS = (
    "routes",
    "services",
    "server.py",
    "scripts",
)


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    kind: str
    symbol: str
    classification: str
    rationale: str
    snippet: str


def relpath(path: Path, repo_root: Path = ROOT) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _line_at(lines: list[str], line_no: int) -> str:
    if line_no <= 0 or line_no > len(lines):
        return ""
    return lines[line_no - 1].strip()[:220]


def _collapse_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _sql_mutation_table(value: str) -> str | None:
    sql = _collapse_sql(value)
    for table in MUTATION_TABLES:
        table_re = re.escape(table.lower())
        if re.search(rf"\bupdate\s+(?:\w+\.)?{table_re}\b", sql):
            return table
        if re.search(rf"\binsert\s+(?:or\s+\w+\s+)?into\s+(?:\w+\.)?{table_re}\b", sql):
            return table
        if re.search(rf"\bdelete\s+from\s+(?:\w+\.)?{table_re}\b", sql):
            return table
    return None


def _touches_balance_columns(value: str) -> bool:
    lowered = value.lower()
    return any(re.search(rf"\b{re.escape(column)}\b", lowered) for column in BALANCE_COLUMNS)


def _classify(rel: str, *, kind: str, symbol: str) -> tuple[str, str]:
    if rel.startswith("tests/"):
        return "test_helper", "test fixture or regression coverage"
    if rel.startswith("scripts/"):
        return "test_helper", "operator or validation script outside runtime product accounting"
    if rel.startswith("services/points_chain/"):
        return "allowed_internal_primitive", "PointsChain core implementation may append replayable ledger events"
    if rel.startswith("services/trading/shadow.py") or rel.startswith("services/snapshots/"):
        return "allowed_internal_primitive", "server-mode or shadow-wallet isolation code, not production wallet truth"
    if kind == "direct_wallet_balance_mutation" and symbol == OFFICIAL_WALLET_TABLE:
        return "blocker_product_bypass", "non-core product code mutates official wallet balance columns directly"
    if kind == "direct_wallet_balance_mutation" and symbol == SHADOW_WALLET_TABLE:
        return "migration_only", "shadow wallet balance mutation must stay outside production accounting truth"
    if symbol == "_record_transaction":
        return "blocker_product_bypass", "product code uses private ledger write API directly"
    if symbol in {"record_transaction", "spend_points", "rollback_ledger"}:
        return "blocker_product_bypass", "product code calls PointsChain service directly instead of an approved facade"
    return "migration_only", "scanner could not infer a safe RC1 classification; review before release"


class WalletInventoryVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, repo_root: Path, source: str):
        self.path = path
        self.repo_root = repo_root
        self.rel = relpath(path, repo_root)
        self.lines = source.splitlines()
        self.findings: list[Finding] = []
        self.call_aliases: dict[str, str] = {}

    def _add(self, *, line: int, kind: str, symbol: str, snippet: str | None = None):
        classification, rationale = _classify(self.rel, kind=kind, symbol=symbol)
        self.findings.append(
            Finding(
                file=self.rel,
                line=int(line or 0),
                kind=kind,
                symbol=symbol,
                classification=classification,
                rationale=rationale,
                snippet=(snippet if snippet is not None else _line_at(self.lines, int(line or 0))),
            )
        )

    def visit_Call(self, node: ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in TARGET_CALLS:
            self._add(line=node.lineno, kind="ledger_service_call", symbol=func.attr)
        elif isinstance(func, ast.Name):
            symbol = self.call_aliases.get(func.id) or (func.id if func.id in TARGET_CALLS else None)
            if symbol:
                self._add(line=node.lineno, kind="ledger_service_call", symbol=symbol)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for alias in node.names:
            if alias.name in TARGET_CALLS:
                self.call_aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str):
            self._maybe_sql(node.value, node.lineno)
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr):
        static_parts = [part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)]
        if static_parts:
            self._maybe_sql(" ".join(static_parts), node.lineno)
        self.generic_visit(node)

    def _maybe_sql(self, value: str, line_no: int):
        table = _sql_mutation_table(value)
        if table and _touches_balance_columns(value):
            self._add(
                line=line_no,
                kind="direct_wallet_balance_mutation",
                symbol=table,
                snippet=_line_at(self.lines, line_no),
            )


def _iter_python_files(repo_root: Path, roots: list[str], include_tests: bool) -> list[Path]:
    candidates: list[Path] = []
    for raw_root in roots:
        root = (repo_root / raw_root).resolve()
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix == ".py":
                candidates.append(root)
            continue
        for path in root.rglob("*.py"):
            rel_parts = set(path.resolve().relative_to(repo_root.resolve()).parts)
            if rel_parts & EXCLUDED_DIRS:
                continue
            if not include_tests and path.resolve().relative_to(repo_root.resolve()).parts[0] == "tests":
                continue
            candidates.append(path)
    return sorted(set(candidates))


def scan_repo(repo_root: Path = ROOT, *, roots: list[str] | None = None, include_tests: bool = False) -> list[Finding]:
    repo_root = repo_root.resolve()
    scan_roots = roots or list(DEFAULT_SCAN_ROOTS)
    if include_tests and "tests" not in scan_roots:
        scan_roots = [*scan_roots, "tests"]
    findings: list[Finding] = []
    for path in _iter_python_files(repo_root, scan_roots, include_tests):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relpath(path, repo_root))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            findings.append(
                Finding(
                    file=relpath(path, repo_root),
                    line=0,
                    kind="scan_error",
                    symbol=type(exc).__name__,
                    classification="unknown",
                    rationale="file could not be parsed by the static scanner",
                    snippet=str(exc),
                )
            )
            continue
        visitor = WalletInventoryVisitor(path, repo_root, source)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return sorted(findings, key=lambda item: (item.file, item.line, item.kind, item.symbol))


def build_payload(repo_root: Path, findings: list[Finding], *, include_tests: bool) -> dict:
    by_classification = Counter(item.classification for item in findings)
    by_symbol = Counter(item.symbol for item in findings)
    by_kind = Counter(item.kind for item in findings)
    blocker_count = sum(count for key, count in by_classification.items() if key in BLOCKER_CLASSIFICATIONS)
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "repo_root": relpath(repo_root, repo_root.parent),
        "include_tests": bool(include_tests),
        "summary": {
            "total": len(findings),
            "blockers": blocker_count,
            "by_classification": dict(sorted(by_classification.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "by_symbol": dict(sorted(by_symbol.items())),
        },
        "findings": [asdict(item) for item in findings],
    }


def render_markdown(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# Wallet Direct Call Inventory",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Include tests: `{payload['include_tests']}`",
        f"- Total findings: `{summary['total']}`",
        f"- Classification counts: `{json.dumps(summary['by_classification'], sort_keys=True)}`",
        f"- Symbol counts: `{json.dumps(summary['by_symbol'], sort_keys=True)}`",
        "",
        "| Classification | Kind | Symbol | File | Line | Rationale |",
        "|---|---|---|---|---:|---|",
    ]
    for finding in payload["findings"]:
        lines.append(
            "| {classification} | {kind} | `{symbol}` | `{file}` | {line} | {rationale} |".format(
                classification=finding["classification"],
                kind=finding["kind"],
                symbol=finding["symbol"],
                file=finding["file"],
                line=finding["line"],
                rationale=str(finding["rationale"]).replace("|", "\\|"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inventory direct PointsChain calls and wallet balance mutations.")
    parser.add_argument("--repo-root", default=str(ROOT), help="Repository root to scan.")
    parser.add_argument("--include-tests", action="store_true", help="Include tests/ in addition to runtime and script paths.")
    parser.add_argument("--root", action="append", dest="roots", help="Relative path root to scan; may be repeated.")
    parser.add_argument("--json-out", default="", help="Write JSON report to this path. Defaults to the external security report root.")
    parser.add_argument("--md-out", default="", help="Write Markdown report to this path. Defaults to the external security report root.")
    parser.add_argument("--fail-on-blocker", action="store_true", help="Exit non-zero if blocker findings exist.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    findings = scan_repo(repo_root, roots=args.roots, include_tests=args.include_tests)
    payload = build_payload(repo_root, findings, include_tests=args.include_tests)

    default_json, default_md = timestamped_security_report_paths("wallet_direct_call_inventory")
    json_out = Path(args.json_out).expanduser() if args.json_out else default_json
    md_out = Path(args.md_out).expanduser() if args.md_out else default_md
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_out.write_text(render_markdown(payload), encoding="utf-8")

    blocker_count = int(payload["summary"].get("blockers") or 0)
    print(
        "wallet direct call inventory: "
        f"{payload['summary']['total']} findings, "
        f"{blocker_count} blocker(s), "
        f"json={json_out}, md={md_out}"
    )
    if args.fail_on_blocker and blocker_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
