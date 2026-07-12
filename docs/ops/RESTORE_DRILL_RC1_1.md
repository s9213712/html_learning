# RC1.1 Snapshot Boundary Drill

The RC1.1 drill proves that ordinary runtime snapshot/restore mechanics work
without reintroducing PointsChain ledger backup/restore. It is safe to run from
a developer checkout because it creates a synthetic runtime under `/tmp` unless
`--workdir` is supplied.

## Command

```bash
python3 scripts/ops/rc1_restore_drill.py
```

The report defaults to
`/tmp/hackme_web_test_artifacts/ops/restore_drill_<timestamp>.json`. Set an
absolute `HACKME_TEST_OUTPUT_ROOT` or pass `--out` when CI needs a retained
external artifact path.

The drill:

1. Creates a temporary runtime and SQLite database.
2. Seeds root/admin/test users and required settings.
3. Creates PointsChain genesis ledger/block data.
4. Confirms PointsChain ledger backup/restore is disabled.
5. Creates a server snapshot.
6. Adds dirty ordinary DB rows and dirty runtime files.
7. Restores the snapshot.
8. Runs PointsChain verify.
9. Checks that dirty ordinary data was removed while ledger backup/restore stayed disabled.

這支 synthetic drill 證明 service-level 邊界；正式 campaign 另外在 recovery target 建立
snapshot 後的新金融交易，確認 ordinary state 會回復，但該 append-only 交易與 live
`finance.db` 不會被 snapshot 或 CLI archive 覆寫。完整流程見
[../AGENTS/24H_OPERATIONAL_CAMPAIGN.md](../AGENTS/24H_OPERATIONAL_CAMPAIGN.md)。

## Artifact

The output JSON includes:

- `ok`
- snapshot id
- PointsChain backup/restore disabled status
- baseline/dirty/restored counts
- restore result
- baseline and restored chain verify results
- invariant map

The drill passes only if every invariant is true.

For debugging, `--workdir` accepts only a new, non-existing directory below
`/tmp`; it never deletes a pre-existing path. Add `--keep-workdir` to retain
that isolated directory after the drill.

## Operational Use

Run this drill:

- before RC1.1 signoff
- after snapshot/restore code changes
- after PointsChain schema changes
- after changing runtime secret or file-root configuration

For live deployments, run the drill against an isolated staging runtime. Do not
use it as a ledger rollback mechanism; PointsChain incidents must use safe mode,
forensic bundles, recovery branches, emergency governance, and append-only
correction transactions.
