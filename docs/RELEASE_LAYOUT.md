# Release Layout

This project keeps source, documentation, test scripts, and runtime data in
separate locations so a downloaded release starts cleanly.

## Tracked Source

| Path | Purpose |
|---|---|
| `server.py` | Flask entrypoint and runtime wiring. |
| `routes/` | HTTP route modules. |
| `services/` | Domain and persistence services. |
| `public/` | Browser assets. |
| `bootstrap.schema.sql` | Bootstrap schema only. Runtime SQLite databases are not tracked. |
| `tests/` | Automated regression tests. |
| `scripts/` | Operator, validation, security, and feature probe scripts. |
| `docs/` | User, admin, security, API, deployment, and release documentation. |
| `deploy/` | Production deployment templates for Nginx, systemd, and runtime directory policy. |

## Runtime Data

Runtime data is generated on the deployment host and must not be committed.
A fresh checkout starts in `test` server mode. Chat messages, forum content,
Cloud Drive files, PointsChain ledger rows, PointsChain blocks, forensic bundles,
and audit chain rows are expected to start empty. Admin initial grants and
weekly salary jobs are not run at startup unless the operator explicitly sets
`HTML_LEARNING_BOOTSTRAP_POINTS_CHAIN=true` for a controlled test environment.

The paths below are relative to the configured external `HACKME_RUNTIME_DIR`;
they are not source-checkout paths. Without an explicit setting, local direct
launches use `$XDG_STATE_HOME/hackme_web` or `~/.local/state/hackme_web`.

| Runtime-relative path | Runtime Data |
|---|---|
| `database/database.db` | SQLite runtime database. |
| `games/models/chess_experiment.db` | 西洋棋 `experiment` 難度的獨立學習資料庫。 |
| `games/models/chess_experiment_2_nn.json` | 西洋棋 `experiment 2:nn` 難度的獨立模型檔。 |
| `storage/` | Cloud Drive user files. |
| `reports/bugs/` | User bug reports. |
| `reports/server_mode_audit/` | Server mode audit export JSON / JSONL / SHA256 bundles. |
| `chats/` | Chat sidecar logs. |
| `anchors/` | Audit/integrity anchor files. |
| `logs/` | Server and audit text logs. |
| `reports/security/` | Security, smoke, and pentest reports. |
| `reports/games/` | 西洋棋自動對弈訓練報告。 |
| `database/points_chain_backups/` | Legacy-named PointsChain forensic bundle directory. It must not contain restorable ledger backups. |
| `cert.pem`, `key.pem` | Local TLS files generated on first start. |
| `.chain_seed`, `.csrfkey`, `.fkey`, `.filekey`, `.integrity_key`, `integrity_manifest.json` | Runtime secrets and integrity state generated locally. |

Legacy repo-root folders such as `secure_backups/`, `attachments/`, `avatars/`, `media/`, and
`uploads/` are not canonical runtime homes anymore. Snapshot/reset wiring now
clears the configured runtime roots (`$HACKME_RUNTIME_DIR/storage/`,
`$HACKME_RUNTIME_DIR/chats/`)
instead of recreating those repo-root folders. Leftover legacy directories
should be treated as migration or cleanup targets, not as valid storage design.

Tracked placeholder files such as `.gitkeep` are allowed only where an empty
directory needs to exist in a fresh checkout.

## Documentation Policy

- The repository root keeps only `README.md` and GitHub-required `SECURITY.md`.
- Long-form guides live under `docs/`.
- Placement and cleanup policy lives in `docs/REPOSITORY_STRUCTURE.md`.
- Security test usage guides live under `docs/security/`.
- Historical abandoned work lives under `docs/archive/`.
- Internal scratchpad research belongs under `research/` (gitignored) and
  must not be part of release commits. Long-form research that should ship
  with the repo lives under `docs/AGENTS/research/`.

## Security Script Policy

- Executable security test scripts live under `scripts/security/`.
- Generated standalone test reports live under `/tmp/hackme_web_test_artifacts/`;
  production-gate reports attached to a deployment live under its configured runtime.
- Reports, raw responses, cookies, server output, and snapshots are local
  artifacts and should be regenerated when needed.

## Known Large Files

These files are intentionally still present but should be split in future
refactors because they are large maintenance surfaces:

- `public/index.html`
- `public/styles.css`
- `public/js/50-admin.js`
- `public/js/35-drive.js`
- `services/points_chain/service.py`
- `routes/files.py`
- `routes/community.py`
- `routes/system_admin.py`

Do not split these files during release cleanup. Split them only in dedicated
refactor branches with focused regression tests.
