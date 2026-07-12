# Exp6 Search Ablation Report

Exact FENs and moves are redacted. Ablations use the current locked evaluator on the same redacted failure-position set.

- JSON report: `$HACKME_RUNTIME_DIR/private/games/exp6/search_failure_ablation.json`
- Positions: `64`

| Patch | Top3 Rate | Mean Delta CP | Max Delta CP | Mean ms |
|---|---:|---:|---:|---:|
| `current_eval_capture_check_promo_ext` | 0.1250 | 237.83 | 725 | 493.5 |
| `current_eval_current_search` | 0.1094 | 244.36 | 750 | 188.6 |
| `current_eval_deeper_search_d3` | 0.1250 | 236.73 | 824 | 1414.6 |
| `current_eval_king_danger_ext` | 0.1250 | 239.36 | 750 | 224.4 |
| `current_eval_q_checks_promos` | 0.0938 | 244.94 | 750 | 213.9 |
| `current_eval_q_off` | 0.1094 | 242.97 | 725 | 160.7 |
| `current_eval_see_qfilter` | 0.0938 | 245.66 | 750 | 193.6 |

## Result

Best diagnostic patch by mean delta: `current_eval_deeper_search_d3`, but the
improvement is too small to qualify as effective:

- mean delta only improved from `244.36cp` to `236.73cp`
- top3 rate only improved from `0.1094` to `0.1250`
- mean latency rose from `188.6ms` to `1414.6ms`
- max delta became worse (`750cp` -> `824cp`)

No search patch qualifies for staged early-gate testing from this ablation.
No runtime patch was promoted.

A search patch must still pass fixed-FEN sanity and staged early gate before any full gate.
