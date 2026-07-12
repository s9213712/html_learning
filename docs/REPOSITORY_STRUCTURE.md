# Repository Structure And Cleanup Plan

This file defines the canonical placement logic for `hackme_web`.
It is a maintainer document, not a first-time deployer guide.

The goal is not to rename everything immediately. The goal is to make future
cleanup predictable, reviewable, and reversible.

## Design Rules

- Keep the repository root small.
- Keep `README.md` as an entry wizard, not a feature encyclopedia.
- Keep mutable runtime data outside the source checkout under the configured
  `HACKME_RUNTIME_DIR`; standalone test artifacts belong under `/tmp`.
- Keep QA reports reviewable and small. Raw generated images, video/HLS media,
  databases, logs, browser traces, and soak artifacts belong under `/tmp` or
  retained CI/release artifact storage, not `docs/AGENTS/reports/`.
- Split by bounded domain, not by tiny helper count.
- Prefer one stable canonical location per concept.
- When moving a public script or module path, keep a compatibility wrapper for
  at least one release unless the path is clearly internal.

## Root Policy

The repository root should contain only:

- entry docs: `README.md`, `SECURITY.md`
- bootstrap files: `requirements*.txt`, `server.py`, `test_for_develop.sh`
- top-level source trees: `routes/`, `services/`, `public/`
- operator and validation tree: `scripts/`
- production deployment templates: `deploy/`
- documentation and tests: `docs/`, `tests/`

The following root folders are treated as legacy runtime baggage and must not
be used for new features:

- `attachments/`
- `avatars/`
- `media/`
- `uploads/`

If they appear on a deployment host, they should be migrated into
`$HACKME_RUNTIME_DIR/storage/` or removed after confirming they are empty. Snapshot/runtime
cleanup flows must never recreate them as fresh repo-root directories.

## Docs Placement Logic

`docs/` should be organized by reader intent, not by feature age.

### Keep At Top Level

These files are canonical, short-entry or cross-cutting docs and may stay at
`docs/` root:

- `00_START_HERE.md` through `12_TROUBLESHOOTING.md`
- `README.md`
- `API_REFERENCE.md`
- `BRANCHING_AND_RELEASE.md`
- `DEPLOYMENT.md`
- `For_developer.md`
- `SECURITY.md`
- `SYSTEM_DEPENDENCIES.md`
- `UPDATE_SUMMARY.md`
- `WEB.md`
- `RELEASE_LAYOUT.md`
- `REPOSITORY_STRUCTURE.md`

### Keep In Dedicated Subtrees

- `docs/security/`: release gates, pentest, smoke, signoff
- `docs/AGENTS/`: agent rules, QA runbooks, reports
- `docs/AGENTS/research/BLOCKCHAIN/`: PointsChain and governance design set (specs only; implementation pending root authorization)
- `docs/archive/competition_2026-05-06/`: backtest and strategy competition evidence
- `docs/trading/`: trading, bot audit, risk-price, benchmark, and BTC_trade reference
- `docs/video/`: deep video/media architecture docs
- `docs/comfyui/`: ComfyUI operator docs
- `docs/ops_boundaries/`: runtime-boundary and recovery docs
- `docs/server_mode_v2/`: Server Mode v2 spec bundle
- `docs/archive/`: retired attempts, historical notes, abandoned designs

### Merge / Archive Candidates

These are useful, but they should not all remain first-class entry docs
forever:

- `SERVER_MODE_V2_*` plan files
- `docs/archive/history/VERSION_STORY.md`
- large slices of `UPDATE_SUMMARY.md`
- standalone competition reports once their conclusions are folded into one
  canonical summary

### New-Doc Rule

Before adding a new top-level doc, prefer one of these homes:

- user/admin/operator guide: extend an existing numbered guide
- low-level feature reference: extend `docs/trading/*`, `docs/video/*`,
  `docs/ops_boundaries/*`, `WEB.md`, `For_developer.md`, or `API_REFERENCE.md`
