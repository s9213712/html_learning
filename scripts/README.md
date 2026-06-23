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
  Canonical pytest entrypoint. Tests must run against a `/tmp` repo copy.
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
- `scripts/prepush/`
  Pre-push framework internals and checks.
- `scripts/security/`
  Security gate, pentest, dependency, and server-mode validation tooling.
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
