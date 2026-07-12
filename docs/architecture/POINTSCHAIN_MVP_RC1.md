# PointsChain MVP RC1

## Scope Lock

PointsChain MVP RC1 is the release-candidate hardening target for the current
in-site points economy.

RC1 is not a public blockchain. It is a permissioned/private chain accounting
layer for this site. The goal is to make the current financial model clean,
replayable, governed, and release-gate verifiable before considering any larger
blockchain roadmap.

Do not split this work into additional phase names. The delivery target is:

`PointsChain MVP RC1`

## RC1 Positioning

RC1 must support:

- wallet identity and multi-wallet accounting
- wallet-to-wallet transfer requests
- pending/proved finality
- sender freeze during pending
- receiver credit only after 20/20 Proved
- chain fee burn
- block sealing
- chain verify
- replay and derived-cache verification
- backup, recovery, and safe mode
- pc0 service-fee immediate internal debit plus legacy service-fee audit replay
- exchange fund accounting and reconciliation
- root/admin governance guardrails
- feature-flagged basic economy mode
- one-command release-gate verification

The financial source of truth is replayable ledger/economy events. Derived
wallet caches and UI summaries are not financial truth.

## Required RC1 Outcomes

RC1 is complete only when all of these are true:

- no product flow can bypass the Economy / PointsChain facade
- no product flow directly mutates wallet balances
- all debit, credit, reward, fee, burn, and exchange fund movement is replayable
- root, mint, Treasury, and exchange fund operations have policy limits and audit
- feature flags are explicit and PointsChain can be disabled without breaking
  basic economy service spending
- release-gate tests can be rerun from one command
- scanner, tests, Playwright, pentest, stress, and production guardrails catch
  release blockers before release

## Fixed Delivery Packages

### A. Legacy Bypass Cleanup

The RC1 scanner must classify direct financial writes as:

- `allowed_internal_primitive`
- `approved_facade`
- `test_helper`
- `migration_only`
- `deprecated_dead_path`
- `blocker_product_bypass`

Runtime product code outside approved facades must not call:

- `record_transaction(...)`
- `_record_transaction(...)`
- direct wallet balance mutation
- direct ledger append
- direct debit or direct credit helpers

Known baseline before RC1 cleanup:

- `11` runtime product migration paths remain in the wallet direct-call inventory.
- `0` non-core direct official wallet balance mutation blockers are currently
  reported by the legacy scanner.

RC1 target:

- `scanner --fail-on-blocker` returns zero blockers after all product bypasses
  are migrated or explicitly blocked.
- Legacy product bypass count is zero.

### B. Product Fee Flow Convergence

Existing product flows must converge on approved financial paths. This work
does not add new paid product features.

Covered modules:

- ComfyUI
- Trading
- Video
- Storage
- Games
- Tips
- Marketplace placeholder
- AI task / agent task placeholder
- default grants
- signup reward
- contribution reward
- admin grant
- Treasury grant
- service fees

Standard service-fee flow:

```text
request service
-> pc0 internal custody debit
-> service revenue credit to official Treasury
-> no network fee and no batch threshold
```

Legacy `service_fee_reserve` / batch rows may still exist in old runtimes and
are read for audit compatibility only. New service payments must not create
reserve rows or wait for a 100-point threshold.

Failure flow:

```text
request service
-> no ledger write before successful service commit, or append refund/compensation
   from the same pc0 rail if a later product step fails after debit
-> no burn
```

Wallet-to-wallet transfer flow:

```text
sender freezes amount + fee
-> pending transaction hash
-> 20/20 Proved
-> ledger append
-> receiver credited
-> fee burned
```

Each product flow must cover:

- success capture
- failure release or refund
- insufficient balance
- PointsChain disabled behavior

### C. Mint / Treasury / Exchange Fund Guardrails

RC1 does not implement DAO governance. It implements minimum abuse resistance
for important money operations.

MintGuard v1 requires:

- proposal
- reason
- reference
- idempotency key
- per-proposal cap
- daily cap
- timelock
- `mint_remaining` enforcement
- `reserved_locked` enforcement
- violation incident
- dashboard visibility for pending proposals

TreasuryGuard v1 requires each movement to include:

- source
- destination
- amount
- transaction type
- reason
- reference
- idempotency key
- operator
- approval mode
- policy version

Treasury movement types:

- `treasury_to_promo`
- `treasury_to_exchange`
- `treasury_to_user_grant`
- `treasury_to_burn`
- `treasury_internal_rebalance`

ExchangeFundPolicy v1 buckets:

- `spot_liquidity`
- `futures_insurance`
- `liquidation_buffer`
- `market_maker_pool`

Exchange fund movement requires:

- proposal
- bucket
- reason
- daily movement cap
- low-water warning
- critical-water warning
- fallback incident
- chain balance and trading reserve pool reconciliation

### D. Minimal Approval / Multisig / Timelock Enforcement

RC1 uses a minimum approval object for guarded operations:

```text
approval_request:
- uuid
- operation_type
- source_wallet
- destination_wallet
- amount
- requested_by
- required_signers
- collected_signers
- threshold
- status
- expires_at
- timelock_until
- reason
- reference
- idempotency_key
- policy_version
```

