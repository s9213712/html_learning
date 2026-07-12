# Scripts Map

`scripts/` is for operator tooling, validation tooling, and subsystem-specific
helper scripts.

It is not a runtime data directory, and it should not become a dumping ground
for one-off experiments.

## Canonical Entry Points

- repo root `python3 server.py --doctor`
  Validate that the current runtime directories already exist and are writable.
  This is the required preflight before a direct `server.py` startup.
- repo root [test_for_develop.sh](../test_for_develop.sh)
  Canonical daily development launcher. It copies the repo to `/tmp` and
  starts the copied `server.py` there with development-friendly defaults.
- [testing/pytest_in_tmp.sh](testing/pytest_in_tmp.sh)
  Canonical pytest entrypoint. Tests run against a `/tmp` repo copy while
  runtime, pytest cache, bytecode, temporary files, and test artifacts stay in
  sibling directories outside that copied checkout.
- [testing/operational_soak_probe.py](testing/operational_soak_probe.py)
  Eight-hour minimum, true multi-account full-function operational simulation.
  It is destructive, requires an owned isolated target, and forces artifacts
  under the selected `/tmp` runtime. Credentials come from
  `HACKME_SOAK_ROOT_PASSWORD`, `HACKME_SOAK_MANAGER_PASSWORD`,
  `HACKME_SOAK_ACCOUNT_PASSWORD`, and `HACKME_SOAK_TEST_PASSWORD`; pass
  `HACKME_SERVER_PIDS` for production-signoff RSS evidence.
- [testing/operational_campaign_24h.py](testing/operational_campaign_24h.py)
  Canonical final operational sign-off: a primary target under continuous
  synchronized load plus a recovery target for destructive backup, restore,
  restart, wallet-incident, and governed-branch drills. Formal runs require at
  least 86,400 active seconds and write every artifact below one new `/tmp`
  campaign root. See
  [24H_OPERATIONAL_CAMPAIGN.md](../docs/AGENTS/24H_OPERATIONAL_CAMPAIGN.md).
- [security/gate/on_live_reports_make.py](security/gate/on_live_reports_make.py)
  Canonical production-gate required-report orchestrator.
- [prepush/pre_push_checks.py](prepush/pre_push_checks.py)
  Canonical local validation entrypoint.
- [admin/root_recovery.py](admin/root_recovery.py)
  Offline root recovery CLI.
- [on_live_reports/](on_live_reports/)
  Stable operator-facing compatibility wrappers for the production-gate,
  permission, pentest, server-mode, and stress tooling.
- [INDEX.md](INDEX.md)
  Mandatory registration table for maintained QA, security, pentest, stress,
  smoke, and production-gate scripts.
- [CALL_MAP.md](CALL_MAP.md)
  Operator-to-module call map for maintained script entrypoints.

## Test Artifact Policy

Repository QA, stress, release-gate, and synthetic drill outputs default to
`/tmp/hackme_web_test_artifacts`. Override that location only with an absolute
`HACKME_TEST_OUTPUT_ROOT` or an explicit script output argument. Tests must not
create `runtime/`, `artifacts/`, pytest caches, or bytecode caches in the source
checkout. Use `scripts/testing/pytest_in_tmp.sh` for every pytest invocation.

Operational scripts that read a real runtime require an explicit runtime path
or `HACKME_RUNTIME_DIR`; choosing a test artifact directory does not choose a
production runtime.

## User-Facing Progress Contract

Scripts that are expected to be run directly by an operator, deployer, tester,
or learner must print visible progress by default unless `--json` or another
machine-readable mode is explicitly selected.

Minimum console contract:

1. Print the selected target/runtime before doing work.
2. Print each major phase before it starts.
3. Print pass/fail/skip status for each check or phase.
4. Print artifact paths and temp-runtime paths.
5. On failure, print the next useful log/report path instead of only a stack
   trace or non-zero exit code.

Focused regression scripts may stay concise, but they must not call themselves
full validation and should still show which scope they covered.

## Current Subtrees

- `scripts/admin/`
  Operator repair and recovery tooling.
