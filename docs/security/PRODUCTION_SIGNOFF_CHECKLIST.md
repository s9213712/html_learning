# Production Sign-off Checklist

This checklist is a release gate. Any failed item blocks production mode.

## Serving Topology

- [ ] Public traffic enters through Nginx or an equivalent reverse proxy.
- [ ] Gunicorn binds only to loopback, for example `127.0.0.1:8000`.
- [ ] The web service runs under systemd or an equivalent supervisor with bounded
  workers, threads, timeout, and restart policy.
- [ ] Flask's development server is not exposed to users.
- [ ] The deployed host has reviewed [../../deploy/README.md](../../deploy/README.md)
  and replaced every domain, path, TLS certificate, and secret placeholder.

## Server Mode Core Safety

Status machine:

- [ ] Mode switching is only available through official root-only APIs.
- [ ] Every mode switch creates a checkpoint.
- [ ] Production entry cannot bypass the production gate.
- [ ] `maintenance` and `incident_lockdown` are formal server modes.
- [ ] `mode_switch_log` is written even when the normal audit chain is disabled.

Mode switch logs:

- [ ] `mode_switch_logs` is append-only at DB level for update/delete.
- [ ] Every row has `event_uuid`, `prev_hash`, `row_hash`,
  `hmac_signature`, `key_version`, `server_boot_id`, request metadata, and
  actor metadata.
- [ ] Hash chain verification reports `broken_links = 0`.
- [ ] HMAC verification reports `invalid_signatures = 0`.
- [ ] There is only one canonical chain.
- [ ] Snapshot restore does not remove or overwrite mode switch logs.
- [ ] Superweak rollback does not remove or overwrite mode switch logs.
- [ ] No frontend or backend API can delete or edit mode switch logs.
- [ ] `GET /api/server-mode/logs/verify` and
  `GET /api/root/server-mode/logs/verify` return `result=PASS`.

Audit export:

- [ ] Every mode switch creates a JSON event under
  `runtime/reports/server_mode_audit/`.
- [ ] Daily JSONL bundle and `.sha256` digest are generated.
- [ ] Restore and superweak rollback do not remove audit exports.
- [ ] Export failure blocks `production` / `dev_ready` and enters
  `incident_lockdown`.

Snapshot and restore:

- [ ] Restore rolls back database state.
- [ ] Snapshot/archive contains forensic copies of `finance`, `points_chain`,
  and `trading`, but restore reports them as
  `append_only_financial_restore_disabled` and does not overwrite live state.
- [ ] CLI runtime restore preserves the current live financial DBs, chain seed,
  and storage payloads; a target with no live ledger refuses financial archive
  replay and uses governed recovery instead.
- [ ] Restore does not roll back protected mode switch logs.
- [ ] Restore failure enters `incident_lockdown`.
- [ ] Post-restore validation checks DB, PointsChain, Cloud Drive metadata, and
  integrity manifest state.

Superweak sandbox:

- [ ] Superweak writes are disposable.
- [ ] Exiting superweak restores the checkpoint and leaves no dirty data.
- [ ] Crash/startup recovery restores the checkpoint.
- [ ] Superweak cannot be used to gain persistent privilege.
- [ ] Superweak Cloud Drive quota is forced to 10MB for every account, including
  root.

Incident lockdown:

- [ ] Non-root APIs are blocked.
- [ ] Tester tokens are invalid.
- [ ] Existing non-root sessions are invalidated by the live request guard.
- [ ] Switching to superweak is blocked.
- [ ] Only root recovery APIs remain available.

## Tester Token Security

- [ ] Tester token routes are whitelist-scoped.
- [ ] Path traversal and encoded bypass are rejected.
- [ ] Route normalization rejects `%2f`, `%5c`, encoded dot traversal,
  semicolon path params, backslashes, and `..`.
- [ ] Rate limit is enforced.
- [ ] Expiration is enforced.
- [ ] Revocation is immediate.
- [ ] Every tester-token allow/deny is recorded in `tester_token_audit`.
- [ ] Tester token cannot call root APIs.
- [ ] Tester token cannot delete checkpoints.
- [ ] Tester token cannot read another user's Cloud Drive.

## Shadow Isolation

- [ ] `shadow_role` does not participate in formal permission checks.
- [ ] `shadow_points` does not affect formal PointsChain.
- [ ] Shadow role/wallet/transactions stay in shadow tables.
- [ ] Code scan confirms no formal permission context uses `shadow_role`.

## Production Gate

- [ ] Required reports exist:
  - clean_smoke
  - adversarial
  - redteam_l2
  - pytest
  - log_chain_verify
  - integrity_guard
  - stress
  - permission
  - functional
  - pentest
  - snapshot_restore
  - points_chain_consistency
  - cloud_drive_quota_permission
  - ai_agent_boundary
