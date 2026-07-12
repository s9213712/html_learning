# RC1 Release Gate

Status: required acceptance gate for the controlled permissioned PointsChain
release.

Primary command:

```bash
python3 scripts/qa/points_chain_release_gate.py --skip-live
```

Live isolated command:

```bash
export HACKME_ROOT_PASSWORD='<root-password-from-secret-manager>'
python3 scripts/qa/points_chain_release_gate.py \
  --base-url https://127.0.0.1:54343 \
  --runtime-root /tmp/hackme_web_isolated_54343/hackme_web/runtime
```

The default report root is `/tmp/hackme_web_test_artifacts`. CI may set an
absolute `HACKME_TEST_OUTPUT_ROOT` to a retained artifact mount. Do not place
credentials in command arguments or committed report files.

The gate must print:

```text
RC1 RELEASE GATE: PASS
```

## Must Pass

- scanner blockers = 0
- latest release gate = pass
- chain verify = pass
- replay protection = pass
- derived cache consistency = pass
- stress invariants:
  - no negative balance
  - no duplicate request
  - no duplicate wallet
  - no double spend
- block tamper detection = pass
- direct product bypass scanner = no blocker
- official treasury multisig threshold enforcement = pass
- user multisig outbound transfer rejected = pass
- dispute provisional freeze expires correctly = pass
- rejected, cancelled, and expired disputes auto-unfreeze = pass
- root/admin dispute API has no reporter_user_id, username, email, or IP leak = pass
- Host header validation = pass
- multipart form limits = pass
- maintenance bypass token accepted only through `X-Maintenance-Bypass-Token` = pass

## Must Document

- Same-IP mass login 429 is expected anti-abuse behavior.
- RC1 has no P2P consensus.
- RC1 has no external chain bridge.
- RC1 has no external immutable anchor yet.
- RC1 has no generic user multisig spend.
- RC1 has no full DAO anti-capture.
- RC1 has no full taint propagation graph.

## Current Gate Coverage

- `scripts/security/gate/wallet_direct_call_inventory.py --fail-on-blocker`
- `tests/points/test_governance_branch.py`
- `tests/points/test_wallet_identity.py`
- `tests/points/test_points_explorer.py`
- block tamper tests in `tests/points/test_points_chain.py`
- selected trading wallet and chain-spend tests in `tests/trading/core/test_trading_engine.py`
- Flask/Werkzeug hardening tests in `tests/security/gates/test_flask_hardening.py`
- feature flag and production guard tests
- optional live governance/dispute Playwright probe
- optional realistic recovery drill against an isolated runtime

## Fail Rules

RC1 cannot be a pass candidate if any of these happens:

- scanner reports a blocker
- a product flow bypasses Economy/PointsChain facade
- user multisig wallet can transfer out, reserve spend, or fund exchange spend
- official treasury proposal executes before governance, timelock, and multisig
- anonymous dispute leaks reporter identity fields
- provisional dispute freeze gets stuck after rejected/cancelled/expired status
- block tamper verification misses forged sealed transaction or forged block hash
- Host header, multipart limits, or maintenance bypass regress
