# Server Mode v2 Test Plan

Every phase must finish with targeted tests plus smoke checks. A failure in a
mode operation must never silently succeed.

## Phase 1 Tests

Unit tests:

- Non-root cannot call mode switch.
- Unknown mode is rejected.
- Legacy `preprod` maps to `dev_ready`.
- Missing or wrong confirmation phrase is rejected.
- Checkpoint creation failure rejects mode switch.
- Successful switch inserts exactly one `mode_switch_logs` row.
- Failed switch inserts a failed `mode_switch_logs` row.
- `mode_switch_logs` can be queried but has no delete API.
- `server_checkpoints` records DB snapshot, config hash, security hash,
  PointsChain hash, Cloud Drive metadata hash, and integrity manifest hash.

Smoke:

- `GET /api/root/server-mode`
- `POST /api/root/server-mode/checkpoint`
- `POST /api/root/server-mode/switch`
- `GET /api/root/server-mode/requirements`
- `GET /api/root/server-mode/logs`

## Phase 2 Tests

Unit/integration tests:

- Restore validation passes when no dirty state remains.
- Restore validation fails if test forum content remains.
- Restore validation fails if PointsChain hash differs.
- Restore validation fails if Cloud Drive metadata hash differs.
- Restore validation fails if config/security hash differs.
- Restore validation fails if integrity manifest hash differs.
- Any failed validation enters `incident_lockdown`.
- `incident_lockdown` blocks switching to `superweak`.
- Root can resolve incident only after required verification checks pass.

Smoke:

- Create checkpoint.
- Add temporary forum/thread/content row.
- Restore checkpoint.
- Verify temporary row is gone.

## Phase 3 Tests

Integration tests:

- Entering superweak creates checkpoint and switches mode.
- Superweak applies 10MB Cloud Drive quota to all users including root.
- Root cannot override superweak 10MB quota.
- Dirty DB row created in superweak disappears after exit.
- Dirty Cloud Drive metadata created in superweak disappears after exit.
- Dirty PointsChain entries do not remain after exit.
- Restore failure on superweak exit enters `incident_lockdown`.
- `keep_dirty_state` is rejected.

Security tests:

- Non-root cannot enter/exit superweak.
- Superweak cannot delete checkpoint.

## Phase 4 Tests

Unit tests:

- Tester token hash verification.
- Expired tester token rejected.
- Revoked tester token rejected.
- Token route outside `allowed_routes` rejected.
- Token feature outside `allowed_features` rejected.
- Token rate limit enforced.

Integration tests:

- Tester changes own role; production `users.role` remains unchanged.
- Tester changes own points; production wallet and PointsChain remain unchanged.
- Tester places test trade; production trading positions/fills remain unchanged.
- Closing test/internal_test removes shadow data.
- Tester cannot access root API.
- Tester cannot read other users' Cloud Drive.
- Tester cannot delete checkpoint/snapshot.
- Tester cannot switch server mode.

## Phase 5 Tests

Unit/integration tests:

- Production entry fails when any required report is missing.
- Production entry fails when report has `pass=false`.
- Production entry fails when critical/high findings are non-zero.
- Production entry fails when report hash/signature is missing.
- Production entry succeeds only with all required passing reports.
- Production mode enables required safety controls.
- Same-IP multi-account and same-account multi-IP settings exist but default to
  disabled.

## Phase 6 Tests

Frontend/static tests:

- Root server mode page exists.
- All buttons show progress and result messages.
- Mode colors match profile matrix.
- Production requirement list marks missing/failed/passing reports.
- Tester token management renders allowed features/routes and expiry.
- Incident status renders current state and resolution blockers.

End-to-end smoke:

- Start clean server.
- Login as root.
- Create checkpoint.
- Switch to `dev_ready`.
- Switch to `maintenance`.
- Enter incident manually.
- Attempt superweak from incident and confirm it is blocked.

## Required Report After Each Phase

After each phase, report:

- Modified files
- Added tables
- Added APIs
- Test commands and results
- Smoke test result
- Known incomplete items

## Current Automated Coverage

The current implementation is covered by these focused tests:

- `tests/snapshots/test_snapshots.py`
  - checkpoint-gated mode switch
  - failed checkpoint to `incident_lockdown`
  - production report gate APIs
  - tester token APIs
  - tester shadow role/wallet isolation
  - superweak dirty-state discard
- `tests/account/auth/test_account_lockout.py`
  - production same-IP and same-account conflict policies remain disabled by
    default
  - scoped tester token login for `internal_test`
- `tests/storage/test_upload_security.py`
  - superweak Cloud Drive quota is forced to 10MB, including root
- `tests/security/integrity/test_integrity_guard.py`
  - production entry with high-risk integrity finding enters
    `incident_lockdown`
- frontend static tests
  - server mode UI still exposes required controls and responsive layout checks
- clean-environment smoke
  - `scripts/security/server_mode/server_mode_v2_clean_smoke.py` creates a new temporary DB/storage
    runtime and switches through all canonical modes without touching the live
    server data.
  - The same smoke is available from `scripts/security/pentest/run_pentest.sh --only
    server-mode-v2`.
