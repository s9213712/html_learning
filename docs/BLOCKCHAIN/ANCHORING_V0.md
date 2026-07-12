# PointsChain Anchoring v0

Anchoring v0 exports signed local checkpoints. It is not a bridge, public
chain publication, validator protocol, or external consensus mechanism.

## Purpose

RC1 local tamper detection assumes the host, DB, and local secrets have not all
been compromised together. Anchoring v0 improves post-incident evidence by
letting operators copy signed chain roots to storage outside the runtime host.

## Export Command

```bash
export HACKME_RUNTIME_DIR=/srv/hackme-web/runtime
python3 scripts/ops/export_chain_anchor.py \
  --environment production \
  --release-version "$SERVER_RELEASE_ID" \
  --out-dir /srv/hackme-web-evidence/anchors
```

`--db` may be supplied instead of `HACKME_RUNTIME_DIR`. The signing key and
optional chain seed default beside that selected runtime; the script never
falls back to a checkout-local `runtime/` directory.

Default output:

```text
/tmp/hackme_web_test_artifacts/anchors/<timestamp>.json
```

That default is intended for isolated QA. Production operators must select a
durable external evidence directory with `--out` or `--out-dir` and copy the
result off-host.

## Anchor Payload

The JSON contains:

- `schema_version = pointschain_anchor_v0`
- `generated_at`
- deployment environment and release version
- canonical branch
- latest block height/hash/merkle root/sealed time
- latest ledger id/hash
- block, ledger, wallet, pending-transfer, and governance counts
- `chain_root`, derived from branch, latest block hash, latest ledger hash, and
  counts
- local chain verify result when `runtime/.chain_seed` is available
- HMAC signature, signing key id, and signed payload hash

## Storage Guidance

Copy anchor files to at least one place outside the mutable runtime host:

- offline archive
- read-only object storage
- private Git repository
- admin-downloaded archive
- timestamp service

RC1.1 does not automatically publish anchors externally. Automated publication
is a later operational integration step.

## Limitations

- HMAC signing key is still local unless operators copy anchors out.
- It does not stop a live compromise by itself.
- It does not replace restore drills, chain verify, replay verify, or snapshot
  integrity checks.
- External immutable anchoring remains post-RC1 unless separately approved.
