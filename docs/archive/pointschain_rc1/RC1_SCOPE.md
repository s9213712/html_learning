# RC1 Scope: Controlled Permissioned PointsChain

Status: RC1 release boundary.

PointsChain RC1 is a controlled, permissioned, single-node internal ledger
release. It is intended to make the current site economy replayable,
auditable, governable, and releasable under a clear gate.

## RC1 Is

- A site-internal permissioned PointsChain ledger.
- A single-node controlled deployment profile.
- An accounting and governance validation release.
- A wallet, transfer, pending/proved, fee-burn, seal, verify, replay,
  backup-disabled recovery-boundary, and forensic/governance recovery layer for
  site points.
- An official treasury multisig release: treasury execution requires proposal,
  governance pass, timelock, payload-hash verification, and signer threshold.
- An anonymous address-proven dispute release: self-custody disputes and
  replies are signed by wallet address, while account-bound official hot wallets
  use server-verified `account_bound_official_hot_v1` proof. Public/root/admin
  views remain address-only and must not expose reporter account identity.
- A provisional-freeze and governance-escalation release for disputed
  transactions.
- A local chain-integrity release with block tamper detection under normal host
  integrity assumptions.

## RC1 Is Not

- Not a public mainnet.
- Not a P2P blockchain.
- Not a validator-consensus chain.
- Not a fork-choice implementation.
- Not a full DAO.
- Not a CEX-grade public exchange.
- Not an external-chain bridge.
- Not a BSC, Layer2, staking, lending, liquidation, or market-making expansion.
- Not a generic user multisig spend system.
- Not a full taint graph or AML forensic engine.
- Not an independent Etherscan-like public indexer.
- Not an HSM or hardware custody release.

## Tamper Boundary

RC1 tamper detection is local-integrity based. It can detect DB/block tampering
under normal host integrity assumptions.

It does not yet protect against full host compromise where both the DB and
`.chain_seed` are stolen or modified. External anchoring, offline checkpoints,
file integrity monitoring, and hardware key custody are production/post-RC1
requirements.

## Release Principle

Do not expand RC1. Lock it down.

Generic user multisig spend, external anchoring, P2P consensus, bridges, full
DAO anti-capture, HSM custody, and full forensic taint graph are post-RC1 and
must stay out of this release.
