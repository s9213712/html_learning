#!/usr/bin/env python3
"""Compare NN-depth-2 vs NN-depth-3 search profile for a given Exp6
snapshot. Architecture-level test post v5: hyperparameter tuning
(v3/v4/v5) couldn't break the +6 staged-10 ceiling despite Stockfish-
quality training labels and clean loss convergence. Hypothesis: depth-2
search can't leverage the learned eval. Depth-3 should give the NN's
eval more material to act on.

Usage:
    python scripts/games/chess_exp6_depth3_comparison.py \\
        --weights /tmp/hackme_web_test_artifacts/games/exp6/curriculum/snapshots/chess_experiment_6_neural_stage10.npz \\
        --baseline-weights /tmp/hackme_web_test_artifacts/games/exp6/curriculum/snapshots/chess_experiment_6_neural_stage00_init.npz \\
        --games-per-depth 10

Outputs JSON under the external Exp6 artifact root and writes progress to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.games.common_paths import exp6_artifacts_root  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402


import services.games.chess_exp6 as _exp6_mod  # noqa: E402
_REAL_CHOOSE = _exp6_mod.choose_experiment_neural_move


def patch_profile(profile_name: str) -> None:
    """Monkey-patch curriculum's hardcoded 'balanced' profile to use
    the given profile name. Uses the module-level function reference
    saved BEFORE any patching to avoid recursion."""
    def _patched(state, color, search_profile=None):
        return _REAL_CHOOSE(state, color, search_profile=profile_name)
    cc.choose_experiment_neural_move = _patched


def restore_profile() -> None:
    cc.choose_experiment_neural_move = _REAL_CHOOSE


def run_one(label: str, weights_path: Path, games_per_depth: int) -> dict:
    print(f"[{label}] running {games_per_depth*len(cc.STAGED_DEPTHS)} games ({weights_path.name})...", flush=True)
    t0 = time.perf_counter()
    res = cc.play_staged_test(weights_path, games_per_depth=games_per_depth)
    dt = time.perf_counter() - t0
    summ = cc.score_summary(res)
    norm = (summ["total_score"] - summ["min_possible_score"]) / (summ["max_possible_score"] - summ["min_possible_score"]) * 100
    print(
        f"[{label}] {dt:.1f}s: {summ['W']}W/{summ['D']}D/{summ['L']}L "
        f"score={summ['total_score']:+d}/{summ['max_possible_score']} (norm={norm:.2f}%)",
        flush=True,
    )
    for d in sorted(summ["by_depth"].keys()):
        bd = summ["by_depth"][d]
        print(f"    depth {d}: {bd['W']}W/{bd['D']}D/{bd['L']}L score={bd['score']:+3d}/{cc.SCORE_WIN*bd['games']}")
    return {
        "games": summ["games"], "W": summ["W"], "D": summ["D"], "L": summ["L"],
        "score_total": summ["total_score"], "score_max": summ["max_possible_score"],
        "norm_pct": norm, "time_s": dt,
        "by_depth": {str(d): bd for d, bd in summ["by_depth"].items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True, type=Path, help="Trained snapshot to test")
    ap.add_argument("--baseline-weights", required=True, type=Path, help="Random-init baseline snapshot")
    ap.add_argument("--games-per-depth", type=int, default=10, help="Games per Stockfish depth (default 10 = 50 total)")
    ap.add_argument("--out", type=Path, default=exp6_artifacts_root() / "v5_depth3_comparison.json")
    args = ap.parse_args()

    out: dict = {"games_per_depth": args.games_per_depth, "total_games": args.games_per_depth * len(cc.STAGED_DEPTHS), "results": {}}

    # Pass 1: depth-2 (balanced) — sanity-check matches staged-5 results
    patch_profile("balanced")
    print("\n=== DEPTH-2 (balanced) ===\n", flush=True)
    out["results"]["depth2_baseline"] = run_one("depth2_baseline", args.baseline_weights, args.games_per_depth)
    print()
    out["results"]["depth2_cand"] = run_one("depth2_cand", args.weights, args.games_per_depth)

    # Pass 2: depth-3 (fixed_depth_d3)
    patch_profile("fixed_depth_d3")
    print("\n=== DEPTH-3 (fixed_depth_d3) ===\n", flush=True)
    out["results"]["depth3_baseline"] = run_one("depth3_baseline", args.baseline_weights, args.games_per_depth)
    print()
    out["results"]["depth3_cand"] = run_one("depth3_cand", args.weights, args.games_per_depth)

    restore_profile()

    d2 = out["results"]["depth2_cand"]["score_total"] - out["results"]["depth2_baseline"]["score_total"]
    d3 = out["results"]["depth3_cand"]["score_total"] - out["results"]["depth3_baseline"]["score_total"]
    out["delta_depth2"] = d2
    out["delta_depth3"] = d3
    print(f"\n=== SUMMARY ===")
    print(f"  depth-2 Δ (cand - baseline) = {d2:+d}")
    print(f"  depth-3 Δ (cand - baseline) = {d3:+d}")
    if d3 > d2:
        print(f"  -> depth-3 amplifies training benefit by {d3 - d2:+d} score points")
    elif d3 < d2:
        print(f"  -> depth-3 fails to leverage training (worse by {d2 - d3} points)")
    else:
        print(f"  -> depth-3 same as depth-2")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2))
    print(f"\nsaved -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
