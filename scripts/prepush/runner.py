from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if __name__ == "__main__":
    sys.dont_write_bytecode = True

from scripts.prepush.checks import (
    api_contract_check,
    ci_safety_check,
    cleanup_check,
    config_safety_check,
    docs_command_targets_check,
    forbidden_paths_check,
    frontend_check,
    git_clean_check,
    local_path_check,
    log_hash_chain_check,
    markdown_links_check,
    pii_check,
    points_chain_check,
    pytest_quick_check,
    release_check,
    scripts_index_check,
    secrets_check,
    server_mode_check,
    smoke_server_check,
    snapshot_restore_check,
    syntax_check,
)
from scripts.prepush.context import PrepushContext
from scripts.prepush.result import FAIL, PASS, SKIP, WARN, CheckResult
from scripts.prepush.utils import sanitize_path


Check = Callable[[PrepushContext], CheckResult]


QUICK_CHECKS: list[Check] = [
    syntax_check.run,
    release_check.run,
    forbidden_paths_check.run,
    local_path_check.run,
    markdown_links_check.run,
    docs_command_targets_check.run,
    scripts_index_check.run,
    secrets_check.run,
    pii_check.run,
    config_safety_check.run,
    ci_safety_check.run,
    frontend_check.run,
    git_clean_check.run,
    pytest_quick_check.run,
]

FULL_CHECKS: list[Check] = QUICK_CHECKS + [
    smoke_server_check.run,
    api_contract_check.run,
    server_mode_check.run,
    snapshot_restore_check.run,
    points_chain_check.run,
    log_hash_chain_check.run,
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Production-grade pre-push v2 gate.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="Run the fast pre-push checks. This is the default.")
    mode.add_argument("--full", action="store_true", help="Run quick checks plus heavier isolated gates.")
    parser.add_argument("--ci", action="store_true", help="Run in non-interactive CI mode.")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result.")
    parser.add_argument("--clean", action="store_true", help="Clean safe repo cache artifacts only.")
    parser.add_argument("--clean-temp", action="store_true", help="Clean old /tmp pre-push runtime artifacts.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation for cleaning modes.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep this run's isolated temp runtime even after CI success.")
    parser.add_argument("--keep-temp-latest", type=int, default=2, help="Number of old temp roots to keep when --clean-temp is used.")
    return parser


def collect_checks(ctx: PrepushContext) -> list[Check]:
    if ctx.mode == "full":
        return FULL_CHECKS
    return QUICK_CHECKS


def run_check(check: Check, ctx: PrepushContext) -> CheckResult:
    started = time.perf_counter()
    try:
        result = check(ctx)
    except Exception as exc:  # noqa: BLE001
        result = CheckResult.fail(
            getattr(check, "__name__", "internal check"),
            f"internal error: {sanitize_path(str(exc))}",
            severity="critical",
            details=[{"trace": sanitize_path(traceback.format_exc(limit=4))}],
            remediation="Fix the pre-push check implementation before relying on this gate.",
        )
    result.elapsed_seconds = round(time.perf_counter() - started, 3)
    return result


def render_text(results: list[CheckResult]) -> None:
    for result in results:
        print(f"[{result.status}] {result.name} ({result.elapsed_seconds:.3f}s): {result.message}")
        for detail in result.details[:8]:
            fragments = ", ".join(f"{key}: {sanitize_path(str(value))}" for key, value in detail.items())
            print(f"  {fragments}")
        if result.remediation and result.status in {FAIL, WARN}:
            print(f"  fix: {result.remediation}")
    counts = Counter(result.status.lower() for result in results)
    print("")
    print("Pre-push v2 summary:")
    print(f"- PASS: {counts.get(PASS.lower(), 0)}")
    print(f"- WARN: {counts.get(WARN.lower(), 0)}")
    print(f"- FAIL: {counts.get(FAIL.lower(), 0)}")
    print(f"- SKIP: {counts.get(SKIP.lower(), 0)}")


def to_payload(results: list[CheckResult]) -> dict[str, object]:
    counts = Counter(result.status.lower() for result in results)
    status = "FAIL" if counts.get(FAIL.lower(), 0) else "PASS"
    return {
        "status": status,
        "summary": {
            "pass": counts.get(PASS.lower(), 0),
            "warn": counts.get(WARN.lower(), 0),
            "fail": counts.get(FAIL.lower(), 0),
            "skip": counts.get(SKIP.lower(), 0),
        },
        "results": [result.to_json() for result in results],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = "full" if args.full else "quick"
    ctx = PrepushContext.build(
        repo_root=ROOT,
        mode=mode,
        is_ci=args.ci,
        json_output=args.json,
        yes=args.yes,
        keep_temp=args.keep_temp,
        clean=args.clean,
        clean_temp=args.clean_temp,
    )

    results: list[CheckResult] = []
    if args.clean:
        results.append(cleanup_check.run_clean(ctx))
    if args.clean_temp:
        results.append(cleanup_check.run_clean_temp(ctx, keep_latest=args.keep_temp_latest))
    if args.clean or args.clean_temp:
        # Cleaning is an explicit mode, but still allow checks if requested by
        # passing --full/--quick without relying on cleanup side effects.
        if not args.full and not args.quick and not args.ci:
            payload = to_payload(results)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                render_text(results)
            return 1 if payload["status"] == "FAIL" else 0

    for check in collect_checks(ctx):
        if not args.json:
            print(f"[RUN] {check.__module__.rsplit('.', 1)[-1]}", flush=True)
        result = run_check(check, ctx)
        results.append(result)
        if not args.json:
            print(
                f"[{result.status}] {result.name} ({result.elapsed_seconds:.3f}s): {result.message}",
                flush=True,
            )

    payload = to_payload(results)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        render_text(results)
    return 1 if payload["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
