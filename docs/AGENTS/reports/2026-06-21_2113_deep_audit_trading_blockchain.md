# Deep Audit: Trading / PointsChain Weighted

Date: 2026-06-21 21:13 Asia/Taipei
Repo: `/home/s92137/hackme_web_05_AI_Agent`

## Scope

Weighted focus was on trading, background trading jobs, price-health enforcement, and PointsChain / blockchain settlement invariants. The pass also included multi-account smoke/stress probes, root management actions, and static frontend contract tests around the split trading/economy bundles.

## Confirmed Findings And Fixes

### Fixed: degraded fused price pause policy could be bypassed

Severity: High

The trading price guard returned early when `high_risk_blocked` was false or when dev/test confidence overrides were enabled. That meant explicit root pause policies such as `trading.price_degrade_pause_market_orders=true` could be bypassed for conservative/degraded fused prices.

Fix:
- `services/trading/engine_market_methods.py`
- Pause policies are now evaluated before the confidence-gate override path.
- Market orders, bot creation, and borrowing/margin paths map to their own `trading.price_degrade_pause_*` settings.
- Blocking emits `TRADING_PRICE_HEALTH_BLOCKED` with the active pause policy in metadata.

Regression coverage:
- `test_market_order_pauses_conservative_fusion_price_when_root_policy_enabled`
- `test_grid_bot_create_pauses_conservative_fusion_price_when_root_policy_enabled`

### Fixed: trading background status could fail with `database is locked`

Severity: High

The background status path repeatedly ran schema DDL/seed work and then performed reads while the background worker could be writing. In concurrent worker/status polling this produced `sqlite3.OperationalError: database is locked`.

Fix:
- `services/trading/background_engine.py`
- Background schema initialization is now per-DB cached and guarded by a process lock.
- Successful initialization commits before marking the DB ready.
- Background status reads now retry retryable SQLite lock/schema-change errors.

Regression coverage:
- `test_background_worker_thread_runs_without_any_login_session`
- `test_root_sitewide_refresh_rebuilds_snapshot_before_read`

### Fixed: stale static contract tests after frontend bundle split

Severity: Test/QA correctness

Trading and economy static contract tests still read only `56-trading.js` or `55-economy.js`, while the relevant code now lives in split bundles.

Fix:
- Trading workflow/grid tests now include `56-trading-bots.js`.
- PointsChain explorer contract test now includes `55-economy-explorer.js`.
- BTC trade test now asserts the actual repo root path instead of a hard-coded `hackme_web` checkout name.

## Verification

Passed:
- `python3 -m py_compile services/trading/background_engine.py services/trading/engine_market_methods.py`
- Targeted price policy tests in `tests/trading/core/test_trading_engine.py`
- Targeted lock regression tests:
  - `tests/trading/core/test_trading_background_engine.py::test_background_worker_thread_runs_without_any_login_session`
  - `tests/trading/core/test_trading_root_sitewide_api.py::test_root_sitewide_refresh_rebuilds_snapshot_before_read`
- Modified trading/points subset passed.
- `tests/frontend` passed before this trading-focused pass after the ComfyUI/front-end fixes.

Live probes:
- New isolated server: `https://127.0.0.1:54290`
- Runtime: `/tmp/hackme_deep_audit_20260621_54290/hackme_web/runtime`
- PointsChain attack probe: PASS
  - Artifact: `/tmp/hackme_deep_audit_20260621_54290/reports/pointschain_attack_escalated.json`
- Trading stress/pentest reached market orders, limit orders, grid preview, grid bot create/scan/delete, workflow bot create/scan/delete, root bot audit, and background snapshot refresh with no server 500s.
  - Artifact: `/tmp/hackme_deep_audit_20260621_54290/reports/trading_stress_escalated/trading_stress_report_20260621T131106Z.json`

## Residual Findings

### Probe drift: manual points funding expected success

The trading stress probe still expects `/api/admin/points/adjust` to succeed. The app now returns HTTP 410 `blockchain_permission_model`, which matches the newer PointsChain governance-only funding model. This should be updated in the probe rather than treated as product failure.

### Client-side IncompleteRead during live trading stress

The trading stress probe ended with `IncompleteRead(0 bytes read, 52 more expected)`. Server logs did not show 500s or tracebacks, and access logs show the preceding requests returning expected 200/202/400/409/410 statuses. This is still worth tracking as probe/network robustness or response-close behavior.

### Performance degradation under stress

Earlier system stress on the 54287 isolated server reported `degraded=true` because ordinary p95 exceeded 1500 ms. Trading dashboard p95 was about 2540 ms. This is performance degradation, not a functional correctness failure.

### Frontend long probes timed out

Two long Playwright-style probes produced no output for multiple 30-second polls and were terminated:
- `playwright_trading_background_correctness.py`
- `pointschain_real_incident_frontend_probe.py`

The backend/security probes above cover the critical trading and PointsChain invariants, but the browser probes need timeout instrumentation before being useful in this loop.

## Cleanup

The isolated 54290 server and long-running probe processes were stopped after verification.
