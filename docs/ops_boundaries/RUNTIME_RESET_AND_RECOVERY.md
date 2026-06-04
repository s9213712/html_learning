# Runtime Reset, Snapshot, And PointsChain Recovery

This document defines the ownership boundary between the three recovery tools.

For the operator-first summary, start with
[09_SNAPSHOT_RESET_RESTORE.md](../09_SNAPSHOT_RESET_RESTORE.md). This file keeps
the detailed boundary and conflict rules.

## Runtime Reset

Runtime reset is a destructive cleanup tool for returning the server to a
minimal runnable state. There are two operational paths and they intentionally
have different key-handling boundaries.

The in-app/admin runtime reset path does:

- create a `pre_reset` server snapshot first
- clear resettable application tables such as forum, chat, DM, storage, album,
  report, notification, moderation, game, and runtime feature data
- clear configured canonical runtime file roots such as `runtime/storage/` and
  `runtime/chats/`
- reset PointsChain live tables through `PointsLedgerService.reset_runtime_chain`
- reset the secure audit chain through `reset_audit_chain_with_event`
- rotate/remove deployment-generated secrets and manifests according to the
  production reset implementation
- switch the server back to management-only feature defaults
- return `requires_restart: true`

The development launcher path, `test_for_develop.sh --reset`, is different: it
preserves server-side key material such as `.filekey`, `.fkey`, `.csrfkey`,
`.integrity_key`, `.chain_seed`, TLS key/cert files, and
`integrity_manifest.json`. Before clearing DB/catalog state it moves existing
storage contents into `storage/.reset_orphan_recovery/reset_<timestamp>/` with
pre-reset DB metadata, copied admin decrypt helper scripts, runtime secrets, and
an `orphaned_storage/` folder. This keeps the post-reset storage root clean
while still allowing exactly one recovery action later.

The reset recovery bundle offers two mutually exclusive helpers:

- `export_server_encrypted_plaintext.sh <output-dir>` exports decrypted
  `server_encrypted` files using the bundled DB metadata and server-side keys.
  Strict E2EE files still require the user's passphrase and the bundled
  `scripts/admin/decrypt_server_files.py --privacy-mode e2ee` helper.
- `restore_database_catalog_from_bundle.sh` imports the pre-reset catalog and
  moves encrypted files back into place. Original owners are preserved when the
  user still exists; missing-owner catalog rows are reassigned to root.

Once either helper starts, `recovery_action.lock` prevents using the other helper
from the same bundle. This avoids a root operator both obtaining plaintext and
then restoring the same encrypted catalog back into service.

It does not delete the `pre_reset` snapshot. That snapshot is the recovery point
if reset was triggered accidentally.

After reset, restart the server and verify the selected recovery path before
removing bundle backups.

## Server Snapshot / Restore

Server snapshot is a whole-server recovery mechanism.

It includes:

- SQLite database backup
- runtime file archive for configured file roots
- selected config archive with `.env` redacted
- manifest, checksums, metadata, and snapshot audit events

It also includes configured runtime secret files. Snapshot metadata records
`secrets_excluded: false` and lists `runtime_secret_files`, which currently
cover deployment-local files such as `runtime/.fkey`, `runtime/.filekey`,
`runtime/.csrfkey`, `runtime/.chain_seed`, `runtime/.integrity_key`,
`runtime/integrity_manifest.json`, `runtime/cert.pem`, and `runtime/key.pem`.
Restoring a snapshot replays those files and then validates their
hashes before the restore is accepted as complete.

Server restore should be used for whole-server rollback, migration, or
cross-machine restore.

## PointsChain Ledger Recovery Boundary

PointsChain no longer supports a restorable ledger backup path. A backup restore
would overwrite append-only financial history, so the only supported recovery
model is to preserve the old event history and append corrective or governance
events.

The recovery boundary covers:

- `points_ledger`
- `points_chain_blocks`
- block signatures
- chain audit logs
- wallet state snapshot
- schema/version metadata
- forensic bundle and HMAC signature

Wallet balances are always rebuilt from ledger replay. The old `points_wallets`
balance is never trusted as source of truth.

Root can use the one-click anomaly handler from the PointsChain operations card
or `POST /api/root/points/chain/recovery/auto-handle`. That action still follows
the recovery boundary above: it first verifies the chain, returns clean status
when no incident exists, or returns a branch/governance recovery plan with the
forensic bundle reference. It never applies a backup or overwrites the live
ledger.

## Conflict Rules

- Use server snapshot restore when the whole site state must roll back together.
- Use PointsChain safe mode, forensic bundle, recovery branch, emergency
  governance, disputes, and corrective transactions when only the economy ledger
  is corrupt or tampered.
- Do not attempt a PointsChain backup restore after full server restore. If chain
  verification fails, safe mode must prepare a branch/governance recovery plan.
- Reset may create a pre-reset server snapshot, but reset itself intentionally
  creates a fresh PointsChain and audit chain.
- Snapshot restore and runtime reset have different boundaries: snapshot restore
  replays captured runtime secrets, while reset follows the key policy of the
  selected path. The development launcher `--reset` preserves server-side key
  material so server-encrypted orphan recovery remains possible.

These boundaries prevent a server snapshot from silently becoming a financial
ledger rewrite tool, and prevent wallet balances from being trusted without
ledger replay.


---

## PointsChain v2 區塊鏈化規劃 (2026-05-04 拍板, 尚未實作)

本模組未來將與全站 PointsChain v2 區塊鏈化整合：

- 工程設計：[`docs/AGENTS/research/BLOCKCHAIN/POINTSCHAIN_ENGINEERING.md`](../AGENTS/research/BLOCKCHAIN/POINTSCHAIN_ENGINEERING.md)
- 用戶白皮書：[`docs/AGENTS/research/BLOCKCHAIN/POINTSCHAIN_WHITEPAPER.md`](../AGENTS/research/BLOCKCHAIN/POINTSCHAIN_WHITEPAPER.md)
- 地址規格：[`docs/AGENTS/research/BLOCKCHAIN/POINTS_WALLET_ADDRESSING.md`](../AGENTS/research/BLOCKCHAIN/POINTS_WALLET_ADDRESSING.md)
- 轉帳 API：[`docs/AGENTS/research/BLOCKCHAIN/POINTS_TRANSFER_API.md`](../AGENTS/research/BLOCKCHAIN/POINTS_TRANSFER_API.md)
- 多簽錢包：[`docs/AGENTS/research/BLOCKCHAIN/MULTISIG_WALLETS.md`](../AGENTS/research/BLOCKCHAIN/MULTISIG_WALLETS.md)
- QA Mining / 貢獻獎勵 (Phase 7)：[`docs/AGENTS/research/BLOCKCHAIN/POINTS_MINING_REWARDS.md`](../AGENTS/research/BLOCKCHAIN/POINTS_MINING_REWARDS.md)
- QA / Release Gate：[`docs/AGENTS/research/BLOCKCHAIN/POINTSCHAIN_QA.md`](../AGENTS/research/BLOCKCHAIN/POINTSCHAIN_QA.md)

**狀態：設計已拍板（root, 2026-05-04），尚未實作完成。**
