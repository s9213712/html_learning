# RC1.1 Operational Integrity Release

Status: in progress on branch `04b.BLOCKCHAIN_RC1.1`.

RC1.1 is an operational hardening release for the already closed RC1
controlled permissioned PointsChain. It does not expand the product-facing
financial surface.

## Goals

- Prove ordinary snapshot/restore boundaries with a repeatable isolated drill, while keeping PointsChain ledger backup/restore disabled.
- Export signed local chain checkpoints that can be copied to offline or
  read-only storage.
- Keep release checks runnable from a single RC1.1 gate script.
- Improve operational evidence without changing user spend, bridge, P2P, or
  governance scope.

## Included In RC1.1-A

- `scripts/ops/rc1_restore_drill.py`
  - builds an isolated synthetic runtime
  - creates PointsChain ledger/block data
  - creates a server snapshot and verifies PointsChain append-only recovery policy
  - dirties DB and runtime files
  - restores the snapshot
  - verifies chain, restore-boundary, and backup-disabled invariants
- `scripts/ops/export_chain_anchor.py`
  - exports a signed checkpoint JSON
  - includes chain height, latest block hash, latest ledger hash, chain root,
    branch, counts, and local verify status
  - writes no chain state and publishes nowhere by itself
- `scripts/qa/points_chain_rc1_1_gate.py`
  - compiles new ops scripts
  - runs RC1.1 operational tests and snapshot tests
  - runs the isolated snapshot-boundary drill and emits a JSON gate artifact
  - generates the RC1.1-A artifact manifest and runs the artifact secret scan

## Explicitly Not Included

- P2P networking
- validator set or fork choice
- external chain bridge
- generic user multisig spend
- full DAO anti-capture
- full forensic taint graph / AML engine
- public exchange liability expansion
- HSM/offline signer integration

These remain post-RC1 backlog items unless a separate scope is approved.

## Acceptance

RC1.1-A is acceptable when:

- `tests/points/test_rc1_1_operational_integrity.py` passes
- `tests/snapshots/test_snapshots.py` passes
- `scripts/ops/rc1_restore_drill.py` produces `ok=true`
- `scripts/ops/export_chain_anchor.py` produces a signed checkpoint
- `scripts/qa/points_chain_rc1_1_gate.py` returns PASS
- `scripts/ops/rc1_1_artifact_manifest.py` reports no missing artifacts or
  forbidden secret patterns

Generated artifacts are operational output and are not the source of truth.
They may be archived externally, but normal commits should not bundle local
runtime artifacts unless specifically requested.
