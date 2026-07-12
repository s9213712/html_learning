# Scanner Baseline RC1

Status: RC1 scanner policy.

The wallet/economy direct-call scanner is allowed to report known non-blocker
findings, but release is blocked by any new blocker or unclassified product
bypass.

Baseline command:

```bash
python3 scripts/security/gate/wallet_direct_call_inventory.py \
  --fail-on-blocker \
  --json-out artifacts/qa/wallet_direct_call_inventory_release_gate.json \
  --md-out artifacts/qa/wallet_direct_call_inventory_release_gate.md
```

## Baseline Rule

- Known allowed finding: does not block release.
- Core primitive: allowed only inside PointsChain/Economy internals.
- Test helper: allowed only in tests.
- Migration-only path: allowed only when not reachable as product runtime.
- Deprecated dead path: allowed only if runtime disabled.
- Product bypass: blocker.
- New unclassified finding: review required and treated as blocking until
  classified.

## RC1 Required Result

- scanner blockers = 0
- direct product `record_transaction` bypass = 0
- direct product `_record_transaction` bypass = 0
- direct mutable balance bypass = 0
- new Economy/PointsChain facade bypass = 0

The latest known baseline during RC1 hardening had 41 scanner findings and 0
blockers. That number is not itself the release target; the target is zero
blockers and no unclassified product bypass.
