#!/usr/bin/env python3
"""v9.5 active-failure data generation.

Pipeline:
1. Play N self-play games of v9.3 vs Stockfish at depths 1-5 (mixed,
   alternating colors, varied openings). For each game record:
   - per-move (fen, side_to_move, v9.3_score, played_uci)
   - terminal outcome
2. Mine failure positions per user spec:
   - all positions from games where v9.3 lost
   - positions where v9.3's score dropped sharply (>200 cp single ply)
   - positions where Stockfish best move differs from v9.3 chosen move
   - last 2-4 plies before mate/blunder
3. Re-label failure positions at Stockfish depth 6 (deeper than the
   training depth-4 baseline).
4. Save under the configured external Exp6 artifact root.
   Schema: {game_idx, fen, cp_white_d6, outcome_white, played_move,
            stockfish_best_move, failure_reason, source_game_result}

This file feeds chess_exp6_v9_5_train.py (next script).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.chess import FEN_KEY  # noqa: E402
from services.games.chess_exp6 import choose_experiment_neural_move  # noqa: E402
from services.games.chess_neural import NeuralEvaluator, load_weights  # noqa: E402
from services.games.chess_search import ZobristHasher, search_best_move  # noqa: E402
from services.games.chess_exp6 import _move_order_score, _SEARCH_PROFILES  # noqa: E402
from services.games.chess_stockfish_teacher import (  # noqa: E402
    UciStockfish, analysis_limit, resolve_stockfish_path,
)
from scripts.games.common_paths import exp6_artifacts_root, exp6_private_dir  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402


OUT_PATH = exp6_private_dir() / "v9_5_failure_positions.jsonl"
STAGED_OPENINGS = cc.STAGED_OPENINGS  # ('start', 'open_game', 'queen_pawn', 'sicilian', 'english')
ENGINE_SEARCH_PROFILE = "balanced"
RELABEL_DEPTH = 6
EVAL_DROP_THRESHOLD_CP = 200          # one-ply cp drop ≥ this → failure
PRE_LOSS_PLIES = 4                    # last N plies of a lost game


def play_game_vs_stockfish(weights, sf_engine, exp6_color: str, opening_moves: list[str],
                            sf_depth: int, max_plies: int = 200):
    """Play one game v9.3 vs Stockfish. Returns list of move records + outcome."""
    evaluator = NeuralEvaluator(weights)
    hasher = ZobristHasher(seed=20260601)
    profile = _SEARCH_PROFILES[ENGINE_SEARCH_PROFILE]
    exp6_color_w = (exp6_color == "white")
    board = chess.Board()
    for uci in opening_moves:
        try:
            mv = chess.Move.from_uci(uci)
            if mv in board.legal_moves:
                board.push(mv)
        except Exception:
            break
    moves: list[dict] = []
    invalid = None
    for ply in range(max_plies):
        if board.is_game_over(claim_draw=True):
            break
        is_exp6_turn = (board.turn == chess.WHITE) == exp6_color_w
        if is_exp6_turn:
            # v9.3's own search
            try:
                result = search_best_move(
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
            except Exception as exc:
                invalid = f"exp6_crash:{exc}"; break
            if result.best_move is None or result.best_move not in board.legal_moves:
                invalid = "exp6_no_move"; break
            move = result.best_move
            moves.append({
                "fen": board.fen(),
                "stm_is_white": (board.turn == chess.WHITE),
                "engine": "exp6",
                "score_stm": int(result.score),
                "played_uci": move.uci(),
            })
            board.push(move)
        else:
            # Stockfish
            limit = analysis_limit(depth=sf_depth, movetime_ms=0)
            try:
                pv = sf_engine.analyse(board, limit=limit, multipv=1)
            except Exception as exc:
                invalid = f"sf_crash:{exc}"; break
            if not pv:
                invalid = "sf_no_move"; break
            best_uci = pv[0].get("pv", [None])[0]
            if not best_uci:
                invalid = "sf_no_pv"; break
            try:
                move = chess.Move.from_uci(best_uci)
            except Exception:
                invalid = "sf_bad_uci"; break
            if move not in board.legal_moves:
                invalid = "sf_illegal"; break
            moves.append({
                "fen": board.fen(),
                "stm_is_white": (board.turn == chess.WHITE),
                "engine": "stockfish",
                "score_stm": int(pv[0].get("teacher_eval_cp", 0) or 0),
                "played_uci": best_uci,
            })
            board.push(move)
    res = board.result(claim_draw=True)
    if invalid:
        outcome = "incomplete"
    elif res == "1-0":
        outcome = "white_win"
    elif res == "0-1":
        outcome = "black_win"
    else:
        outcome = "draw"
    # Outcome from v9.3's POV
    if outcome == "incomplete":
        exp6_outcome = "incomplete"
    elif (outcome == "white_win" and exp6_color_w) or (outcome == "black_win" and not exp6_color_w):
        exp6_outcome = "exp6_win"
    elif outcome == "draw":
        exp6_outcome = "draw"
    else:
        exp6_outcome = "exp6_loss"
    return {
        "moves": moves,
        "exp6_color": exp6_color,
        "sf_depth": sf_depth,
        "exp6_outcome": exp6_outcome,
        "termination_reason": invalid or res,
        "plies": board.ply(),
    }


def is_failure_position(prev_move: dict | None, this_move: dict, game: dict, idx: int) -> tuple[bool, str]:
    """Return (is_failure, reason).
    Failure criteria (per user spec):
      1. game was an exp6 loss → all exp6 moves are failures (or pre-loss tail)
      2. exp6's score dropped > THRESHOLD cp in one ply
      3. (deferred: exp6 chose different move from Stockfish best)
      4. last PRE_LOSS_PLIES plies before a loss
    """
    if this_move["engine"] != "exp6":
        return False, ""
    # Rule 1: lost game pre-loss tail
    if game["exp6_outcome"] == "exp6_loss":
        # Count this move's distance from end
        exp6_moves_idx = [i for i, m in enumerate(game["moves"]) if m["engine"] == "exp6"]
        if idx in exp6_moves_idx[-PRE_LOSS_PLIES:]:
            return True, "pre_loss"
    # Rule 2: large score drop
    if prev_move is not None and prev_move["engine"] == "exp6":
        # cp_score_stm dropped — convert to common POV (white)
        prev_cp_w = prev_move["score_stm"] * (1 if prev_move["stm_is_white"] else -1)
        cur_cp_w = this_move["score_stm"] * (1 if this_move["stm_is_white"] else -1)
        if prev_move["stm_is_white"]:
            drop = prev_cp_w - cur_cp_w
        else:
            drop = cur_cp_w - prev_cp_w
        if drop > EVAL_DROP_THRESHOLD_CP:
            return True, f"eval_drop_{int(drop)}cp"
    return False, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", type=Path,
                    default=exp6_artifacts_root() / "v7_3" / "snapshots" / "v9_3_best.npz",
                    help="Engine under test (default v9.3 best)")
    ap.add_argument("--games-per-depth", type=int, default=10,
                    help="Games per Stockfish depth (1-5). Default 10 → 50 games total.")
    ap.add_argument("--depths", type=str, default="1,2,3,4,5",
                    help="Comma-separated Stockfish depths to test")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--seed", type=int, default=20260518)
    args = ap.parse_args()

    if not args.weights.exists():
        print(f"missing {args.weights}")
        return 1
    weights = load_weights(args.weights)
    sf_path = resolve_stockfish_path()
    if not sf_path:
        print("Stockfish not found")
        return 2

    depths = [int(d) for d in args.depths.split(",")]
    rng = random.Random(args.seed)

    games: list[dict] = []
    t0 = time.perf_counter()
    n_games = args.games_per_depth * len(depths)
    print(f"playing {n_games} v9.3-vs-Stockfish games ({args.games_per_depth}/depth × {len(depths)} depths)", flush=True)
    with UciStockfish(sf_path) as sf:
        for i in range(n_games):
            d = depths[i % len(depths)]
            color = "white" if (i // len(depths)) % 2 == 0 else "black"
            opening_id, opening_moves = STAGED_OPENINGS[rng.randrange(len(STAGED_OPENINGS))]
            g = play_game_vs_stockfish(weights, sf, color, opening_moves, d)
            g["i"] = i
            g["opening_id"] = opening_id
            games.append(g)
            if (i + 1) % 10 == 0 or i == n_games - 1:
                outcomes = [x["exp6_outcome"] for x in games]
                w = outcomes.count("exp6_win"); d_ = outcomes.count("draw"); l = outcomes.count("exp6_loss"); incomplete = outcomes.count("incomplete")
                print(f"  game {i+1}/{n_games}: W{w}/D{d_}/L{l} (+{incomplete} incomplete) "
                      f"{time.perf_counter()-t0:.1f}s", flush=True)

    # Mine failures
    failure_positions: list[dict] = []
    for g in games:
        for idx, m in enumerate(g["moves"]):
            prev = g["moves"][idx - 1] if idx > 0 else None
            ok, reason = is_failure_position(prev, m, g, idx)
            if ok:
                failure_positions.append({
                    "game_i": g["i"],
                    "opening_id": g["opening_id"],
                    "sf_depth": g["sf_depth"],
                    "exp6_color": g["exp6_color"],
                    "exp6_outcome": g["exp6_outcome"],
                    "fen": m["fen"],
                    "stm_is_white": m["stm_is_white"],
                    "played_uci": m["played_uci"],
                    "score_stm_v93": m["score_stm"],
                    "failure_reason": reason,
                })

    print(f"\nmined {len(failure_positions)} failure positions from {n_games} games", flush=True)
    # Dedup by FEN (same position can appear in multiple games)
    seen_fens: set[str] = set()
    dedup_failures: list[dict] = []
    for p in failure_positions:
        if p["fen"] in seen_fens:
            continue
        seen_fens.add(p["fen"])
        dedup_failures.append(p)
    print(f"  after FEN dedup: {len(dedup_failures)}", flush=True)

    # Re-label at Stockfish depth 6
    print(f"\nre-labeling {len(dedup_failures)} positions at Stockfish depth {RELABEL_DEPTH}...", flush=True)
    t0 = time.perf_counter()
    n_labeled = 0
    n_skipped = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with UciStockfish(sf_path) as sf, args.out.open("w") as out_f:
        for i, p in enumerate(dedup_failures):
            board = chess.Board(p["fen"])
            try:
                pv = sf.analyse(board, limit=analysis_limit(depth=RELABEL_DEPTH, movetime_ms=0), multipv=1)
            except Exception:
                n_skipped += 1
                continue
            if not pv:
                n_skipped += 1
                continue
            cp = pv[0].get("teacher_eval_cp")
            if cp is None:
                n_skipped += 1
                continue
            cp_white = float(cp) if board.turn == chess.WHITE else -float(cp)
            sf_best = pv[0].get("pv", [None])[0]
            # Determine outcome_white for this game
            outcome_map = {"exp6_win": 1.0 if p["exp6_color"] == "white" else -1.0,
                           "exp6_loss": -1.0 if p["exp6_color"] == "white" else 1.0,
                           "draw": 0.0, "incomplete": 0.0}
            out_white = outcome_map.get(p["exp6_outcome"], 0.0)
            out_f.write(json.dumps({
                "fen": p["fen"],
                "cp_white": cp_white,
                "outcome_white": out_white,
                "stockfish_best_move": sf_best,
                "exp6_played_move": p["played_uci"],
                "exp6_score_stm": p["score_stm_v93"],
                "failure_reason": p["failure_reason"],
                "sf_depth": p["sf_depth"],
                "exp6_outcome": p["exp6_outcome"],
                "exp6_color": p["exp6_color"],
                "label_depth": RELABEL_DEPTH,
            }) + "\n")
            n_labeled += 1
            if n_labeled % 100 == 0:
                dt = time.perf_counter() - t0
                print(f"  re-labeled {n_labeled}/{len(dedup_failures)} ({dt:.1f}s)", flush=True)

    print(f"\ndone: {n_labeled} re-labeled, {n_skipped} skipped → {args.out}", flush=True)

    # Summary stats
    outcomes = [g["exp6_outcome"] for g in games]
    w = outcomes.count("exp6_win"); d = outcomes.count("draw"); l = outcomes.count("exp6_loss"); inc = outcomes.count("incomplete")
    print(f"\n=== game summary ===")
    print(f"  total: {len(games)} games, W{w}/D{d}/L{l} (+{inc} incomplete)")
    by_depth: dict[int, list] = {}
    for g in games:
        by_depth.setdefault(g["sf_depth"], []).append(g["exp6_outcome"])
    for dd in sorted(by_depth.keys()):
        outs = by_depth[dd]
        ww = outs.count("exp6_win"); dd_ = outs.count("draw"); ll = outs.count("exp6_loss")
        print(f"  SF d{dd}: W{ww}/D{dd_}/L{ll}  ({len(outs)} games)")

    reason_counts: dict[str, int] = {}
    for p in failure_positions:
        reason_counts[p["failure_reason"]] = reason_counts.get(p["failure_reason"], 0) + 1
    print(f"\n=== failure reason breakdown ===")
    for k, v in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
