# Exp5 Restart Playbook

This playbook restarts the paused Exp5 chess-strength experiment from the
accepted V28e baseline without leaking private validation material.

## Baseline

| Field | Value |
|---|---|
| Accepted profile | `fixed_depth_fianchetto_tail_castle_guard_v28e_depth3_no_null_mate_net30_fast_king_mobility4` |
| Accepted commit | `5414b21` |
| Accepted staged result | `0W/4D/1L`, score `40%` |
| Private data root | `$EXP5_PRIVATE_ROOT` (deployment-local, outside the public repo) |
| Local Stockfish path | `$STOCKFISH_PATH` |

Stockfish/Blockfish is a local teacher and sparring opponent only. It is not a
runtime dependency for production users and must not be bundled into the repo.

## Environment

```bash
cd /path/to/hackme_web
export PROJECT_ROOT="$PWD"
export PYTHONPATH="$PROJECT_ROOT"
export STOCKFISH_PATH=/path/to/stockfish
export EXP5_PRIVATE_ROOT=/path/to/private-exp5-data
export EXP5_BASELINE_PROFILE=fixed_depth_fianchetto_tail_castle_guard_v28e_depth3_no_null_mate_net30_fast_king_mobility4
```

Before changing code:

```bash
git status --short
git log -1 --oneline
```

If the worktree contains unrelated changes, do not stage them with chess
experiment changes.

## Preflight

```bash
PYTHONPATH=. python3 -m py_compile \
  services/games/chess_nnue.py \
  scripts/games/chess_exp5_blockfish_match.py \
  scripts/games/chess_exp5_expanded_validation.py \
  scripts/games/chess_exp5_failure_taxonomy.py \
  scripts/games/chess_exp5_restart_smoke.py

PYTHONPATH=. pytest -q tests/games/test_chess_exp5_architecture.py
```

These checks only prove the wiring is intact. They do not prove strength.

## Fast Screening Order

Use this order to avoid spending a full 100-question run on a weak candidate.

Convenience wrapper:

```bash
PYTHONPATH=. python3 scripts/games/chess_exp5_restart_smoke.py
```

Add `--run-staged` when the targeted screen should continue into the five-game
Blockfish screen. Use `--dry-run` to print the exact commands without running
them.

### 1. Targeted Two-Game Collapse Screen

This checks the most informative staged openings/colors without revealing the
private move sequence in public docs.

```bash
PYTHONPATH=. python3 scripts/games/chess_exp5_blockfish_match.py \
  --profile "$EXP5_BASELINE_PROFILE" \
  --stockfish-path "$STOCKFISH_PATH" \
  --stockfish-depth-schedule 3,6 \
  --games 2 \
  --opening-ids open_game,english \
  --exp5-colors black,white \
  --max-plies 600 \
  --private-jsonl /tmp/exp5_v28e_targeted_fastgate_replay.jsonl \
  --summary-json /tmp/exp5_v28e_targeted_fastgate_summary.json
```

For a candidate, change only `--profile` and output file names.

Reject immediately if:

- A known V28e draw becomes a loss.
- Runtime explodes before completing the two games.
- The candidate creates an early tactical collapse.

### 2. Staged Five-Game Blockfish Screen

```bash
PYTHONPATH=. python3 scripts/games/chess_exp5_blockfish_match.py \
  --profile "$EXP5_BASELINE_PROFILE" \
  --stockfish-path "$STOCKFISH_PATH" \
  --stockfish-depth-schedule 2,3,4,5,6 \
  --games 5 \
  --max-plies 600 \
  --private-jsonl /tmp/exp5_v28e_blockfish_staged_5_replay.jsonl \
  --summary-json /tmp/exp5_v28e_blockfish_staged_5_summary.json
```

Acceptance floor:

- V28e baseline: `0W/4D/1L`
- Candidate floor: no worse than V28e unless the user explicitly asks to keep a
  slower diagnostic branch for research.

### 3. Quick Percent-Tail Screen

Use a small private subset first. Do not commit the subset. Do not publish
position-level detail.

Recommended output pattern:

```text
/tmp/exp5_<candidate>_percent_tail_quick_eval.json
/tmp/exp5_<candidate>_percent_tail_quick_eval_detail.jsonl
```

Public docs may summarize only:

- total positions
- clean/review+/rejected
- top1/top3/top5 aggregate rates
- section aggregate rates

### 4. Full Percent-Tail Or Expanded-100 Validation

Run this only after the fast screens pass.

Use the private question set under `$EXP5_PRIVATE_ROOT`. The public output must
remain redacted.

## Promotion Rules

Promote only if all are true:

- Staged Blockfish score beats or matches V28e.
- No known V28e draw line regresses to a loss.
- Percent-tail quick screen does not materially regress.
- Full validation, when run, shows weak-slice improvement or no regression.
- Runtime remains practical.
- The patch is generic and does not memorize validation data.

Rejected experiments should be documented with:

- profile name
- what changed
- targeted screen result
- staged result if run
- exact rejection reason
- whether the code was reverted

## Private/Public Artifact Policy

Private only:

- FEN
- moves
- teacher PV
- chosen/source moves
- source game ids
- per-question answers
- full replay JSONL
- validation detail JSONL

Public allowed:

- aggregate counts
- win/draw/loss summaries
- section-level clean/review+/rejected rates
- redacted failure taxonomy
- high-level technical diagnosis

## Current Next Patch Shape

Preferred next patch:

- small bounded mate-net verifier
- cache repeated tactical checks
- root-only or near-root trigger
- preserve defensive candidates without global search deepening

Avoid:

- broad king-pressure evaluator
- broad pawn-structure evaluator
- global qdepth increase
- always-on low-legal pressure
- validation-set priors or exact-memory lookup
- runtime Stockfish calls