- `scripts/comfyui/`
  ComfyUI probe tooling and ComfyUI-specific local startup template.
- `scripts/games/`
  Chess experiment training plus non-chess board-game AI benchmarking. See
  [games/README.md](games/README.md) for the current Exp5 restart workflow.
- `scripts/media/`
  Video/HLS worker entrypoints for an explicitly selected runtime.
- `scripts/on_live_reports/`
  Stable production-report wrappers.
- `scripts/ops/`
  Runtime-explicit operational drills, exports, and tunnel helpers.
- `scripts/prepush/`
  Pre-push framework internals and checks.
- `scripts/qa/`
  Release-gate orchestration that writes evidence outside the checkout.
- `scripts/security/`
  Security gate, pentest, dependency, and server-mode validation tooling.
- `scripts/storage/`
  Remote-download and Transmission operator tooling.
- `scripts/testing/`
  Isolated pytest, Playwright, stress, capacity, and operational simulations.
- `scripts/trading/`
  Trading probes, benchmarks, validation, and integration bridges.

## Root Rule

Do not add new feature scripts directly under `scripts/` root.

New code should go into one of the existing domain subtrees unless it is a
cross-domain framework component with a clear long-term reason to live at the
top level.

## Placement Rules

The final placement policy lives in:

- [PLACEMENT_RULES.md](PLACEMENT_RULES.md)
- [INDEX.md](INDEX.md)
- [CALL_MAP.md](CALL_MAP.md)

Use `PLACEMENT_RULES.md` as the canonical rulebook for what may or may not live
under `scripts/`. Use `INDEX.md` to register maintained QA/security scripts and
to define production-gate owner, purpose, artifact, and failure meaning. Use
`CALL_MAP.md` when you need to know what a script calls and where its artifacts
land.

## Games Script Call Map

For the paused Exp5 chess experiment restart procedure, read
[games/README.md](games/README.md) and
[../docs/games/references/exp5_restart_playbook.md](../docs/games/references/exp5_restart_playbook.md)
before running long validation. Start with quick Blockfish screening; do not run
full held-out validation first unless explicitly requested.

### Board AI Benchmark

Entry:

```bash
python3 scripts/games/board_ai_benchmark.py
```

Purpose:

- Quantify Reversi, 19x19 Go, and Gomoku local AI strength.
- Run `random/easy/normal/hard` round-robin with color swaps.
- Emit standings, head-to-head matrix, Elo estimate, illegal move counts, timing, and deterministic skill probes.

Call map:

```text
scripts/games/board_ai_benchmark.py
  -> services/games/board_arena.py::run_board_ai_benchmark(...)
    -> play_board_ai_match(...)
      -> services/games/board_ai.py::choose_board_game_ai_move(...)
  -> write_board_ai_benchmark_report(...)
```

Artifact:

- `runtime/reports/games/board_ai_benchmark_*.json`

Deep tutorial:

- [../docs/games/references/BOARD_AI_BENCHMARK.md](../docs/games/references/BOARD_AI_BENCHMARK.md)

### KataGo Setup

Entry:

```bash
python3 scripts/games/setup_katago.py
```

Purpose:

- Download KataGo and the default Go neural-network model.
- Generate `runtime/katago/analysis.cfg`.
- Write `runtime/katago/hackme_katago.env` for custom runtime exports.
- Let `services/games/board_ai.py` auto-detect the default install path for the Go `katago` difficulty.

Dry run:

```bash
python3 scripts/games/setup_katago.py --dry-run
```

## Production Gate Live Regression Rule

When changing production-gate logic, do not stop at unit tests.

At minimum, QA must run:

1. `scripts/security/gate/on_live_reports_make.py` or the equivalent required-report
   generation flow against an isolated `/tmp` server.
2. A live regression proving:
   - verified `old/fake target_commit` required reports **cannot** unlock production
   - verified `current target_commit` required reports **can** unlock production

If you launch the isolated server with [test_for_develop.sh](../test_for_develop.sh),
`HTML_LEARNING_GIT_REPO_DIR` must still point at a real git repo with `.git`;
do not point it at the `/tmp` copied workspace when validating `target_commit`.
