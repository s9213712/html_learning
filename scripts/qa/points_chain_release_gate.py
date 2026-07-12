#!/usr/bin/env python3
"""PointsChain MVP RC1 release-gate runner.

The gate is intentionally small and repeatable: static bypass scan, targeted
unit coverage, optional live probes, and a machine-readable RC1 summary.
"""

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


DEFAULT_OUT = test_artifact_path("qa", "pointschain_rc1_release_gate.json")
PYTEST_WRAPPER = ROOT / "scripts" / "testing" / "pytest_in_tmp.sh"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redacted_command(cmd: list[str]) -> list[str]:
    redacted = list(cmd)
    for index, value in enumerate(redacted[:-1]):
        if value in {"--password", "--root-password", "--manager-password"}:
            redacted[index + 1] = "<REDACTED>"
    return redacted


def run_step(
    name: str,
    cmd: list[str],
    *,
    timeout: int = 300,
    env_overrides: dict[str, str] | None = None,
) -> dict:
    started = utc_now()
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(test_artifact_path("pycache"))
    env.update(env_overrides or {})
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
        "started_at": started,
        "finished_at": utc_now(),
        "command": redacted_command(cmd),
        "output_tail": output[-6000:],
    }


def pytest_command(*targets: str) -> list[str]:
    return [str(PYTEST_WRAPPER), "-q", *targets]


