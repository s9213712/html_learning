#!/usr/bin/env python3
"""v9.4 search-hybrid: confirm whether v9.3 staged-10 regression vs
Stockfish is partly due to insufficient endgame search depth.

A/B compares each weight file with/without the endgame-only
depth-3 hybrid (triggered when piece_count <= EXP6_HYBRID_PIECE_THRESHOLD).

For each (weights, hybrid) combo: run staged-10 vs Stockfish 1-5
(2 games per depth, alternating colors).

Output: external test artifact directory plus console table.

Important: env var EXP6_HYBRID_ENDGAME_D3 is set BEFORE importing
the curriculum module so chess_exp6._resolve_search_profile picks
it up.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.games.common_paths import exp6_artifacts_root  # noqa: E402

EXP6_ARTIFACTS = exp6_artifacts_root()

WEIGHT_SETS = {
    "v6_2_S2": EXP6_ARTIFACTS / "curriculum" / "snapshots" / "chess_experiment_6_neural_stage02.npz",
    "v9_3": EXP6_ARTIFACTS / "v7_3" / "snapshots" / "v9_3_best.npz",
}
# We treat hybrid-on as a SEPARATE Python subprocess so its env var
# is clean. The subprocess just loads weights and runs staged-10.

RUNNER_SNIPPET = """
import os, sys, json
sys.path.insert(0, '{root}/scripts/games')
sys.path.insert(0, '{root}')
import chess_exp6_curriculum as cc
from pathlib import Path
w = Path('{weights}')
results = cc.play_staged_test(w)
summ = cc.score_summary(results)
sr = cc.score_rate(results)
by_depth = {{str(d): bd for d, bd in summ['by_depth'].items()}}
out = {{
    'W': summ['W'], 'D': summ['D'], 'L': summ['L'],
    'score_total': summ['total_score'],
    'score_max': summ['max_possible_score'],
    'score_rate': sr,
    'by_depth': by_depth,
    'hybrid_env': os.environ.get('EXP6_HYBRID_ENDGAME_D3', ''),
}}
print('RESULT_JSON_START')
print(json.dumps(out))
print('RESULT_JSON_END')
"""


def run_staged(weights: Path, hybrid: bool) -> dict:
    env = os.environ.copy()
    if hybrid:
        env["EXP6_HYBRID_ENDGAME_D3"] = "1"
        env["EXP6_HYBRID_PIECE_THRESHOLD"] = "10"
    else:
        env.pop("EXP6_HYBRID_ENDGAME_D3", None)
    code = RUNNER_SNIPPET.format(root=str(ROOT), weights=str(weights))
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env, capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        print(f"  [run error] stderr: {result.stderr}", flush=True)
        return {"error": result.stderr}
    # Extract JSON between markers
    out_lines = result.stdout.splitlines()
    try:
        i_start = out_lines.index("RESULT_JSON_START")
        i_end = out_lines.index("RESULT_JSON_END")
        return json.loads(out_lines[i_start + 1])
    except Exception as exc:
        print(f"  [parse error] {exc}\n  stdout: {result.stdout[-500:]}")
        return {"error": "parse failure"}


def main() -> int:
    results: dict[str, dict] = {}
    for label, weights in WEIGHT_SETS.items():
        if not weights.resolve().exists():
            print(f"missing {weights} (alt: {weights.resolve()})", flush=True)
            continue
        for hybrid in (False, True):
            key = f"{label}__{'hybrid' if hybrid else 'plain'}"
            print(f"\n=== {key}: staged-10 vs Stockfish 1-5 ===", flush=True)
            r = run_staged(weights.resolve(), hybrid)
            results[key] = r
            if "error" not in r:
                print(f"  {r['W']}W/{r['D']}D/{r['L']}L  score={r['score_total']:+d}/{r['score_max']}  norm={r['score_rate']:.2%}")
                for d, bd in sorted(r['by_depth'].items()):
                    print(f"    SF d{d}: {bd['W']}W/{bd['D']}D/{bd['L']}L  score={bd['score']:+d}")

    out_path = EXP6_ARTIFACTS / "v9_4_search_hybrid.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))

    # Summary table
    print(f"\n=== SUMMARY ===")
    print(f"  {'config':30s}  {'W/D/L':>10s}  {'score':>8s}  {'norm':>7s}")
    for key, r in results.items():
        if "error" in r:
            print(f"  {key:30s}  ERROR")
            continue
        wdl = f"{r['W']}/{r['D']}/{r['L']}"
        print(f"  {key:30s}  {wdl:>10s}  {r['score_total']:+d}/{r['score_max']:<3d}  {r['score_rate']:.2%}")
    print(f"\nsaved -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