Supported statuses:

- `draft`
- `pending_signatures`
- `timelocked`
- `ready_to_execute`
- `executed`
- `cancelled`
- `expired`
- `rejected`

RC1 applies this enforcement to:

- mint proposal
- large Treasury movement
- large exchange fund movement
- production-profile official transfer
- emergency rollback/recovery branch pointer changes

Governance security details are locked in
`docs/architecture/GOVERNANCE_SECURITY_MODEL.md`. The important RC1 invariant is
that governance passing does not equal official wallet control: official
treasury proposals still require execution-bundle hash verification and
official multisig signing after the vote.

RC1 multisig scope is intentionally narrow:

- spend-capable multisig exists only for `wallet_type=official_treasury_multisig`
  with `wallet_scope=official_treasury`
- general user multisig is hidden/preview only and is downgraded to
  `spend_capability=receive_only`
- user multisig can receive and be observed, but transfer/service-fee/wallet-fee
  spend paths must reject it server-side
- manager+ governance UI must expose Treasury Signer Center for signer list,
  role/weight, threshold, pending proposals, timelock, and readiness

Development and isolated profiles may allow root single approval, but the UI and
reports must mark it as a dev override. Production requires root plus admin,
timelock, and cap enforcement.

### E. Release Gate Automation

RC1 must provide:

- `scripts/qa/points_chain_release_gate.py`
- `docs/qa/POINTSCHAIN_RELEASE_GATE.md`
- `/tmp/hackme_web_test_artifacts/qa/pointschain_rc1_release_gate.json` by
  default, or an explicit external CI artifact path

The release gate must cover:

- feature flag matrix
- wallet matrix
- transfer matrix
- service fee matrix
- Explorer / block matrix
- stress matrix
- root / admin matrix
- security matrix

Required final JSON shape:

```json
{
  "release_candidate": "PointsChain MVP RC1",
  "scanner_blockers": 0,
  "chain_verify": "pass",
  "replay_verify": "pass",
  "derived_cache_verify": "pass",
  "playwright": "pass",
  "pentest": "pass",
  "stress": "pass",
  "legacy_bypass_paths": 0,
  "production_profile_guard": "pass"
}
```

### F. Production Profile Guardrails

Profiles:

- `development`
- `isolated`
- `staging`
- `production`

Development and isolated may allow:

- default local credentials
- root single-sign override
- debug chain APIs
- test grants
- unsafe local reset

Production must reject or fail release gate on:

- default root/admin/test credentials
- dev single-sign override
- debug reset
- unsafe bootstrap
- unprotected root grant
- untimelocked mint
- direct chain mutation endpoints
- disabled mint, Treasury, exchange, or approval guardrails
- scanner blockers

### G. Documentation and Operations

RC1 documentation must include:

- `docs/architecture/POINTSCHAIN_MVP_RC1.md`
- `docs/architecture/ECONOMY_LAYER_GUARDRAILS.md`
- `docs/architecture/MINT_GUARD_V1.md`
- `docs/architecture/TREASURY_GUARD_V1.md`
- `docs/architecture/EXCHANGE_FUND_POLICY_V1.md`
- `docs/architecture/POINTSCHAIN_APPROVALS_V1.md`
- `docs/qa/POINTSCHAIN_RELEASE_GATE.md`
- `docs/ops/POINTSCHAIN_BACKUP_RECOVERY.md`
- `docs/ops/POINTSCHAIN_SAFE_MODE.md`
- `docs/ops/PRODUCTION_PROFILE_GUARDRAILS.md`

## Deferred for Post-RC1

These items are explicitly out of scope for RC1:

- P2P networking
- validator set
- fork choice
- external chain deposit or withdrawal
- BSC / Layer2 bridge
- staking
- lending
- liquidation engine expansion
- DAO governance
- public Etherscan-like standalone indexer
- fully decentralized multisig wallet protocol
- general-user spendable multisig wallets
- new paid product features
- expanded exchange gameplay

If any of these are discovered while implementing RC1, document the risk here
or in the relevant guardrail document instead of adding it to RC1 scope.

## Final RC1 Acceptance

RC1 cannot be called complete unless all checks pass:

- scanner blockers = `0`
- legacy product bypass paths = `0`
- chain verify pass
- replay verify pass
- derived cache verify and rebuild pass
- no negative balance under stress
- duplicate idempotency keys have no duplicate effect
- pending receiver is not credited
- sender amount plus fee is frozen during pending
- 20/20 Proved confirms the transfer
- fee goes to BURN
- seal block pass
- backup and recovery pass
- mint cap and reserved-locked enforcement pass
- timelock enforcement pass
- production approval enforcement pass
- Treasury movement audit pass
- exchange fund bucket movement controls pass
- abnormal guarded operation writes incident
- ComfyUI, Trading, Video, Storage, and Games quick regressions pass
- service fee reserve, capture, release, and burn pass
- cold wallet local signing pass
- pentest pass
- Playwright pass
- production default credential guard pass
- disabled legacy admin APIs still return `410`
- RC1 architecture, QA, operations, and release reports are complete