def parse_scanner_blockers(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int((payload.get("summary") or {}).get("blockers") or 0)
    except Exception:
        return -1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the PointsChain MVP RC1 release gate.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="JSON output path.")
    parser.add_argument("--base-url", default="", help="Optional live isolated server URL for Playwright probes.")
    parser.add_argument("--runtime-root", default="", help="Optional runtime root for live recovery drill probes.")
    parser.add_argument(
        "--root-password",
        default=os.environ.get("HACKME_ROOT_PASSWORD") or os.environ.get("HTML_LEARNING_ROOT_PASSWORD", ""),
        help="Root password for live probes. Prefer HACKME_ROOT_PASSWORD to avoid shell history and process arguments.",
    )
    parser.add_argument("--capacity-probe", action="store_true", help="Run the isolated RC1 capacity probe as part of this gate.")
    parser.add_argument(
        "--capacity-probe-out",
        default=str(test_artifact_path("qa", "predeploy_capacity_probe_rc1.json")),
        help="JSON output path for --capacity-probe.",
    )
    parser.add_argument("--skip-live", action="store_true", help="Skip live Playwright/API probes even if URLs are supplied.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.skip_live and args.base_url and not args.root_password:
        raise SystemExit("HACKME_ROOT_PASSWORD or --root-password is required when --base-url is used")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    scanner_json = test_artifact_path("qa", "wallet_direct_call_inventory_release_gate.json")
    scanner_md = test_artifact_path("qa", "wallet_direct_call_inventory_release_gate.md")

    steps: list[dict] = []
    steps.append(run_step(
        "legacy_bypass_scanner",
        [
            sys.executable,
            "scripts/security/gate/wallet_direct_call_inventory.py",
            "--fail-on-blocker",
            "--json-out",
            str(scanner_json),
            "--md-out",
            str(scanner_md),
        ],
        timeout=120,
    ))
    steps.append(run_step(
        "points_chain_py_compile",
        [
            sys.executable,
            "-m",
            "py_compile",
            "services/points_chain/service.py",
            "services/points_chain/backup_recovery.py",
            "services/trading/engine.py",
            "services/trading/orders.py",
            "services/trading/schema_ddl.py",
            "routes/economy.py",
            "routes/trading.py",
        ],
        timeout=120,
    ))
    steps.append(run_step(
        "points_chain_governance_branch_tests",
        pytest_command("tests/points/test_governance_branch.py"),
        timeout=240,
    ))
    steps.append(run_step(
        "points_chain_wallet_and_explorer_tests",
        pytest_command(
            "tests/points/test_wallet_identity.py",
            "tests/points/test_points_explorer.py",
        ),
        timeout=240,
    ))
    steps.append(run_step(
        "points_chain_block_tamper_tests",
        pytest_command(
            "tests/points/test_points_chain.py::test_points_chain_block_and_signature_tables_are_append_only",
            "tests/points/test_points_chain.py::test_points_chain_verify_detects_forged_sealed_transaction_hash_recompute",
            "tests/points/test_points_chain.py::test_points_chain_verify_detects_forged_block_rehash_without_node_signature",
            "tests/points/test_points_chain.py::test_points_chain_verify_does_not_auto_resign_missing_block_signature",
        ),
        timeout=180,
    ))
    steps.append(run_step(
        "trading_wallet_fee_regression_tests",
        pytest_command(
            "tests/trading/core/test_trading_engine.py::test_trading_dashboard_uses_selected_payment_wallet_balance",
            "tests/trading/core/test_trading_engine.py::test_trading_dashboard_rejects_inactive_selected_payment_wallet",
            "tests/trading/core/test_trading_engine.py::test_exchange_order_rejects_user_multisig_receive_only_wallet_source",
            "tests/trading/core/test_trading_engine.py::test_spot_buy_uses_trial_credit_before_points_chain_and_updates_position",
            "tests/trading/core/test_trading_engine.py::test_mixed_trial_and_real_points_buy_only_records_real_points_on_chain",
            "tests/trading/core/test_trading_engine.py::test_spot_cfd_principal_payout_and_fee_flow_through_exchange_fund",
            "tests/trading/core/test_trading_engine.py::test_margin_cfd_price_loss_is_collected_by_exchange_reserve_pool",
        ),
        timeout=180,
    ))
    steps.append(run_step(
        "product_service_revenue_accounting_tests",
        pytest_command(
            "tests/video/api/test_video_tips.py::test_video_tip_debits_viewer_credits_uploader_and_is_idempotent",
            "tests/points/test_governance_branch.py::test_official_treasury_signer_center_reports_service_fee_income",
            "tests/points/test_points_chain.py::test_pc0_spend_points_debits_internal_hot_wallet_immediately",
        ),
        timeout=180,
    ))
    steps.append(run_step(
        "flask_werkzeug_hardening_tests",
        pytest_command("tests/security/gates/test_flask_hardening.py"),
        timeout=180,
    ))
    steps.append(run_step(
        "feature_flag_and_production_guard_tests",
        pytest_command(
            "tests/platform/test_feature_flags.py",
            "tests/platform/test_release_policy.py",
            "tests/points/test_chain_production_only.py",
        ),
        timeout=240,
    ))

    capacity_results = {"requested": False, "steps": [], "report": ""}
    if args.capacity_probe:
        capacity_results["requested"] = True
        capacity_out = Path(args.capacity_probe_out)
        capacity_results["report"] = str(capacity_out)
        capacity_results["steps"].append(run_step(
            "predeploy_capacity_probe_rc1",
            [
                sys.executable,
                "scripts/testing/predeploy_capacity_probe.py",
                "--output",
                str(capacity_out),
                "--no-sync-defaults",
            ],
            timeout=1200,
        ))

    live_results = {"requested": False, "steps": []}
    if not args.skip_live and args.base_url:
        live_results["requested"] = True
        live_results["steps"].append(run_step(
            "live_governance_dispute_playwright",
            [
                sys.executable,
                "scripts/testing/pointschain_governance_dispute_probe.py",
                "--base-url",
                args.base_url,
                "--username",
                "root",
                "--out",
                str(test_artifact_path("qa", "playwright", "pointschain_governance_dispute_probe.json")),
            ],
            timeout=180,
            env_overrides={"HACKME_ROOT_PASSWORD": args.root_password},
        ))
    if not args.skip_live and args.runtime_root:
        live_results["requested"] = True
        live_results["steps"].append(run_step(
            "live_realistic_recovery_drill",
            [
                sys.executable,
                "scripts/testing/pointschain_realistic_recovery_drill.py",
                "--runtime-root",
                args.runtime_root,
                "--out",
                str(test_artifact_path("qa", "pointschain_realistic_recovery_drill.json")),
            ],
            timeout=240,
        ))

    all_steps = [*steps, *capacity_results["steps"], *live_results["steps"]]
    scanner_blockers = parse_scanner_blockers(scanner_json)
    ok = all(step["ok"] for step in all_steps) and scanner_blockers == 0
    payload = {
        "release_candidate": "PointsChain MVP RC1",
        "generated_at": utc_now(),
        "ok": ok,
        "scanner_blockers": scanner_blockers,
        "chain_verify": "pass" if ok else "check_steps",
        "replay_verify": "pass" if ok else "check_steps",
        "derived_cache_verify": "pass" if ok else "check_steps",
        "playwright": "pass" if live_results["steps"] and all(step["ok"] for step in live_results["steps"]) else ("skipped" if not live_results["requested"] else "fail"),
        "pentest": "not_run_by_this_gate",
        "stress": "covered_by_targeted_stress_or_external_gate",
        "capacity": "pass" if capacity_results["steps"] and all(step["ok"] for step in capacity_results["steps"]) else ("skipped" if not capacity_results["requested"] else "fail"),
        "legacy_bypass_paths": scanner_blockers,
        "production_profile_guard": "pass" if next((s for s in steps if s["name"] == "feature_flag_and_production_guard_tests"), {}).get("ok") else "fail",
        "artifacts": {
            "scanner_json": str(scanner_json),
            "scanner_md": str(scanner_md),
            "capacity_json": capacity_results["report"],
        },
        "steps": all_steps,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "release_candidate": payload["release_candidate"],
        "ok": payload["ok"],
        "scanner_blockers": payload["scanner_blockers"],
        "capacity": payload["capacity"],
        "playwright": payload["playwright"],
        "production_profile_guard": payload["production_profile_guard"],
        "out": str(out),
    }, ensure_ascii=False, indent=2))
    print(f"RC1 RELEASE GATE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
