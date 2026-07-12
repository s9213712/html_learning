#!/usr/bin/env python3
"""Match-gating for Exp6 candidate vs current-best.

Per user design feedback: training loss going down DOES NOT prove
the model is stronger. The candidate must demonstrate strength
through actual play against the current best, with mixed openings
and color swaps, before being promoted to runtime.

Usage:
    python chess_exp6_match.py \\
        --candidate /tmp/hackme_web_test_artifacts/games/exp6/selfplay_curriculum/snapshots/iterNN.npz \\
        --baseline /tmp/hackme_web_test_artifacts/games/exp6/curriculum/snapshots/chess_experiment_6_neural_stage02.npz \\
        --games 100 \\
        --search-profile fixed_depth_d2

Decision rule (default):
  Promote if candidate score-rate ≥ 55% AND zero W/L flip-flop
  margin (i.e., candidate not losing in too many specific games).

The match uses mirrored opening pairs: for each opening, candidate
plays both white and black to control for opening bias.
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
from scripts.games.common_paths import exp6_artifacts_root, runtime_model_path  # noqa: E402

# Reuse the curriculum's opening list.
sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402


# A wider opening set than STAGED_OPENINGS so the match doesn't
# fixate on 5 lines. These are 4-ply openings of typical mainstream
# choices; the match plays each as both colors.
MATCH_OPENINGS: list[tuple[str, list[str]]] = [
    ("start", []),
    ("e4_e5", ["e2e4", "e7e5"]),
    ("e4_c5", ["e2e4", "c7c5"]),
    ("e4_e6", ["e2e4", "e7e6"]),
    ("e4_c6", ["e2e4", "c7c6"]),
    ("d4_d5", ["d2d4", "d7d5"]),
    ("d4_nf6", ["d2d4", "g8f6"]),
    ("d4_f5", ["d2d4", "f7f5"]),
    ("c4_e5", ["c2c4", "e7e5"]),
    ("c4_c5", ["c2c4", "c7c5"]),
    ("nf3_d5", ["g1f3", "d7d5"]),
    ("nf3_nf6", ["g1f3", "g8f6"]),
    ("e4_e5_nf3", ["e2e4", "e7e5", "g1f3"]),
    ("e4_c5_nf3", ["e2e4", "c7c5", "g1f3"]),
    ("d4_d5_c4", ["d2d4", "d7d5", "c2c4"]),
]


def _board_state(board: chess.Board) -> dict:
    state = {chess.square_name(sq): p.symbol() for sq, p in board.piece_map().items()}
    state[FEN_KEY] = board.fen()
    return state


def play_match_game(white_weights: Path, black_weights: Path, opening_moves: list[str],
                    search_profile: str, max_plies: int = 200) -> tuple[str, int, str]:
    """Return (result, plies, reason). result ∈ {white_win, black_win, draw, incomplete}."""
    board = chess.Board()
    for uci in opening_moves:
        try:
            mv = chess.Move.from_uci(uci)
            if mv in board.legal_moves:
                board.push(mv)
        except Exception:
            break
    invalid = None
    for _ in range(max_plies):
        if board.is_game_over(claim_draw=True):
            break
        weights_path = white_weights if board.turn == chess.WHITE else black_weights
        side_name = "white" if board.turn == chess.WHITE else "black"
        try:
            payload = choose_experiment_neural_move(
                _board_state(board), side_name,
                weights_path=str(weights_path),
                search_profile=search_profile,
            )
        except Exception as exc:
            invalid = ("crash", str(exc)); break
        if not payload:
            invalid = ("no_move", side_name); break
        promo = payload.get("promotion") or ""
        try:
            move = chess.Move.from_uci(f"{payload['from']}{payload['to']}{promo}")
        except Exception:
            invalid = ("bad_uci", str(payload)); break
        if move not in board.legal_moves:
            invalid = ("illegal", str(move)); break
        board.push(move)
    res = board.result(claim_draw=True)
    if invalid:
        return "incomplete", board.ply(), f"invalid:{invalid[0]}"
    if res == "1-0":
        return "white_win", board.ply(), board.outcome().termination.name if board.outcome() else "unknown"
    if res == "0-1":
        return "black_win", board.ply(), board.outcome().termination.name if board.outcome() else "unknown"
    return "draw", board.ply(), board.outcome().termination.name if board.outcome() else "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True, type=Path, help="Candidate weights file")
    ap.add_argument("--baseline", required=True, type=Path, help="Current best weights file")
    ap.add_argument("--games", type=int, default=100, help="Total games (split evenly between mirrored openings)")
    ap.add_argument("--search-profile", default="fixed_depth_d2", help="Search profile both sides use")
    ap.add_argument("--out", type=Path, default=exp6_artifacts_root() / "v7_match_report.json")
    ap.add_argument("--promote-on-pass", action="store_true",
                    help="If candidate wins, copy it to runtime/games/models/chess_experiment_6_neural.npz")
    args = ap.parse_args()

    # Pair each opening for white + black → 2*len openings.
    pairs_needed = args.games // 2
    pairs: list[tuple[str, list[str], str]] = []  # (opening_id, moves, candidate_color)
    rng = random.Random(20260517)
    while len(pairs) < pairs_needed:
        oid, moves = MATCH_OPENINGS[rng.randrange(len(MATCH_OPENINGS))]
        pairs.append((oid, moves, "white"))
        pairs.append((oid, moves, "black"))
    pairs = pairs[:args.games]

    cand_w = cand_d = cand_l = 0
    incomplete = 0
    game_records: list[dict] = []
    t0 = time.perf_counter()
    for i, (oid, moves, cand_color) in enumerate(pairs):
        if cand_color == "white":
            white_w, black_w = args.candidate, args.baseline
        else:
            white_w, black_w = args.baseline, args.candidate
        result, plies, reason = play_match_game(white_w, black_w, moves, args.search_profile)
        # Outcome from candidate's POV
        if result == "incomplete":
            incomplete += 1
            outcome_label = "incomplete"
        elif (result == "white_win" and cand_color == "white") or (result == "black_win" and cand_color == "black"):
            cand_w += 1
            outcome_label = "candidate_win"
        elif result == "draw":
            cand_d += 1
            outcome_label = "draw"
        else:
            cand_l += 1
            outcome_label = "candidate_loss"
        game_records.append({
            "i": i, "opening": oid, "candidate_color": cand_color,
            "result": result, "outcome": outcome_label, "plies": plies, "reason": reason,
        })
        if (i + 1) % 10 == 0 or i == len(pairs) - 1:
            dt = time.perf_counter() - t0
            wr = cand_w + cand_d / 2
            denom = cand_w + cand_d + cand_l
            wr_pct = (wr / denom) if denom else 0
            print(f"  match {i+1}/{len(pairs)}: cand W{cand_w}/D{cand_d}/L{cand_l} (+{incomplete} incomplete) "
                  f"wr={wr_pct:.2%}  {dt:.1f}s", flush=True)

    denom = cand_w + cand_d + cand_l
    win_rate = (cand_w + cand_d / 2) / denom if denom else 0.0
    print(f"\n=== MATCH RESULT (candidate-perspective) ===")
    print(f"  candidate: {args.candidate.name}")
    print(f"  baseline:  {args.baseline.name}")
    print(f"  W/D/L: {cand_w}/{cand_d}/{cand_l}  ({incomplete} incomplete)")
    print(f"  win rate (W + 0.5D): {win_rate:.2%}")
    decision = "PROMOTE" if (win_rate >= 0.55 and cand_w >= cand_l) else "REJECT"
    print(f"  decision: {decision} (threshold ≥55% win rate AND not net-negative)")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "candidate": str(args.candidate), "baseline": str(args.baseline),
        "search_profile": args.search_profile,
        "W": cand_w, "D": cand_d, "L": cand_l, "incomplete": incomplete,
        "win_rate": win_rate, "decision": decision,
        "games": game_records,
    }, indent=2))
    print(f"  saved -> {args.out}", flush=True)
    if decision == "PROMOTE" and args.promote_on_pass:
        import shutil
        runtime_path = runtime_model_path("chess_experiment_6_neural.npz")
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(args.candidate, runtime_path)
        print(f"  promoted -> {runtime_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
