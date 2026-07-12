# RC1 Signoff

Generated at: 2026-05-23T02:56:13Z

## Release

- Release name: RC1 Controlled Permissioned PointsChain Release
- Release type: controlled / permissioned / single-node internal ledger
- Final status: PASS / CLOSED
- Commit reference: branch head `04.BLOCKCHAIN_RC1` after the closure commit
  series. Use `git log -1` on the pushed branch for the exact release SHA.
- Working tree note: this signoff covers the current RC1 hardening workspace
  state and the live artifact below.

## Runtime Checked

- `https://127.0.0.1:54343`
- `https://127.0.0.1:54344`

The current PIDs are runtime smoke-test facts only. They are not release
conditions.

## Final Artifacts

- Final live release gate:
  `artifacts/qa/pointschain_rc1_release_gate_live.json`
- Hardening validation artifact, live gate skipped, not final signoff:
  `artifacts/qa/pointschain_rc1_release_gate_hardening_skip_live.json`
- Live governance/dispute Playwright:
  `artifacts/qa/playwright/pointschain_governance_dispute_probe.json`
- Live dispute privacy/API regression:
  `artifacts/qa/pointschain_dispute_api_privacy_final.json`
- Scanner:
  `artifacts/qa/wallet_direct_call_inventory_release_gate.json`

## Gate Summary

| Gate | Result |
|---|---|
| scanner blockers | 0 |
| scanner findings | 41, all allowed internal primitive or test helper |
| release gate | PASS |
| chain verify | PASS |
| replay / derived cache | PASS |
| block tamper | PASS |
| Flask/Werkzeug hardening | PASS |
| direct product bypass | 0 blocker |
| Playwright/live gate | PASS |
| live recovery drill | PASS |
| production profile guard | PASS |

## User Multisig RC1 Hard-Block

Expected RC1 behavior:

- receive/view: allowed
- transfer out: rejected
- service fee reserve: rejected
- exchange order funding: rejected
- product payment/spend: rejected
- rejection code: `user_multisig_receive_only_rc1`

Covered by:

- `tests/points/test_wallet_identity.py`
- `tests/trading/core/test_trading_engine.py`
- `scripts/qa/points_chain_release_gate.py`

## Official Treasury Multisig

Expected RC1 behavior:

- OFFICIAL_TREASURY proposal required
- governance pass required
- root veto must not be active
- timelock must be elapsed
- execution payload hash must match
- signer snapshot must be valid
- threshold count must be met
- threshold weight must be met
- execute writes audit and submitted transaction

Covered by:

- `tests/points/test_governance_branch.py`
- `scripts/qa/points_chain_release_gate.py`

## Dispute Privacy

Live privacy probe result: PASS.

Forbidden identity fields were not leaked:

- `reporter_user_id`
- `reporter_username`
- `username`
- `email`
- `ip`
- `ip_address`
- `session_id`
- `login_id`
- `account_id`

Allowed public dispute material remains address/tx based:

- `tx_hash`
- `from_address`
- `to_address`
- `amount`
- `signature_verified`
- `statement`
- `evidence_refs`
- `case_status`
- `freeze_expires_at`
- `voting_ends_at`

## Runtime Parity

Checked key file parity from repo to:

- `/tmp/hackme_web_isolated_54343/hackme_web`
- `/tmp/hackme_web_isolated_54344/hackme_web`

Result: no drift for the RC1 hardening key files.

Path casing policy:

- canonical ops path: `docs/ops`
- `docs/OPS` must not be introduced

## Known Limitations

Explicitly post-RC1 backlog only:

- external anchor
- HSM / offline signer
- generic user multisig spend
- P2P / validator / fork choice
- external chain bridge
- full DAO anti-capture
- full forensic taint graph / AML engine
- public exchange liability expansion

## Signoff Decision

RC1 Controlled Permissioned PointsChain Release: PASS / CLOSED

Final artifact:

- `artifacts/qa/pointschain_rc1_release_gate_live.json`
