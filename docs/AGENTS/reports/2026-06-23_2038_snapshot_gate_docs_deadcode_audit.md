# Snapshot, Gate, Docs, Dead-Code Audit

Date: 2026-06-23 20:38 Asia/Taipei
Target checkout: `/home/s92137/hackme_web_05_AI_Agent`

## Confirmed Findings And Fixes

1. **Runtime reset scope lagged behind feature growth**
   - Impact: runtime reset could leave AI Agent conversations, ComfyUI jobs/runs, Job Center tasks, media stream/HLS metadata, social/game interactions, and newer trading bot/grid runtime tables behind.
   - Fix: expanded `RESETTABLE_TABLES` in `services/snapshots/schema.py`.
   - Regression: `tests/snapshots/test_snapshots.py::test_runtime_reset_clears_expanded_feature_runtime_tables`.

2. **Production gate did not include an AI Agent boundary report**
   - Impact: AI Agent write-tool and server-filesystem boundary regressions were covered by pytest, but not as a first-class production gate report.
   - Fix: added required report type `ai_agent_boundary`, wrapper `scripts/on_live_reports/ai_agent_boundary.py`, and generator integration in `on_live_reports_make.py` and `full_generator_live_validate.py`.
   - Scope: deterministic tests only; no LLM call and no token spend.

3. **Script registry was missing maintained tooling entries**
   - Impact: `scripts/testing/transmission_copy_monitor.py` existed without `scripts/INDEX.md` ownership/artifact registration; new AI Agent boundary wrapper also needed registration.
   - Fix: updated `scripts/INDEX.md` and governance tests.

4. **Production-gate docs were stale and hard-coded to 13 reports**
   - Impact: active docs could instruct operators to run an incomplete gate after adding AI Agent capabilities.
   - Fix: updated `docs/11_QA_TESTING.md`, `docs/server_mode_v2/03_production_gate_playbook.md`, `docs/02_DEPLOY_PRODUCTION.md`, `docs/server_mode_v2/SERVER_MODE_V2_PROFILE_MATRIX.md`, `docs/architecture/DATABASE_LAYOUT.md`, `scripts/README.md`, and `scripts/on_live_reports/README.md`.
   - Historical validation reports and archived docs were left unchanged.

5. **A test still assumed launch-check frontend strings lived in `50-admin.js`**
   - Impact: false documentation/frontend consistency failure after launch-check code moved to `51-admin-server-mode-launch-check.js`.
   - Fix: updated the test to read both admin JS entry files.

## Scope Review

- Snapshot backup already covers primary DB, split DBs, `runtime/chats`, `runtime/storage`, and PointsChain forensics.
- Docs now state the expanded DB labels: auth, audit, control, storage catalog, PointsChain, trading, finance, jobs, and chess engine.
- PointsChain ledger backup/restore remains disabled by design; recovery stays under safe mode, forensic bundle, branch, and governance flows.
- `cloud_drive_security_policies`, account credential tables, and PointsChain core ledger/governance tables are intentionally not direct runtime-reset table deletes.

## Verification

- `python3 scripts/on_live_reports/ai_agent_boundary.py` -> 7 passed.
- `python3 -m pytest -q tests/scripts/security/test_on_live_reports_make_script.py` -> 13 passed.
- Targeted snapshot/gate/docs pytest -> 11 passed.
- `python3 -m py_compile services/snapshots/schema.py scripts/on_live_reports/ai_agent_boundary.py scripts/security/gate/on_live_reports_make.py scripts/security/gate/full_generator_live_validate.py` -> passed.
