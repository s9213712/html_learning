# Documentation Map

This is the canonical documentation entry point. Start with one route below,
then use domain indexes for deeper reference. Archive material is evidence, not
operator guidance.

## Start Here

| Need | Read |
|---|---|
| First time in this repo | [00_START_HERE.md](00_START_HERE.md) |
| Local or staging setup | [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md) |
| Production readiness | [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md) |
| Root/admin operation | [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md) |
| End-user handbook | [04_USER_GUIDE.md](04_USER_GUIDE.md) |
| Feature map | [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md) |
| Security model | [06_SECURITY_MODEL.md](06_SECURITY_MODEL.md) |
| PointsChain | [07_POINTSCHAIN.md](07_POINTSCHAIN.md), [architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md](architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md), [architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md](architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md), [architecture/ECONOMY_LAYER_GUARDRAILS.md](architecture/ECONOMY_LAYER_GUARDRAILS.md) |
| Trading engine | [08_TRADING_ENGINE.md](08_TRADING_ENGINE.md) |
| Snapshot / reset / restore | [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md) |
| Blockchain walletization prework | [10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md](10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md) |
| QA and validation | [11_QA_TESTING.md](11_QA_TESTING.md) |
| Troubleshooting | [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md) |

## Learning Tracks

| Track | Core Docs | Deep Docs |
|---|---|---|
| Deploy | [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md), [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md), [SYSTEM_DEPENDENCIES.md](SYSTEM_DEPENDENCIES.md) | [DEPLOYMENT.md](DEPLOYMENT.md), [../deploy/README.md](../deploy/README.md), [RELEASE_LAYOUT.md](RELEASE_LAYOUT.md), [REPOSITORY_STRUCTURE.md](REPOSITORY_STRUCTURE.md) |
| Admin | [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md), [06_SECURITY_MODEL.md](06_SECURITY_MODEL.md), [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md) | [CLI_ADMIN_PLAYBOOK.md](CLI_ADMIN_PLAYBOOK.md), [security/PRE_RELEASE_CHECKLIST.md](security/PRE_RELEASE_CHECKLIST.md), [security/PRODUCTION_SIGNOFF_CHECKLIST.md](security/PRODUCTION_SIGNOFF_CHECKLIST.md) |
| Developer | [For_developer.md](For_developer.md), [API_REFERENCE.md](API_REFERENCE.md), [WEB.md](WEB.md) | [architecture/DATABASE_LAYOUT.md](architecture/DATABASE_LAYOUT.md), [architecture/ASYNC_JOB_QUEUE_FEASIBILITY.md](architecture/ASYNC_JOB_QUEUE_FEASIBILITY.md), [EXTERNAL_INTEGRATION_PLAYBOOK.md](EXTERNAL_INTEGRATION_PLAYBOOK.md), [EXTERNAL_API_COMMAND_MATRIX.md](EXTERNAL_API_COMMAND_MATRIX.md) |
| Security / QA | [11_QA_TESTING.md](11_QA_TESTING.md), [security/QA_ARCHITECTURE.md](security/QA_ARCHITECTURE.md), [SECURITY.md](SECURITY.md) | [security/FUNCTIONAL_SMOKE.md](security/FUNCTIONAL_SMOKE.md), [security/FUNCTIONAL_PERMISSION_PENTEST.md](security/FUNCTIONAL_PERMISSION_PENTEST.md), [security/PENTEST.md](security/PENTEST.md), [security/TRADING_STRESS_PENTEST.md](security/TRADING_STRESS_PENTEST.md), [security/secrets_scanning.md](security/secrets_scanning.md) |
| Release | [UPDATE_SUMMARY.md](UPDATE_SUMMARY.md), [BRANCHING_AND_RELEASE.md](BRANCHING_AND_RELEASE.md) | [ARCHIVE_INDEX.md](ARCHIVE_INDEX.md), [archive/README.md](archive/README.md) |

## Domain Indexes

| Domain | Entry |
|---|---|
| Agents / QA missions | [AGENTS/README.md](AGENTS/README.md) |
| Agent research / future specs | [AGENTS/research/README.md](AGENTS/research/README.md) |
| ComfyUI | [comfyui/README.md](comfyui/README.md) |
| Games / board AI / chess experiments | [games/README.md](games/README.md) |
| Runtime and encryption boundaries | [ops_boundaries/README.md](ops_boundaries/README.md) |
| Security runbooks | [security/QA_ARCHITECTURE.md](security/QA_ARCHITECTURE.md) |
| Server Mode v2 | [server_mode_v2/README.md](server_mode_v2/README.md), [server_mode_v2/SERVER_MODE_V2_PROFILE_MATRIX.md](server_mode_v2/SERVER_MODE_V2_PROFILE_MATRIX.md) |
| Social / profiles / friends | [social/USER_PROFILES_AND_FRIENDS.md](social/USER_PROFILES_AND_FRIENDS.md) |
| Trading | [trading/README.md](trading/README.md), [trading/TRADING.md](trading/TRADING.md) |
| Video | [video/README.md](video/README.md), [video/VIDEO_PLATFORM.md](video/VIDEO_PLATFORM.md), [video/VIDEO_STREAMING_SERVICE_TIERS.md](video/VIDEO_STREAMING_SERVICE_TIERS.md) |