- retired historical content: `docs/archive/`
- long-form agent research that remains relevant to future work: `docs/AGENTS/research/`
- agent-only process notes: `docs/AGENTS/`

## Scripts Placement Logic

Top-level `scripts/` should contain only stable operator entrypoints and
well-known validation commands.

### Keep As Stable Operator Entry Points

- repo root `server.py`
- repo root `test_for_develop.sh`
- `scripts/prepush/pre_push_checks.py`
- `scripts/admin/root_recovery.py`

### Canonical Subtrees

- `scripts/prepush/`: pre-push framework and checks
- `scripts/trading/`: trading probes, bridges, offline validation helpers
- `scripts/comfyui/`: ComfyUI probes and helper tooling
- `scripts/admin/`: operator recovery / repair tools
- `scripts/prepush/`: pre-push framework and checks
- `scripts/security/`: pentest, production gate, server-mode smoke, and related tooling
- `scripts/trading/`: trading probes, bridges, offline validation helpers
- `scripts/comfyui/`: ComfyUI probes and helper tooling

### Recent Canonical Moves

These scripts now live in clearer subtrees:

- `scripts/trading/bridges/btc_signal_bridge.py`
- `scripts/trading/probes/backtest_20000_probe.py`
- `scripts/comfyui/feature_probe.py`
- `scripts/comfyui/comfyui_run_in_linux.template.sh`

## Tests Placement Logic

`tests/` is large because it mixes four different roles:

- API/service regression
- frontend interface checks
- script/CLI checks
- smoke / pentest / release gates

The long-term target is domain-first grouping.

### Target Layout

- `tests/frontend/`
- `tests/trading/`
- `tests/video/`
- `tests/storage/`
- `tests/security/`
- `tests/server_mode/`
- `tests/scripts/`
- `tests/contracts/`

### Consolidation Rule

Do not merge tests only to reduce file count.

Merge when all of the following are true:

- they exercise the same bounded feature
- they share setup and fixtures
- they fail for the same class of regressions
- the merged file remains readable

Do not merge when the only benefit is fewer filenames.

### Immediate Consolidation Candidates

- scattered `test_frontend_*` files should gradually become page or domain
  suites, for example account/admin, drive, trading, videos, governance
- script checks such as deploy/pentest/stress/probe/root-recovery should
  converge under `tests/scripts/`
- tiny single-purpose UI files should be merged into feature suites when they
  share the same page and fixtures

### Keep Separate

These should stay as focused suites because they protect distinct invariants:

- `test_trading_engine.py`
- `test_trading_reference_prices.py`
- `test_points_chain.py`
- `test_snapshots.py`
- `test_security_issue_regressions.py`
- `test_prepush_v2.py`

## Source Cleanup Priorities

### Highest Priority God Files

- `routes/files.py`
- `routes/comfyui.py`
- `routes/system_admin.py`
- `public/js/50-admin.js`
- `public/js/35-drive.js`
- `public/js/36-comfyui.js`
- `public/js/56-trading.js`

### Large But Cohesive; Split Carefully

- `services/trading/engine.py`
- `services/points_chain/service.py`
- `services/snapshots/server_mode.py`

## Baggage Review Rules

When deciding whether to keep or remove historical artifacts:

- Keep if it is still linked from canonical docs or still protects a regression.
- Archive if it is useful evidence but no longer active guidance.
- Remove only if it is untracked, duplicated, and no longer referenced.

The legacy binary-heavy i2i bundles already under `docs/AGENTS/reports/` are
historical migration debt. Do not load them as canonical docs or copy/scan them
in normal test gates; migrate them to artifact storage in a dedicated history
change rather than silently deleting evidence during unrelated hardening.

Do not delete history until a canonical replacement exists.

## Next Slices

1. finish the docs navigation layer so every operator can find the right file
2. normalize script naming and add compatibility wrappers when moving paths
3. regroup tests by domain in small, reviewable slices
4. continue bounded splits of route and frontend god files
