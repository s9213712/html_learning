#!/usr/bin/env python3
"""RC1.1 operational integrity gate."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_artifacts import test_artifact_path  # noqa: E402


DEFAULT_OUT = test_artifact_path("qa", "pointschain_rc1_1_gate.json")
PYTEST_WRAPPER = ROOT / "scripts" / "testing" / "pytest_in_tmp.sh"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_step(name: str, cmd: list[str], *, timeout: int = 300) -> dict:
    started_at = utc_now()
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(test_artifact_path("pycache"))
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        returncode = proc.returncode
        output = proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        output = f"step timed out after {timeout}s\n{exc.stdout or ''}"
    except OSError as exc:
        returncode = 127
        output = f"step could not start: {exc}"
    return {
        "name": name,
        "ok": returncode == 0,
        "returncode": returncode,
        "started_at": started_at,
        "finished_at": utc_now(),
        "command": cmd,
        "output_tail": output[-6000:],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run RC1.1 operational integrity checks.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--skip-drill", action="store_true", help="Skip isolated snapshot-boundary drill.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    restore_out = test_artifact_path("ops", "restore_drill_rc1_1_gate.json")
    support_manifest_out = test_artifact_path("qa", "rc1_1_gate_support_manifest.json")
    steps = [
        run_step(
            "rc1_1_ops_py_compile",
            [
                sys.executable,
                "-m",
                "py_compile",
                "scripts/ops/export_chain_anchor.py",
                "scripts/ops/rc1_restore_drill.py",
                "scripts/qa/points_chain_rc1_1_gate.py",
            ],
            timeout=120,
        ),
        run_step(
            "rc1_1_operational_tests",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/points/test_rc1_1_operational_integrity.py",
                "tests/snapshots/test_snapshots.py",
            ],
            timeout=240,
        ),
    ]
    if not args.skip_drill:
        steps.append(
            run_step(
                "isolated_snapshot_boundary_drill",
                [
                    sys.executable,
                    "scripts/ops/rc1_restore_drill.py",
                    "--out",
                    str(restore_out),
                ],
                timeout=240,
            )
        )
        steps.append(
            run_step(
                "restore_artifact_secret_scan",
                [
                    sys.executable,
                    "scripts/ops/rc1_1_artifact_manifest.py",
                    "--out",
                    str(support_manifest_out),
                    str(restore_out),
                ],
                timeout=120,
            )
        )
    ok = all(step["ok"] for step in steps)
    payload = {
        "release_candidate": "PointsChain RC1.1 Operational Integrity",
        "generated_at": utc_now(),
        "ok": ok,
        "restore_drill": "pass" if next((s for s in steps if s["name"] == "isolated_snapshot_boundary_drill"), {}).get("ok") else ("skipped" if args.skip_drill else "fail"),
        "snapshot_boundary_drill": "pass" if next((s for s in steps if s["name"] == "isolated_snapshot_boundary_drill"), {}).get("ok") else ("skipped" if args.skip_drill else "fail"),
        "anchor_export": "covered_by_operational_tests",
        "scope_expansion": "blocked",
        "steps": steps,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": ok,
        "out": str(out),
        "restore_drill": payload["restore_drill"],
        "anchor_export": payload["anchor_export"],
    }, ensure_ascii=False, indent=2))
    print(f"RC1.1 OPERATIONAL GATE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
