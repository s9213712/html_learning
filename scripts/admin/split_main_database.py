#!/usr/bin/env python3
"""Analyze or export domain database tables from a legacy main database.

This tool is intentionally non-destructive.  It does not drop or rename tables
from database.db; use it to produce domain DB files and a hash manifest before
runtime routing is switched table by table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.server.domain_databases import (  # noqa: E402
    DOMAIN_TABLES,
    analyze_database,
    export_domain_tables,
)
from services.server.runtime import default_runtime_root_path  # noqa: E402


def _default_db_path() -> Path:
    runtime_root = Path(os.environ.get("HACKME_RUNTIME_DIR") or default_runtime_root_path())
    return runtime_root.expanduser().resolve() / "database" / "database.db"


def _parse_domains(raw: str) -> set[str] | None:
    text = str(raw or "").strip()
    if not text or text.lower() == "all":
        return None
    domains = {part.strip() for part in text.split(",") if part.strip()}
    unknown = sorted(domains - set(DOMAIN_TABLES))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown domain(s): {', '.join(unknown)}")
    return domains


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Split/analyze hackme_web database domains")
    parser.add_argument(
        "--source",
        default=str(_default_db_path()),
        help="Source database.db path. Defaults to $HACKME_RUNTIME_DIR/database/database.db or external XDG state.",
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="Directory for exported domain DB files. Required for --mode export.",
    )
    parser.add_argument(
        "--mode",
        choices=("analyze", "export"),
        default="analyze",
        help="analyze prints the domain table map; export copies domain tables and writes a manifest.",
    )
    parser.add_argument(
        "--domains",
        type=_parse_domains,
        default=None,
        help="Comma-separated domains to export, or all. Available: " + ", ".join(sorted(DOMAIN_TABLES)),
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing exported domain DB files.")
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    return parser


def _print_human_analysis(report: dict) -> None:
    print(f"source: {report['source']}")
    print(f"tables: {report['table_count']}")
    print("domains:")
    for domain, stats in sorted(report["domains"].items()):
        print(f"  - {domain}: tables={stats['tables']} rows={stats['rows']}")
    unclassified = [
        row for row in report["tables"]
        if row["domain"] == "unclassified"
    ]
    if unclassified:
        print("unclassified:")
        for row in unclassified:
            print(f"  - {row['table']} rows={row['rows']}")


def _print_human_export(manifest: dict) -> None:
    print(f"source: {manifest['source']}")
    print(f"out_dir: {manifest['out_dir']}")
    for domain, data in sorted(manifest["domains"].items()):
        if not data.get("path"):
            continue
        print(
            f"  - {domain}: tables={data['table_count']} rows={data['row_count']} "
            f"sha256={data['sha256']} path={data['path']}"
        )
    print(f"manifest: {manifest['manifest_path']}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        parser.error(f"source database not found: {source}")
    if args.mode == "analyze":
        report = analyze_database(source)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            _print_human_analysis(report)
        return 0
    out_dir = Path(args.out_dir or "").expanduser()
    if not args.out_dir:
        parser.error("--out-dir is required for --mode export")
    manifest = export_domain_tables(source, out_dir, domains=args.domains, overwrite=bool(args.overwrite))
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human_export(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