- adversarial validation
  - `scripts/security/server_mode/server_mode_v2_adversarial.py` creates a new temporary DB/storage
    runtime and tries mode-switch log update/delete, snapshot log rollback,
    tester-token traversal, shadow-role privilege escalation, fake/replayed
    production reports, and incident-lockdown bypass.
  - The same adversarial check is available from
    `scripts/security/pentest/run_pentest.sh --only server-mode-v2-adversarial`.
- enterprise sign-off validation
  - `scripts/security/server_mode/server_mode_v2_redteam_l2.py` layers HMAC signature tamper,
    revoked-token replay, integrity-manifest tamper signal, and the full
    adversarial suite into an evidence report.
- live HTTP sign-off validation
  - `scripts/security/server_mode/server_mode_v2_live_http_smoke.py` starts an isolated loopback
    Flask server, logs in through real HTTP CSRF/session cookies, sends tester
    token traversal payloads to live routes, enters superweak, kills the server
    process with SIGKILL, restarts it, verifies rollback, enters
    `incident_lockdown`, and verifies old sessions/tokens are blocked.
  - This closes the local live-deployment coverage gap and emits
    `production_readiness=YES` when all live checks pass.
  - It still does not verify off-host append-only log replication or
    filesystem-level immutable storage.
  - With clean smoke 13/13 PASS, adversarial 8/8 PASS, Red Team L2
    `breaches_total=0`, live HTTP smoke 6/6 PASS, and zero critical/high
    findings, Server Mode v2 is marked `production_readiness=YES`.
  - This status applies only to Server Mode v2. Whole-site production readiness
    still requires separate stress, permission, functional, pentest,
    snapshot_restore, points_chain_consistency, cloud_drive_quota_permission,
    and off-host append-only audit backup / immutable log replication evidence.
  - The full enterprise bundle is available from
    `scripts/security/pentest/run_pentest.sh --only server-mode-v2-enterprise`.
  - Live HTTP smoke alone is available from
    `scripts/security/pentest/run_pentest.sh --only server-mode-v2-live-http`.
  - Red Team L2 alone is available from
    `scripts/security/pentest/run_pentest.sh --only server-mode-v2-redteam-l2`.

Required command set before merging this work:

```bash
PROJECT_ROOT=/path/to/hackme_web

"$PROJECT_ROOT/scripts/testing/pytest_in_tmp.sh" \
  tests/snapshots/test_snapshots.py \
  tests/account/auth/test_account_lockout.py \
  tests/storage/test_upload_security.py \
  tests/security/integrity/test_integrity_guard.py \
  tests/frontend/storage/test_frontend_drive_preview.py \
  tests/platform/test_release_policy.py \
  tests/frontend/layout/test_mobile_responsive_layout.py

PYTHONPATH="$PROJECT_ROOT" python3 -m py_compile \
  "$PROJECT_ROOT/server.py" \
  "$PROJECT_ROOT/services/users/auth.py" \
  "$PROJECT_ROOT/services/snapshots/schema.py" \
  "$PROJECT_ROOT/routes/public.py" \
  "$PROJECT_ROOT/routes/system_admin.py" \
  "$PROJECT_ROOT/routes/economy.py" \
  "$PROJECT_ROOT/services/security/upload_security.py"

git -C "$PROJECT_ROOT" diff --check

ARTIFACT_ROOT=/tmp/hackme_web_test_artifacts/reports/security/server_mode_v2
mkdir -p "$ARTIFACT_ROOT"

PYTHONPATH="$PROJECT_ROOT" python3 \
  "$PROJECT_ROOT/scripts/security/server_mode/server_mode_v2_clean_smoke.py" \
  --out "$ARTIFACT_ROOT"

PYTHONPATH="$PROJECT_ROOT" python3 \
  "$PROJECT_ROOT/scripts/security/server_mode/server_mode_v2_adversarial.py" \
  --out "$ARTIFACT_ROOT"

PYTHONPATH="$PROJECT_ROOT" python3 \
  "$PROJECT_ROOT/scripts/security/server_mode/server_mode_v2_redteam_l2.py" \
  --out "$ARTIFACT_ROOT"

PYTHONPATH="$PROJECT_ROOT" python3 \
  "$PROJECT_ROOT/scripts/security/server_mode/server_mode_v2_live_http_smoke.py" \
  --out "$ARTIFACT_ROOT"

cd "$PROJECT_ROOT"
scripts/security/pentest/run_pentest.sh --only server-mode-v2 \
  --target http://127.0.0.1:5000 \
  --out "$ARTIFACT_ROOT"

scripts/security/pentest/run_pentest.sh --only server-mode-v2-adversarial \
  --target http://127.0.0.1:5000 \
  --out "$ARTIFACT_ROOT"

scripts/security/pentest/run_pentest.sh --only server-mode-v2-live-http \
  --target http://127.0.0.1:5000 \
  --out "$ARTIFACT_ROOT"

scripts/security/pentest/run_pentest.sh --only server-mode-v2-enterprise \
  --target http://127.0.0.1:5000 \
  --out "$ARTIFACT_ROOT"

scripts/security/pentest/run_pentest.sh --only server-mode-v2-redteam-l2 \
  --target http://127.0.0.1:5000 \
  --out "$ARTIFACT_ROOT"
```
