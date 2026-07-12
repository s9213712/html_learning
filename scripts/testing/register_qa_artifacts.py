#!/usr/bin/env python3
"""Register a QA run and archive its report artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.system.release_artifacts import register_qa_run  # noqa: E402
from scripts.test_artifacts import test_artifact_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Register hackme_web QA artifacts outside the source checkout.")
    parser.add_argument("--suite", required=True, help="QA suite name, for example playwright_deep_site_check.")
    parser.add_argument("--status", required=True, choices=["pass", "fail", "unknown"], help="QA result status.")
    parser.add_argument("--artifact", action="append", default=[], help="File or directory to archive. Can be repeated.")
    parser.add_argument("--command", default="", help="Command that produced the artifacts.")
    parser.add_argument("--run-id", default="", help="Stable run id. Defaults to timestamp plus suite.")
    parser.add_argument("--summary-json", default="", help="Small JSON object with extra counters or notes.")
    parser.add_argument("--reports-dir", default=str(test_artifact_path("reports")))
    args = parser.parse_args()

    summary = {}
    if args.summary_json:
        payload = json.loads(args.summary_json)
        if not isinstance(payload, dict):
            raise SystemExit("--summary-json must be a JSON object")
        summary = payload
    result = register_qa_run(
        base_dir=ROOT,
        reports_dir=args.reports_dir,
        git_repo_dir=ROOT,
        suite=args.suite,
        status=args.status,
        artifact_paths=args.artifact,
        command=args.command,
        summary=summary,
        run_id=args.run_id or None,
    )
    print(json.dumps({
        "ok": result.get("ok"),
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "artifact_count": result.get("artifact_count"),
        "manifest_path": result.get("manifest_path"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
