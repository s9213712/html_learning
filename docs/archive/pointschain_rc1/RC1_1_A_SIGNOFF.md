# RC1.1-A Signoff

## Release

- Release: RC1.1-A Operational Integrity Drills
- Branch: `04b.BLOCKCHAIN_RC1.1`
- Closure reference: branch head `04b.BLOCKCHAIN_RC1.1` after the signoff
  closure commit series. Use `git log -1` on the pushed branch for the exact
  current SHA.
- Status: PASS / CLOSED

## Scope

Included:

- snapshot-boundary drill
- signed local chain checkpoint anchor v0
- RC1.1 gate
- operational integrity docs
- artifact manifest and narrow artifact secret scan

Explicitly not included:

- P2P
- bridge
- full DAO
- generic user multisig spend
- external immutable anchoring
- HSM/offline signer
- full forensic taint graph

## Final Artifacts

Artifacts are generated release evidence and are not committed as source.

- `artifacts/qa/pointschain_rc1_1_gate.json`
- `artifacts/ops/restore_drill_rc1_1_gate.json`
- `artifacts/anchors/pointschain_rc1_1_54343_anchor.json`
- `artifacts/anchors/pointschain_rc1_1_54344_anchor.json`
- `artifacts/qa/rc1_1_a_artifact_manifest.json`

Artifact manifest:

- status: PASS
- commit recorded by manifest: `b7e7055`
- note: the manifest was generated before the signoff-only closure commit; this
  is expected because artifacts are not committed as source
- secret scan: PASS
- artifact count: 4

## Gate Summary

- RC1.1 gate: PASS
- snapshot-boundary drill: PASS
- operational tests: PASS
- snapshot tests: PASS
- artifact secret scan: PASS

Snapshot-boundary drill summary:

- source runtime: synthetic / isolated
- PointsChain ledger backup/restore disabled: true
- dirty state created: true
- restore performed: true
- chain verify after restore: pass
- count invariants after restore: pass
- file invariants after restore: pass
- ledger backup restore exercised: false
- production DB mutated: false

## Anchor Summary

Anchoring v0 is a signed local checkpoint export. It is not external immutable
anchoring.

- 54343 anchor export: PASS
  - chain verify: pass
  - latest block height: 60
  - latest block hash: `08ef6d7d08030fdab27a7e0f88e92909bf632a9419b26b1b95b8f07500cd64f3`
- 54344 anchor export: PASS
  - chain verify: pass
  - latest block height: 11
  - latest block hash: `988d0b38ade8cc3e381d20e571c3e5025690836b1fae3150780901fd2c72c3f6`

## Rerun Commands

```bash
python3 scripts/qa/points_chain_rc1_1_gate.py
python3 scripts/ops/rc1_restore_drill.py --out artifacts/ops/restore_drill_manual.json
python3 scripts/ops/export_chain_anchor.py --db runtime/database/database.db
python3 scripts/ops/rc1_1_artifact_manifest.py
```

## Decision

RC1.1-A Operational Integrity Drills: PASS / CLOSED.

The next recommended subphase is RC1.1-B Evidence Pack & Observability. Do not
expand RC1.1-A with product-facing financial features.
