# Extreme Trading, PointsChain, and Mixed Feature Pressure Audit

- Date: 2026-06-22 06:07 CST
- Repo: `/home/s92137/hackme_web_05_AI_Agent`
- Isolated target: `https://127.0.0.1:54380`
- Runtime: `/tmp/hackme_extreme_audit_20260622_54380/hackme_web/runtime`
- Main server on port 5000 was not touched.

## Scope

This pass focused on extreme behavior around trading correctness, PointsChain transfers, chain/off-chain flows, background jobs, quota exhaustion, and mixed user traffic across trading, cloud drive, HLS/video, forum/chat, game/catalog, notifications, and ComfyUI/HF-style generation endpoints.

## Result

No confirmed double-spend, negative balance, duplicate request UUID, failed/pending test transfer, PointsChain verification error, quota overrun, traceback, SQLite lock failure, gunicorn worker timeout, or hard 5xx failure was found.

The server does degrade under high mixed concurrency by returning controlled JSON `503 server_busy` responses and by pushing QoS latency over the probe threshold. This is a capacity/backpressure boundary, not a confirmed accounting bug. At 80-160 concurrent mixed users, the server stays coherent but spends more than half of requests in protective backpressure.

## Findings And Observations

1. Extreme mixed traffic reaches controlled degraded mode.
   - 160 concurrency plus destructive chain stress: `server_busy_503_rate=0.571116`, `hard_failure_rate_excluding_503=0.0`, degraded reasons: `ordinary_p95_gt_1500ms`, `qos_version_p95_gt_1000ms`.
   - 80 concurrency standalone baseline: `server_busy_503_rate=0.561963`, `hard_failures_excluding_503=0`, degraded reason: `qos_version_p95_gt_1000ms`.
   - Interpretation: QoS/backpressure protects the process and data integrity, but user-facing throughput collapses under this mixed workload.

2. One initial trading full run reported `direct wallet mutation endpoint rejected` as a CRITICAL failure because the pentest result saw `HTTP 404` with an empty body.
   - Follow-up live reproduction with both `requests` and the pentest `Client` returned JSON `404` with `ok=false`.
   - Wallet balance remained unchanged.
   - Current classification: not a product security bypass and not reproducible as a stable API bug. Most likely an overload-time truncated error-body read during the first full run.

3. Cloud drive quota exhaustion behaved correctly.
   - Test quota override: 1 MiB.
   - Concurrent uploads: 32.
   - Results: 3 success, 7 explicit quota rejects, 22 controlled `server_busy`, 0 hard failures.
   - Final counted usage: 809341 bytes, below the 1048576-byte quota.

4. PointsChain destructive stress passed.
   - Direct transfers completed: 700.
   - Direct transfer errors: 0.
   - External transfers: 80.
   - Transport retries: 5.
   - DB checks: duplicate active wallet address groups `0`, duplicate request UUID groups `0`, test prefix confirmed `821`, failed `0`, pending `0`.
   - Post-stress root verification: `verification_ok=true`, `error_count=0`.

5. Member/full feature probe passed.
   - 17 checks.
   - Findings: none.
   - Covered upload/share/download, attachments, HLS/video seed path, forum/chat, bad input rejection, and member-facing flows.

## Evidence Artifacts

- Trading full pressure: `/tmp/hackme_extreme_audit_20260622_54380/reports/trading_full_round1/trading_stress_report_20260621T160729Z.json`
- PointsChain destructive stress: `/tmp/hackme_extreme_audit_20260622_54380/reports/pointschain_destructive_round2.json`
- Mixed feature pressure, 160 concurrency: `/tmp/hackme_extreme_audit_20260622_54380/reports/system_mixed_during_chain_round1.json`
- Mixed feature baseline, 80 concurrency: `/tmp/hackme_extreme_audit_20260622_54380/reports/system_mixed_baseline80_round2.json`
- Cloud drive quota exhaustion: `/tmp/hackme_extreme_audit_20260622_54380/reports/quota_exhaustion_round1.json`
- Member probe: `/tmp/hackme_extreme_audit_20260622_54380/reports/member_probe_round1.json`

## Log Review

Searched runtime logs for:

- `Traceback`
- `ERROR`
- `CRITICAL`
- `database is locked`
- `OperationalError`
- `Worker timeout`
- `WORKER TIMEOUT`
- `internal_server_error`
- `Exception on /api`

No matches were found in the isolated runtime logs after the stress runs.

## Recommendation

The next engineering work should target capacity and user experience under protective backpressure:

- Tune QoS thresholds and feature gate behavior so public health/version endpoints remain below the p95 target during heavy feature pressure.
- Consider per-feature admission limits so high-cost endpoints such as drive upload, HLS, remote download, and generation do not consume enough worker capacity to make low-cost endpoints slow.
- Add a regression probe that treats JSON `503 server_busy` as controlled, but separately fails if QoS p95 exceeds the chosen SLO after cooldown.
