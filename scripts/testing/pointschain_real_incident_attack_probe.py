#!/usr/bin/env python3
"""Run targeted PointsChain real-incident attack regressions.

This probe intentionally maps each step to a real incident class rather than a
generic test file. It can be run inside an isolated runtime checkout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_artifacts import test_artifact_path  # noqa: E402


DEFAULT_OUT = test_artifact_path("qa", "pointschain_real_incident_attack_probe.json")
PYTEST_WRAPPER = ROOT / "scripts" / "testing" / "pytest_in_tmp.sh"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_step(name: str, priority: str, incident: str, command: list[str], *, timeout: int = 240) -> dict:
    started_at = utc_now()
    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
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
        "priority": priority,
        "incident": incident,
        "ok": returncode == 0,
        "returncode": returncode,
        "command": command,
        "started_at": started_at,
        "finished_at": utc_now(),
        "output_tail": output[-5000:],
    }


def run_health(base_url: str) -> dict:
    if not base_url:
        return {"name": "live_health", "requested": False, "ok": True}
    # Prefer the IPv4 loopback for isolated QA servers; some sandboxes resolve
    # localhost to IPv6 first while the dev server is bound to 127.0.0.1.
    health_url = f"{base_url.rstrip('/')}/api/version"
    health_url = health_url.replace("://localhost:", "://127.0.0.1:")
    command = ["curl", "-k", "-sS", "--retry", "2", "--retry-delay", "1", health_url]
    started_at = utc_now()
    proc = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    return {
        "name": "live_health",
        "requested": True,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": command,
        "started_at": started_at,
        "finished_at": utc_now(),
        "output_tail": (proc.stdout or "")[-2000:],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run PointsChain P0/P1/P2 real-incident attack probe.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--base-url", default="", help="Optional isolated live server URL for health evidence.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    steps = [
        run_step(
            "p0_multisig_masked_payload",
            "P0",
            "Bybit/Safe-style signer UI payload masking",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/points/test_governance_branch.py::test_official_treasury_signer_center_exposes_offline_payload_verifier",
                "tests/points/test_governance_branch.py::test_governance_execute_rejects_payload_tamper_and_active_timelock",
            ],
        ),
        run_step(
            "p0_governance_capture",
            "P0",
            "Beanstalk-style fast governance capture",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/points/test_governance_branch.py::test_public_governance_root_has_no_veto_and_user_proposal_requires_sponsor",
                "tests/points/test_governance_branch.py::test_public_governance_proposal_requires_trusted_member_level",
                "tests/points/test_governance_branch.py::test_public_governance_spam_duplicate_and_rollback_entry_are_blocked",
                "tests/points/test_governance_branch.py::test_multisig_and_voter_snapshot_ignore_new_manager_after_proposal_creation",
            ],
        ),
        run_step(
            "p0_migration_branch_supply",
            "P0",
            "Genesis/migration/fork replay and fund zeroing without ledger backup restore",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/points/test_governance_branch.py::test_official_funds_are_branch_scoped_after_recovery_branch",
                "tests/points/test_governance_branch.py::test_multiple_recovery_forks_preserve_canonical_ledger_and_do_not_zero_funds",
            ],
        ),
        run_step(
            "p0_oracle_price_manipulation",
            "P0",
            "Mango-style oracle/price manipulation",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/trading/core/test_trading_engine.py::test_test_live_price_provider_is_marked_synthetic_and_not_risk_grade_usable",
                "tests/trading/core/test_trading_engine.py::test_manual_root_price_is_not_risk_grade_usable",
                "tests/trading/core/test_trading_engine.py::test_price_fusion_status_excludes_midpoint_outlier_and_one_sided_depth",
                "tests/trading/core/test_trading_engine.py::test_price_jump_requires_explicit_confirmation",
            ],
        ),
        run_step(
            "p0_exchange_liability_visibility",
            "P0",
            "Mt. Gox/FTX-style asset and liability opacity",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/trading/core/test_trading_engine.py::test_spot_cfd_principal_payout_and_fee_flow_through_exchange_fund",
                "tests/trading/core/test_trading_engine.py::test_margin_cfd_price_loss_is_collected_by_exchange_reserve_pool",
                "tests/trading/core/test_trading_engine.py::test_official_exchange_fund_transfer_syncs_trading_reserve_pool",
                "tests/trading/core/test_trading_engine.py::test_reserve_pool_tampering_enters_trading_safe_mode",
            ],
        ),
        run_step(
            "p1_external_reserve_scope",
            "P1",
            "USDC/SVB and Terra-style external reserve/depeg risk",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/platform/test_feature_flags.py::test_external_chain_features_remain_disabled_by_rc1_scope",
            ],
        ),
        run_step(
            "p1_phishing_and_frontend_signing",
            "P1",
            "Curve DNS / fake frontend / blind signing",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/frontend/trading/test_frontend_economy.py::test_root_points_page_is_chain_operations_console",
                "tests/points/test_governance_branch.py::test_cold_wallet_signatures_are_branch_action_and_signer_bound",
            ],
        ),
        run_step(
            "p1_tx_malleability_idempotency",
            "P1",
            "transaction malleability / idempotency mismatch",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/points/test_points_explorer.py::test_points_explorer_acceleration_is_append_only_and_idempotent",
                "tests/points/test_points_explorer.py::test_wallet_transfer_pending_does_not_credit_chain_address_until_proved",
                "tests/points/test_governance_branch.py::test_recovery_branch_replays_parent_without_stolen_tx_and_isolates_old_assets",
            ],
        ),
        run_step(
            "p1_mev_fee_priority_abuse",
            "P1",
            "MEV / front-running / priority-fee abuse",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/points/test_governance_branch.py::test_acceleration_cannot_bypass_pending_freeze_or_cross_branch",
                "tests/trading/core/test_trading_engine.py::test_limit_order_matcher_executes_when_price_reaches_limit",
            ],
        ),
        run_step(
            "p1_clock_deadline_tamper",
            "P1",
            "server-time / timelock deadline tampering",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/points/test_governance_branch.py::test_governance_deadline_guard_rejects_timelock_row_tamper",
                "tests/points/test_governance_branch.py::test_governance_clock_fast_forward_enters_safe_mode",
            ],
        ),
        run_step(
            "p2_dust_privacy_leak",
            "P2",
            "dust attack / address identity inference",
            [
                str(PYTEST_WRAPPER),
                "-q",
                "tests/points/test_governance_branch.py::test_account_bound_official_hot_wallet_can_open_and_reply_to_dispute_without_private_key",
                "tests/points/test_governance_branch.py::test_address_signed_dispute_hides_reporter_identity_and_freezes_to_for_one_hour",
                "tests/frontend/test_points_chain_dispute_frontend.py::test_admin_user_list_shows_official_hot_wallet_for_manager_only",
            ],
        ),
    ]
    live_health = run_health(args.base_url)
    ok = all(step["ok"] for step in steps) and live_health["ok"]
    payload = {
        "probe": "pointschain_real_incident_attack_probe",
        "generated_at": utc_now(),
        "ok": ok,
        "base_url": args.base_url,
        "live_health": live_health,
        "steps": steps,
        "summary": {
            "p0_pass": all(step["ok"] for step in steps if step["priority"] == "P0"),
            "p1_pass": all(step["ok"] for step in steps if step["priority"] == "P1"),
            "p2_pass": all(step["ok"] for step in steps if step["priority"] == "P2"),
        },
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": ok, "out": str(out), **payload["summary"]}, ensure_ascii=False, indent=2))
    print(f"POINTSCHAIN REAL INCIDENT ATTACK PROBE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
