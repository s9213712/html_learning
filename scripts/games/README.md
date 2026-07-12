# Games Scripts

This directory contains operator scripts for board-game AI benchmarking and the
chess experiment pipeline. Runtime artifacts, downloaded PGNs, full replay
JSONL, validation detail JSONL, and private question sets do not belong here.

Standalone benchmark/validation output defaults to
`/tmp/hackme_web_test_artifacts/games/`. Production models and private training
data follow an explicitly configured external `HACKME_RUNTIME_DIR`; Exp6 output
can be redirected to persistent storage with `HACKME_EXP6_OUTPUT_DIR`. No game
script should create `<repo>/runtime` or `~/exp6_output` implicitly.

## Current Chess Restart Entry

Exp5 experiments are paused at V28e. Restart from:

```text
fixed_depth_fianchetto_tail_castle_guard_v28e_depth3_no_null_mate_net30_fast_king_mobility4
```

Read first:

- `docs/games/reports/2026-05-15_exp5_v28_pause_and_restart_handoff.md`
- `docs/games/references/exp5_restart_playbook.md`

## Chess Script Chain

```text
chess_exp5_blockfish_match.py
  -> complete Exp5 vs local Stockfish/Blockfish games
  -> prints per-game progress
  -> writes private replay JSONL only to caller-selected private paths
  -> writes redacted aggregate summary when requested

chess_exp5_restart_smoke.py
  -> conservative V28e restart smoke wrapper
  -> runs compile/tests, targeted two-game Blockfish screen, and optional staged five-game screen
  -> writes artifacts under /tmp or an operator-selected private path

chess_exp5_expanded_validation.py
  -> builds/evaluates private held-out validation sets
  -> uses local Stockfish/Blockfish teacher when available
  -> docs outputs must stay redacted

chess_exp5_failure_taxonomy.py
  -> reads private validation detail
  -> emits aggregate failure taxonomy only

chess_exp5_v26_gate.py
  -> compares candidate validation summaries against a fixed baseline
  -> useful pattern for future V28/V29 gates
```

## Restart Screening Order

1. Static Python compile and focused chess tests.
2. Targeted two-game Blockfish screen.
3. Staged five-game Blockfish screen.
4. Quick percent-tail validation subset.
5. Full percent-tail or expanded-100 validation only after the fast screens pass.

Do not run the full validation first unless the user explicitly wants the long
run.

Convenience command:

```bash
PYTHONPATH=. python3 scripts/games/chess_exp5_restart_smoke.py
```

To include the staged five-game screen:

```bash
PYTHONPATH=. python3 scripts/games/chess_exp5_restart_smoke.py --run-staged
```

## Private Data Rules

Keep sensitive artifacts outside the repo, preferably under:

```text
<private-runtime-root>/games/exp5/
```

Do not commit:

- FEN/move detail
- teacher PV
- source game ids
- chosen/source moves
- full replay JSONL
- private validation detail JSONL
- exact-memory opening or move priors

## Board AI Scripts

For Reversi, Go, and Gomoku strength measurement:

```bash
python3 scripts/games/board_ai_benchmark.py
```

For local KataGo setup:

```bash
python3 scripts/games/setup_katago.py
```