- [ ] Report hash uses `sha256:<64 hex>`.
- [ ] Report includes target commit, target branch, server mode, test result,
  tester, and signature.
- [ ] Replay of the same report hash and commit is rejected.
- [ ] Critical/high findings block production.
- [ ] Unresolved findings block production.

## Required Test Evidence

- [ ] `scripts/security/server_mode/server_mode_v2_clean_smoke.py` passes.
- [ ] `scripts/security/server_mode/server_mode_v2_adversarial.py` passes.
- [ ] Adversarial report includes payloads, state snapshots, expected/actual,
  hash-chain evidence, restore evidence, and lockdown evidence.
- [ ] Relevant pytest suite passes.
- [ ] `git diff --check` passes.
- [ ] Secret scan passes.
- [ ] `scripts/security/server_mode/server_mode_v2_redteam_l2.py` passes with
  `production_readiness: YES`.
- [ ] `scripts/security/server_mode/server_mode_v2_live_http_smoke.py` passes with
  `production_readiness: YES`.
- [ ] Live HTTP smoke evidence includes real HTTP CSRF/session login, tester
  token traversal requests against live routes, true SIGKILL superweak
  recovery, incident-lockdown old session/token rejection, and live log-chain
  verification.
- [ ] Off-host append-only log replication / filesystem-level immutable storage
  is either verified in the deployment environment or explicitly accepted as a
  deployment residual risk.
- [ ] `scripts/testing/operational_campaign_24h.py` completed at least 86,400
  active seconds; authorization/dependency wait time is excluded.
- [ ] Primary stayed under synchronized multi-account operation rotation while
  the recovery target ran destructive snapshot, archive, restore, restart,
  wallet-incident, and governed-branch drills.
- [ ] Long-video evidence includes a one-hour-or-longer source, two audio tracks,
  subtitle, concurrent uploads, HLS jobs/playlists/segments, random seek,
  password sharing, desktop/mobile playback, and revoke-after-use rejection.
- [ ] AI Agent evidence covers root/member UI, Drive/share, server operations,
  governance, trading, media tasks, confirmation/role boundaries, and launch
  preflight; configured external ComfyUI also completes real generation.
- [ ] Trading and PointsChain evidence covers background matching, bots,
  margin/lending, high-frequency transfer/trading, replay/idempotency,
  overspend, branch/governance, theft/freeze/recovery, and financial invariants.
- [ ] Campaign evidence reports no missing account/operation success, hard
  failure, source drift, secret leak, DB lock, unhandled server traceback,
  silent frontend failure, or unplanned sentinel outage.
- [ ] Campaign checkpoint ended in `complete`; primary/recovery PID evidence has
  non-zero RSS; DB/WAL, memory, load, latency and final control-plane checks are
  within configured SLO. A short `--allow-short-duration` run is not evidence.
- [ ] The formal JSON was imported with `HACKME_OPERATIONAL_CAMPAIGN_REPORT`
  through `scripts/security/gate/on_live_reports_make.py`; the signed
  `operational_campaign_24h` required report passes and its source manifest
  matches the exact code being promoted.
- [ ] Root AI Agent launch preflight was verified as dry-run by default; an
  explicit production switch requires `auto_switch=true` and exact
  `confirm=GO_LIVE`.

## Scope Note

Server Mode v2 `production_readiness: YES` means the server-mode control plane
passed its dedicated clean smoke, adversarial, Red Team L2, and live HTTP
session/kill-9 evidence. It does not mean the whole site is production-ready.

Whole-site production still requires separate passing evidence for:

- stress
- permission
- functional
- pentest
- snapshot_restore
- points_chain_consistency
- cloud_drive_quota_permission
- ai_agent_boundary
- 24-hour dual-target synchronized operational campaign
- off-host append-only audit backup / immutable log replication

The aggregate check is:

```bash
PYTHONPATH=. scripts/security/pentest/run_pentest.sh \
  --target https://<staging-or-production-host> \
  --only whole-site-production-gate
```

The aggregate report must end with:

```text
WHOLE_SITE_PRODUCTION_GATE_SUMMARY:
- result: PASS
- production_readiness: YES
- critical_findings: 0
- high_findings: 0
```

Do not copy an old release's module count or artifact path into a new sign-off.
Attach the current run's JSON/Markdown reports from the selected runtime,
confirm the target commit and branch match the deployed build, and record any
explicitly accepted residual risk beside that release.

## Final Decision

Production is allowed only when every item above is checked.

```text
ALL PASS -> production allowed
ANY FAIL -> production blocked
```
