#!/usr/bin/env python3
"""Depth-3 feasibility study for Exp6 v9 / future architecture.

Measures (without training):
1. Per-move wall-clock at depth 2 vs depth 3 (with quiescence)
2. Position-count distribution of plies in real games (proxy for
   when depth-3 might be triggered)
3. 30-game match estimated total time
4. Endgame-only depth-3 viability: when piece count < N, use d3;
   measure how often this fires and resulting average per-move time
5. Page latency impact: 95th percentile per-move latency
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from statistics import mean, median, quantiles

import chess

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.chess_neural import NeuralEvaluator, load_weights  # noqa: E402
from services.games.chess_search import ZobristHasher, search_best_move  # noqa: E402
from services.games.chess_exp6 import _move_order_score, _SEARCH_PROFILES  # noqa: E402
from scripts.games.common_paths import exp6_artifacts_root, runtime_model_path  # noqa: E402

CHAMPION_PATH = runtime_model_path("chess_experiment_6_neural.npz")


def benchmark_one_position(weights, board: chess.Board, profile_name: str) -> float:
    """Return wall-clock seconds for one search."""
    evaluator = NeuralEvaluator(weights)
    hasher = ZobristHasher(seed=20260601)
    profile = _SEARCH_PROFILES[profile_name]
    t0 = time.perf_counter()
    search_best_move(
        board,
        max_depth=int(profile["depth"]),
        evaluate=evaluator,
        move_order_fn=_move_order_score,
        quiescence_depth=int(profile["quiescence_depth"]),
        hasher=hasher,
        time_budget_ms=profile.get("time_budget_ms"),
        enable_pvs=bool(profile.get("enable_pvs")),
        enable_lmr=bool(profile.get("enable_lmr")),
        enable_null_move=bool(profile.get("enable_null_move")),
        enable_futility=bool(profile.get("enable_futility")),
    )
    return time.perf_counter() - t0


def main() -> int:
    weights = load_weights(CHAMPION_PATH)

    # Sample diverse positions: opening, middlegame, endgame
    samples_by_phase = {
        "opening_1.e4_e5": (chess.Board(), ["e2e4", "e7e5"]),
        "opening_1.d4_d5": (chess.Board(), ["d2d4", "d7d5"]),
        "middlegame_kingside_attack": (chess.Board(
            "r1bqr1k1/pppn1ppp/3p1n2/4p3/2P1P3/2N2N2/PP1PBPPP/R1BQ1RK1 w - - 0 8"
        ), []),
        "middlegame_pawn_chains": (chess.Board(
            "r2qrbk1/3n1pp1/p2bpn1p/1p1p4/3P4/PNN1PP2/1PQ1B1PP/R1B1R1K1 w - - 0 14"
        ), []),
        "endgame_KRP_vs_KR": (chess.Board(
            "8/8/4k3/4P3/4K3/8/8/3R4 w - - 0 1"
        ), []),
        "endgame_KQ_vs_KR": (chess.Board(
            "8/8/4k3/8/8/4K3/2Q5/3r4 w - - 0 1"
        ), []),
        "endgame_pawn_break": (chess.Board(
            "8/p7/1p6/2p5/3P4/4P3/PPP5/8 w - - 0 1"
        ), []),
    }
    boards = {}
    for name, (board, opening) in samples_by_phase.items():
        b = board.copy()
        for uci in opening:
            b.push_uci(uci)
        boards[name] = b

    # Warmup
    print("[warmup]", flush=True)
    for _ in range(3):
        benchmark_one_position(weights, chess.Board(), "balanced")

    # Per-position benchmark
    results: dict[str, dict] = {}
    for name, board in boards.items():
        piece_count = len(board.piece_map())
        d2_times = []
        d3_times = []
        for _ in range(5):
            d2_times.append(benchmark_one_position(weights, board, "fixed_depth_d2"))
            d3_times.append(benchmark_one_position(weights, board, "fixed_depth_d3"))
        ratio = mean(d3_times) / mean(d2_times) if mean(d2_times) > 0 else float("inf")
        results[name] = {
            "piece_count": piece_count,
            "fen": board.fen(),
            "d2_median_ms": median(d2_times) * 1000,
            "d3_median_ms": median(d3_times) * 1000,
            "d2_mean_ms": mean(d2_times) * 1000,
            "d3_mean_ms": mean(d3_times) * 1000,
            "ratio_d3_over_d2": ratio,
        }
        print(f"  {name}: pieces={piece_count}  d2_med={results[name]['d2_median_ms']:.0f}ms  "
              f"d3_med={results[name]['d3_median_ms']:.0f}ms  ratio={ratio:.1f}x", flush=True)

    # Aggregate
    d2_all = [r["d2_median_ms"] for r in results.values()]
    d3_all = [r["d3_median_ms"] for r in results.values()]
    avg_d2 = mean(d2_all)
    avg_d3 = mean(d3_all)
    avg_ratio = mean([r["ratio_d3_over_d2"] for r in results.values()])

    print(f"\n=== AGGREGATE ===")
    print(f"  d2 median across phases: {avg_d2:.0f} ms")
    print(f"  d3 median across phases: {avg_d3:.0f} ms")
    print(f"  d3/d2 ratio (avg):       {avg_ratio:.1f}x")

    # Match estimate: 30 games × ~80 plies × 2 engines × 1 move/ply
    plies_per_game = 80
    moves_per_game = plies_per_game  # one side moves per ply
    games = 30
    d2_match_seconds = games * moves_per_game * (avg_d2 / 1000)
    d3_match_seconds = games * moves_per_game * (avg_d3 / 1000)
    print(f"\n  30-game match estimate (~80 plies each):")
    print(f"    depth-2:  {d2_match_seconds/60:.1f} min")
    print(f"    depth-3:  {d3_match_seconds/60:.1f} min")

    # Page latency
    print(f"\n  page latency (per AI move):")
    print(f"    depth-2:  median {median(d2_all):.0f} ms, max {max(d2_all):.0f} ms")
    print(f"    depth-3:  median {median(d3_all):.0f} ms, max {max(d3_all):.0f} ms")
    if max(d3_all) < 1500:
        print(f"    -> depth-3 max < 1500 ms: acceptable web latency")
    elif max(d3_all) < 3000:
        print(f"    -> depth-3 max in 1500-3000 ms: noticeable but tolerable")
    else:
        print(f"    -> depth-3 max > 3000 ms: too slow for live web play")

    # Endgame-only depth-3
    eg_results = [r for r in results.values() if r["piece_count"] <= 10]
    mg_results = [r for r in results.values() if r["piece_count"] > 10]
    if eg_results:
        eg_d2 = mean([r["d2_median_ms"] for r in eg_results])
        eg_d3 = mean([r["d3_median_ms"] for r in eg_results])
        print(f"\n  ENDGAME-ONLY ANALYSIS (piece_count ≤ 10, {len(eg_results)} positions):")
        print(f"    d2 median: {eg_d2:.0f} ms")
        print(f"    d3 median: {eg_d3:.0f} ms")
        print(f"    d3 ratio:  {eg_d3/eg_d2:.1f}x")
    if mg_results:
        mg_d2 = mean([r["d2_median_ms"] for r in mg_results])
        mg_d3 = mean([r["d3_median_ms"] for r in mg_results])
        print(f"\n  MIDDLEGAME ANALYSIS (piece_count > 10, {len(mg_results)} positions):")
        print(f"    d2 median: {mg_d2:.0f} ms")
        print(f"    d3 median: {mg_d3:.0f} ms")
        print(f"    d3 ratio:  {mg_d3/mg_d2:.1f}x")

    out_path = exp6_artifacts_root() / "v9_5_depth3_feasibility.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "per_position": results,
        "aggregate": {
            "d2_median_ms_avg": avg_d2,
            "d3_median_ms_avg": avg_d3,
            "ratio_d3_over_d2": avg_ratio,
            "match_30g_d2_minutes": d2_match_seconds / 60,
            "match_30g_d3_minutes": d3_match_seconds / 60,
        },
    }, indent=2))
    print(f"\nsaved -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