## Frequently Needed Deep Links

- Board-game AI benchmark: [games/references/BOARD_AI_BENCHMARK.md](games/references/BOARD_AI_BENCHMARK.md)
- Chess model files: [games/references/chess_model_files.md](games/references/chess_model_files.md)
- Chess training pipeline: [games/references/chess_training_pipeline.md](games/references/chess_training_pipeline.md)
- Exp5 restart playbook: [games/references/exp5_restart_playbook.md](games/references/exp5_restart_playbook.md)
- Trading risk-grade prices: [trading/TRADING_RISK_PRICE.md](trading/TRADING_RISK_PRICE.md)
- Trading background engine: [trading/TRADING_BACKGROUND_ENGINE.md](trading/TRADING_BACKGROUND_ENGINE.md)
- BTC_trade integration: [trading/BTC_TRADE_INTEGRATION.md](trading/BTC_TRADE_INTEGRATION.md)
- Video streaming architecture: [video/VIDEO_STREAMING_ARCHITECTURE.md](video/VIDEO_STREAMING_ARCHITECTURE.md)
- Video streaming service tiers: [video/VIDEO_STREAMING_SERVICE_TIERS.md](video/VIDEO_STREAMING_SERVICE_TIERS.md)
- Encryption runtime boundary: [ops_boundaries/ENCRYPTION_RUNTIME_BOUNDARY.md](ops_boundaries/ENCRYPTION_RUNTIME_BOUNDARY.md)
- Runtime reset and recovery: [ops_boundaries/RUNTIME_RESET_AND_RECOVERY.md](ops_boundaries/RUNTIME_RESET_AND_RECOVERY.md)
- Nginx / systemd production templates: [../deploy/README.md](../deploy/README.md)
- Async job / Redis / queue feasibility: [architecture/ASYNC_JOB_QUEUE_FEASIBILITY.md](architecture/ASYNC_JOB_QUEUE_FEASIBILITY.md)
- Blockchain wallet identity contract: [architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md](architecture/BLOCKCHAIN_WALLET_IDENTITY_CONTRACT.md)
- PC0 dual-rail wallet model: [architecture/PC0_DUAL_RAIL_WALLET_MODEL.md](architecture/PC0_DUAL_RAIL_WALLET_MODEL.md)
- Database domain split: [architecture/DATABASE_DOMAIN_SPLIT.md](architecture/DATABASE_DOMAIN_SPLIT.md)
- Settings UI redesign notes: [architecture/SETTINGS_UI_REDESIGN_TODO.md](architecture/SETTINGS_UI_REDESIGN_TODO.md)
- Economy layer guardrails: [architecture/ECONOMY_LAYER_GUARDRAILS.md](architecture/ECONOMY_LAYER_GUARDRAILS.md)
- PointsChain financial settlement network: [architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md](architecture/POINTSCHAIN_FINANCIAL_SETTLEMENT_NETWORK.md)
- User profiles and friends: [social/USER_PROFILES_AND_FRIENDS.md](social/USER_PROFILES_AND_FRIENDS.md)
- Server mode profile matrix: [server_mode_v2/SERVER_MODE_V2_PROFILE_MATRIX.md](server_mode_v2/SERVER_MODE_V2_PROFILE_MATRIX.md)
- PointsChain v2 research: [AGENTS/research/BLOCKCHAIN/README.md](AGENTS/research/BLOCKCHAIN/README.md)
- Blockchain walletization prework: [10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md](10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md)
- Branching and release: [BRANCHING_AND_RELEASE.md](BRANCHING_AND_RELEASE.md)
- External repo / executable integration: [EXTERNAL_INTEGRATION_PLAYBOOK.md](EXTERNAL_INTEGRATION_PLAYBOOK.md)
- Update summary: [UPDATE_SUMMARY.md](UPDATE_SUMMARY.md)
- Pre-release checklist: [security/PRE_RELEASE_CHECKLIST.md](security/PRE_RELEASE_CHECKLIST.md)
- Functional smoke checklist: [security/FUNCTIONAL_SMOKE.md](security/FUNCTIONAL_SMOKE.md)
- Functional permission pentest: [security/FUNCTIONAL_PERMISSION_PENTEST.md](security/FUNCTIONAL_PERMISSION_PENTEST.md)
- Pentest guide: [security/PENTEST.md](security/PENTEST.md)

## Placement Rules

- New tutorials should extend the numbered guides or a domain `README.md`.
- Long-lived feature references belong in the relevant domain folder.
- One-off reports, old QA output, and retired experiments belong in [ARCHIVE_INDEX.md](ARCHIVE_INDEX.md).
- Do not add new top-level docs unless they are cross-cutting entry points.

## Deployment Reading Rules

- Use numbered guides and domain `README.md` files for deployment decisions.
- Treat `archive/`, `evidence/`, and one-off reports as history, not current operator guidance.
- Treat `AGENTS/research/` as future-work specification unless the referenced feature also appears in the numbered guides or API reference as implemented.
- When a feature document has both implemented and planned sections, deploy only the implemented surface and keep planned endpoints behind feature flags or release gates.

- [13_REMOTE_DOWNLOAD_TRANSMISSION.md](13_REMOTE_DOWNLOAD_TRANSMISSION.md) - Transmission / aria2 remote-download deployment settings.
