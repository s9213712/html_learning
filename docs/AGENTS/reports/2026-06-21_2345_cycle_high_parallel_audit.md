# Cycle High-Parallel Audit

Date: 2026-06-21
Target: isolated dev_ready runtime at `https://127.0.0.1:54322`
Runtime: `/tmp/hackme_cycle_audit_20260621_54322/hackme_web/runtime`

## Findings

1. MEDIUM - whole-site mixed stress exceeds current gunicorn/backpressure capacity.
   - Evidence: `/tmp/hackme_cycle_audit_20260621_54322/reports/system_stress_roundB.json`
   - Evidence: `/tmp/hackme_cycle_audit_20260621_54322/reports/system_stress_roundC.json`
   - Round B: 80 logical users, 900 requested ops, concurrency 80, 1081 total measured ops, hard failure rate excluding controlled 503 was 11.19%, ordinary p95 was about 20.0s.
   - Round C: 60 logical users, 700 requested ops, concurrency 48, 957 total measured ops, hard failure rate excluding controlled 503 was 14.94%, ordinary p95 was about 20.0s.
   - The failures were client-side read timeouts spread across low-cost and feature endpoints. Error logs showed no traceback, worker timeout, HTTP 500, or SQLite lock. `/api/version` recovered after load.
   - Interpretation: this is a capacity/backpressure boundary. App-level `server_busy` works for accepted requests, but under these thread-saturating loads some requests wait before the app can return JSON backpressure.

## Passes

- Trading/blockchain weighted full probe passed at high pressure.
  - Artifact: `/tmp/hackme_cycle_audit_20260621_54322/reports/trading_stress_roundA/trading_stress_report_20260621T144019Z.json`
  - Mode `full`, users 25, orders per user 100, concurrency 64, rate 128.
  - Result: `ok=true`, failures 0, total requests 263, server error count 0.
  - Covered spot orders, invalid orders, permission boundaries, manual price dev/test behavior, conservative price gates, margin/short/liquidation, PointsChain verification, concurrent order creation, cancel race, snapshot restore.

- PointsChain real incident attack regression passed.
  - Artifact: `/tmp/hackme_cycle_audit_20260621_54322/reports/pointschain_attack_roundA.json`
  - Result: `p0_pass=true`, `p1_pass=true`, `p2_pass=true`.

- Member behavior probe passed.
  - Artifact: `/tmp/hackme_cycle_audit_20260621_54322/reports/member_probe_roundA.json`
  - Result: findings empty.
  - Covered real uploads/previews/share/E2EE/video/password flow/grid fee math/reserve allocation behavior.

- Log audit found no hidden server exceptions.
  - Scope: `gunicorn_error.log` for both `/tmp/hackme_cycle_audit_20260621_54322` and previous `/tmp/hackme_cycle_audit_20260621_54310`.
  - Search terms included traceback, exception, error, critical, database lock, operational error, status 500, worker timeout.
  - Result: no matching hard server errors; only expected management-plane slow warnings.

## Probe Fixes Made

- Updated `scripts/security/pentest/trading_stress_pentest.py` so the probe matches current product behavior:
  - Handles partial HTTP error reads without crashing.
  - Allows controlled JSON `server_busy` under explicit `--allow-server-busy`.
  - Treats PointsChain manual admin point adjustment deprecation (`blockchain_permission_model`) as expected.
  - Detects dev/test server mode before asserting manual price override behavior.
  - Polls async PointsChain verification when the API returns `202`.
  - Corrected margin test collateral data so it matches current ETH price.

- Fixed the whole-site backpressure capacity boundary found in this audit:
  - `services/server/backpressure.py` now keeps normal/feature/heavy gate limits below gunicorn thread capacity and reserves fast-lane worker budget in auto mode.
  - Overload at 48 and 80 concurrency now returns controlled JSON `server_busy` instead of client read timeouts.
  - Artifacts:
    - `/tmp/hackme_backpressure_fix_20260621_54333/reports/system_stress_48_after.json`
    - `/tmp/hackme_backpressure_fix_20260621_54334/reports/system_stress_80_after_v2.json`
    - `/tmp/hackme_backpressure_fix_20260621_54337/reports/system_stress_80_after_schema_fix.json`

- Fixed a startup/request race exposed by the 80-concurrency rerun:
  - `services/users/profiles.py` now tolerates duplicate-column races when multiple gunicorn workers concurrently ensure `user_profiles` schema on an older DB.
  - Regression: `tests/users/test_user_profile_appearance.py::test_user_profile_schema_tolerates_duplicate_column_race`.
  - Before the fix, `/api/me` produced two HTTP 500 responses with `sqlite3.OperationalError: duplicate column name: display_timezone`.
  - After the fix, the rerun had `hard_failure_rate=0.0`, `/api/me` status `200` for 83/83 requests, and no Traceback/500/worker-timeout log entries.

## Residual Risk

- The original whole-site timeout issue is closed for the tested isolated gunicorn shape (`4 workers x 6 threads`): 48 and 80 concurrency reruns finished with `ok=true`, `degraded=false`, and controlled `server_busy` accounting for overload.
- Under extreme 80-concurrency load, non-critical feature endpoints still return many controlled `server_busy` responses. That is intentional overload protection, not a hidden failure, but production capacity tuning can still raise accepted throughput.
