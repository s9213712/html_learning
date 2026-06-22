# 2026-06-22 Security, Governance, Member Audit

## Findings

### Fixed: PointsChain governance clock guard falsely entered safe mode under host drift

- Severity: High.
- Impact: Governance writes could be blocked by `governance_clock_jump_detected` during WSL/host suspend or high-load clock drift, leaving PointsChain in safe mode and preventing hacker-incident review from creating governance proposals.
- Evidence:
  - `/tmp/hackme_frontend_bg_audit_20260622_54381/reports/custom_front_bg/security_governance_member_probe.json`
  - `/tmp/hackme_security_gov_audit_20260622_54382/reports/security_governance_member_probe_after_fix_v2.json`
  - Safe-mode row showed `wall_elapsed_seconds=858.393`, `monotonic_elapsed_seconds=815.522`, fixed 30s tolerance, and `reason=governance_clock_jump_detected`.
- Fix:
  - `services/points_chain/service.py` now prefers suspend-aware `CLOCK_BOOTTIME` when available.
  - Clock jump tolerance now includes bounded long-running drift tolerance: 10% of elapsed time, capped at 300s, while retaining the 30s minimum.
  - Fast-forward attacks still enter safe mode.

## Coverage

### Security incident and governance probe

- Artifact: `/tmp/hackme_security_gov_audit_20260622_54383/reports/security_governance_member_probe_after_clock_tolerance.json`
- Result: `ok=true`, `failures=0`, `noise_5xx=0`.
- Exercised:
  - root creates mixed test members.
  - user cannot list admin users.
  - manager can list users and adjust normal user level.
  - manager cannot create users, modify root, or change roles.
  - root restores member level.
  - account-bound suspicious transfer fixture.
  - non-owner hacker dispute rejected.
  - victim hacker dispute accepted.
  - duplicate dispute rejected.
  - manager review creates governance proposals.
  - duplicate direct public/risk/freeze proposals are rejected as similar active proposals.
  - vote/sponsor/execute are blocked by governance lifecycle when not eligible/ready.
- DB deltas from passing run:
  - `points_chain_transfer_requests`: +1
  - `points_chain_governance_proposals`: +3
  - `points_chain_governance_audit_log`: +3
  - `points_chain_address_provisional_freezes`: +1

### ComfyUI pressure

- Artifacts:
  - `/tmp/hackme_frontend_bg_audit_20260622_54381/reports/custom_front_bg/comfyui_pressure_probe_8jobs.json`
  - `/tmp/hackme_frontend_bg_audit_20260622_54381/reports/custom_front_bg/comfyui_pressure_probe_16jobs.json`
- Result: no ComfyUI job errors and no background 5xx.
- 16-job pressure run accepted 6 jobs and rejected 10 through expected 409/429/503 backpressure/points guards.

### Trading and background jobs

- Artifact: `/tmp/hackme_frontend_bg_audit_20260622_54381/reports/custom_front_bg/trading_background_probe_margin_ok.json`
- Result: `ok=true`, `failures=0`, `noise_5xx=0`.
- Covered limit order matching, conditional/grid bot scans, margin open, interest accrual scan, liquidation scan, and DB invariants.
- Final counts: orders 23, fills 18, margin positions 1, bot runs 895, background errors 0.

### Capacity stress observation

- `test_for_develop.sh` auto capacity gate was started and manually stopped after completing 1x6, 2x6, and 3x6 profiles because it continued into 4x6.
- Console summaries showed hard failures/server busy under the default predeploy capacity flow:
  - 1x6: 36 server failures, statuses included 35x `503` and 1x `500`.
  - 2x6: 23 server failures, statuses included 22x `503` and 1x `500`.
  - 3x6: 24 server failures, statuses included 24x `503`.
- No final JSON artifact was written because the run was intentionally interrupted before 4x6 completed.

## Verification

- `python3 -m pytest tests/points/test_governance_branch.py::test_governance_clock_fast_forward_enters_safe_mode tests/points/test_governance_branch.py::test_governance_clock_uses_suspend_aware_boottime_when_available tests/points/test_governance_branch.py::test_governance_clock_tolerates_small_long_running_host_drift tests/points/test_governance_branch.py::test_address_signed_dispute_hides_reporter_identity_and_freezes_to_for_one_hour tests/points/test_governance_branch.py::test_account_bound_official_hot_wallet_can_open_and_reply_to_dispute_without_private_key -q`
  - Passed: 5.
- `python3 -m pytest tests/ai_agent/test_ai_agent_routes.py -q`
  - Passed: 34.

## Residual Risk

- The predeploy capacity flow still reports hard 5xx/server-busy samples under broad all-feature pressure. This was not fixed in this pass because the confirmed blocker was governance safe-mode false positives; capacity failure details need a dedicated artifact-producing rerun.
