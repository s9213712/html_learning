#!/usr/bin/env python3
"""Horizontal strength comparison: run the same Exp6 curriculum
``staged_test`` (depth 1-5, 2 games per depth, score +4/-1/-4) across
all available Exp1-Exp6 difficulties plus normal / hard.

Each engine plays 10 games against Stockfish:

- d1 white, d1 black, d2 white, d2 black, ... d5 white, d5 black

Per-game records: result, reason, plies, wall-clock, per-side
decision time statistics, per-process CPU/RSS deltas. Aggregate
per-engine score and per-depth W/D/L are written under the external Exp6
artifact root and mirrored to the configured external runtime.

Engines dispatched via ``routes.games.choose_computer_move`` so each
gets the same configuration the live web app uses. The Exp6 entry
uses the bundled-seed (random-init) weights so this comparison
matches the curriculum's stage 00 baseline number exactly.
"""

from __future__ import annotations

import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import chess  # noqa: E402

from services.games.chess import FEN_KEY  # noqa: E402
from services.games.chess_stockfish_teacher import (  # noqa: E402
    UciStockfish, analysis_limit, resolve_stockfish_path,
)
from routes import games as games_routes  # noqa: E402
from scripts.games.common_paths import exp6_artifacts_root, exp6_private_dir  # noqa: E402


STOCKFISH_BIN = resolve_stockfish_path(os.environ.get("STOCKFISH_PATH", ""))
OUT_DIR = exp6_artifacts_root() / "engine_comparison"
REPORT_JSON = OUT_DIR / "engine_comparison.json"
REPORT_MD = OUT_DIR / "engine_comparison.md"
REPORT_JSON_RUNTIME = exp6_private_dir() / "engine_comparison.json"
REPORT_MD_RUNTIME = exp6_private_dir() / "engine_comparison.md"

STAGED_OPENINGS = [
    ("start", []),
    ("open_game", ["e2e4", "e7e5"]),
    ("queen_pawn", ["d2d4", "d7d5"]),
    ("sicilian", ["e2e4", "c7c5"]),
    ("english", ["c2c4", "g8f6"]),
]
STAGED_DEPTHS = [1, 2, 3, 4, 5]
STAGED_GAMES_PER_DEPTH = 2
SCORE_WIN = 4
SCORE_DRAW = -1
SCORE_LOSS = -4

# Engines under test. Keys are the routes-level difficulty strings
# accepted by ``choose_computer_move``.
ENGINE_DIFFICULTIES = [
    # Exp0 = 2-ply material minimax (renamed from "hard"). Backend
    # dispatcher resolves "experiment 0:minimax2ply" → the legacy
    # heuristic code, so passing the new string runs the same engine.
    "experiment 0:minimax2ply",
    # Exp1 = engine search + learning store (renamed from
    # "experiment"). The dispatcher accepts the new name and falls
    # through to the legacy ``choose_experiment_move`` body.
    "experiment 1:search",
    "experiment 2:nn",
    "experiment 3:dl",
    "experiment 4:pv",
    "experiment 5:nnue",
    "experiment 6:neuralnet",
]


def _state_from_board(board: chess.Board) -> dict:
    state = {chess.square_name(sq): p.symbol() for sq, p in board.piece_map().items()}
    state[FEN_KEY] = board.fen()
    return state


def _engine_move(board: chess.Board, difficulty: str) -> chess.Move | None:
    """Dispatch through ``routes.games.choose_computer_move`` so each
    engine uses the same code path the web app uses for real play."""
    side = "white" if board.turn == chess.WHITE else "black"
    state = _state_from_board(board)
    move_history = [{"uci": m.uci()} for m in board.move_stack]
    payload = games_routes.choose_computer_move(
        state, side, difficulty=difficulty, move_history=move_history,
    )
    if not payload:
        return None
    promo = payload.get("promotion") or ""
    try:
        return chess.Move.from_uci(f"{payload['from']}{payload['to']}{promo}")
    except Exception:
        return None


def play_one_game(opening_id, opening_moves, engine_color_name, difficulty,
                  stockfish_depth, stockfish_engine, max_plies=400):
    board = chess.Board()
    for u in opening_moves:
        board.push_uci(u)
    engine_color = chess.WHITE if engine_color_name == "white" else chess.BLACK
    engine_decision_seconds = []
    sf_decision_seconds = []
    rusage0 = resource.getrusage(resource.RUSAGE_SELF)
    wall0 = time.perf_counter()
    invalid_actor = None
    for _ply in range(max_plies):
        if board.is_game_over(claim_draw=True):
            break
        if board.turn == engine_color:
            t_move = time.perf_counter()
            move = _engine_move(board, difficulty)
            engine_decision_seconds.append(time.perf_counter() - t_move)
            if not move or move not in board.legal_moves:
                invalid_actor = "engine"; break
        else:
            t_move = time.perf_counter()
            try:
                pv = stockfish_engine.analyse(
                    board,
                    limit=analysis_limit(depth=stockfish_depth, movetime_ms=0),
                    multipv=1,
                )
            except Exception:
                sf_decision_seconds.append(time.perf_counter() - t_move)
                invalid_actor = "stockfish"; break
            sf_decision_seconds.append(time.perf_counter() - t_move)
            if not pv:
                invalid_actor = "stockfish"; break
            try:
                move = chess.Move.from_uci(pv[0]["move"])
            except Exception:
                invalid_actor = "stockfish"; break
            if move not in board.legal_moves:
                invalid_actor = "stockfish"; break
        board.push(move)
    elapsed_wall = time.perf_counter() - wall0
    rusage1 = resource.getrusage(resource.RUSAGE_SELF)
    outcome = board.outcome(claim_draw=True)
    if invalid_actor:
        winner = "stockfish" if invalid_actor == "engine" else "engine"
        result = f"{winner}_win"
        reason = "invalid_move"
    elif outcome is None:
        result = "incomplete"
        reason = "max_plies"
    elif outcome.winner is None:
        result = "draw"
        reason = outcome.termination.name.lower()
    else:
        winner = "engine" if outcome.winner == engine_color else "stockfish"
        result = f"{winner}_win"
        reason = outcome.termination.name.lower()

    def _stat(seconds):
        if not seconds:
            return {"count": 0, "mean_s": 0.0, "max_s": 0.0}
        return {
            "count": len(seconds),
            "sum_s": round(sum(seconds), 4),
            "mean_s": round(statistics.fmean(seconds), 4),
            "max_s": round(max(seconds), 4),
        }
    score = SCORE_WIN if result == "engine_win" else (SCORE_DRAW if result == "draw" else SCORE_LOSS)
    return {
        "opening_id": opening_id,
        "stockfish_depth": stockfish_depth,
        "engine_color": engine_color_name,
        "result": result,
        "reason": reason,
        "plies": len(board.move_stack),
        "elapsed_wall_s": round(elapsed_wall, 3),
        "score_points": score,
        "engine_decision_times": _stat(engine_decision_seconds),
        "stockfish_decision_times": _stat(sf_decision_seconds),
        "process_resource_delta": {
            "user_cpu_s": round(rusage1.ru_utime - rusage0.ru_utime, 3),
            "sys_cpu_s": round(rusage1.ru_stime - rusage0.ru_stime, 3),
            "max_rss_mb_after": round(rusage1.ru_maxrss / 1024.0, 1),
        },
    }


def play_staged_test(difficulty):
    schedule = []
    for d in STAGED_DEPTHS:
        for k in range(STAGED_GAMES_PER_DEPTH):
            opening_id, opening_moves = STAGED_OPENINGS[(d + k - 1) % len(STAGED_OPENINGS)]
            engine_color = "white" if k % 2 == 0 else "black"
            schedule.append((opening_id, opening_moves, engine_color, d))
    rows = []
    with UciStockfish(STOCKFISH_BIN) as engine:
        for i, (opening_id, opening_moves, engine_color, depth) in enumerate(schedule, 1):
            row = play_one_game(opening_id, opening_moves, engine_color, difficulty, depth, engine)
            rows.append(row)
            ex = row["engine_decision_times"]; sf = row["stockfish_decision_times"]
            print(
                f"    g{i:02d} d{depth} {opening_id}/eng={engine_color}: "
                f"{row['result']:>14s} ({row['reason']}) "
                f"{row['plies']:3d}p  wall={row['elapsed_wall_s']:.1f}s  "
                f"eng_mean={ex.get('mean_s',0)*1000:.0f}ms max={ex.get('max_s',0)*1000:.0f}ms  "
                f"sf_mean={sf.get('mean_s',0)*1000:.0f}ms max={sf.get('max_s',0)*1000:.0f}ms  "
                f"score={row['score_points']:+d}",
                flush=True,
            )
    return rows


def score_summary(rows):
    wins = sum(1 for r in rows if r["result"] == "engine_win")
    draws = sum(1 for r in rows if r["result"] == "draw")
    losses = sum(1 for r in rows if r["result"] == "stockfish_win")
    total_score = sum(r["score_points"] for r in rows)
    by_depth = {}
    for r in rows:
        d = r["stockfish_depth"]
        bd = by_depth.setdefault(d, {"games": 0, "W": 0, "D": 0, "L": 0, "score": 0})
        bd["games"] += 1
        if r["result"] == "engine_win": bd["W"] += 1
        elif r["result"] == "draw": bd["D"] += 1
        elif r["result"] == "stockfish_win": bd["L"] += 1
        bd["score"] += r["score_points"]
    return {
        "games": len(rows), "W": wins, "D": draws, "L": losses,
        "total_score": total_score,
        "max_possible_score": SCORE_WIN * len(rows),
        "min_possible_score": SCORE_LOSS * len(rows),
        "by_depth": by_depth,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    engine_records = []
    for diff in ENGINE_DIFFICULTIES:
        print(f"\n=== {diff} ===", flush=True)
        t0 = time.perf_counter()
        rows = play_staged_test(diff)
        elapsed = time.perf_counter() - t0
        summary = score_summary(rows)
        wall_mean = round(sum(r["elapsed_wall_s"] for r in rows) / max(1, len(rows)), 2)
        engine_mean_ms = round(
            statistics.fmean([r["engine_decision_times"].get("mean_s", 0) * 1000 for r in rows if r["engine_decision_times"].get("count", 0) > 0]) if rows else 0,
            1,
        )
        record = {
            "difficulty": diff,
            "elapsed_s": round(elapsed, 1),
            "summary": summary,
            "wall_mean_s_per_game": wall_mean,
            "engine_mean_ms_per_move": engine_mean_ms,
            "games": rows,
        }
        engine_records.append(record)
        print(
            f"  {diff}: {summary['W']}W/{summary['D']}D/{summary['L']}L "
            f"score={summary['total_score']:+d}/{summary['max_possible_score']} "
            f"({elapsed:.0f}s)",
            flush=True,
        )
        # Incremental write so partial progress is visible.
        body = json.dumps({"engines": engine_records, "complete": False}, indent=2, default=str)
        REPORT_JSON.write_text(body)
        REPORT_JSON_RUNTIME.write_text(body)

    final = {"engines": engine_records, "complete": True}
    body = json.dumps(final, indent=2, default=str)
    REPORT_JSON.write_text(body)
    REPORT_JSON_RUNTIME.write_text(body)

    md = []
    md.append("# Chess Engine Horizontal Comparison\n\n")
    md.append(f"Date: 2026-05-16  \n")
    md.append(f"Test: 10 games per engine vs Stockfish ({STAGED_GAMES_PER_DEPTH} per depth × {len(STAGED_DEPTHS)} depths [1..5])  \n")
    md.append(f"Score: WIN={SCORE_WIN:+d} / DRAW={SCORE_DRAW:+d} / LOSS={SCORE_LOSS:+d}, range [{SCORE_LOSS*10:+d}, {SCORE_WIN*10:+d}]  \n\n")

    md.append("## Summary\n\n")
    md.append("| Engine | W | D | L | Total score | d1 | d2 | d3 | d4 | d5 | Wall mean (s) | Eng mean ms/move |\n")
    md.append("|---|---:|---:|---:|---:|---|---|---|---|---|---:|---:|\n")
    for rec in engine_records:
        s = rec["summary"]
        by_depth = s.get("by_depth") or {}
        cells = []
        for d in STAGED_DEPTHS:
            cell = by_depth.get(d) or by_depth.get(str(d))
            if cell:
                cells.append(f"{cell['W']}/{cell['D']}/{cell['L']} ({cell['score']:+d})")
            else:
                cells.append("—")
        md.append(
            f"| `{rec['difficulty']}` | {s['W']} | {s['D']} | {s['L']} | "
            f"{s['total_score']:+d}/{s['max_possible_score']} | " + " | ".join(cells) + f" | "
            f"{rec['wall_mean_s_per_game']} | {rec['engine_mean_ms_per_move']:.0f} |\n"
        )

    md.append("\n## Per-game detail\n")
    for rec in engine_records:
        md.append(f"\n### `{rec['difficulty']}`\n\n")
        md.append("| # | Depth | Open. | Color | Result | Reason | Plies | Wall s | Eng mean ms (max) | SF mean ms (max) | UserCPU s | Score |\n")
        md.append("|---:|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|\n")
        for i, g in enumerate(rec["games"], 1):
            ex = g.get("engine_decision_times", {})
            sf = g.get("stockfish_decision_times", {})
            res = g.get("process_resource_delta", {})
            md.append(
                f"| {i} | {g['stockfish_depth']} | {g['opening_id']} | {g['engine_color']} | {g['result']} | {g['reason']} | "
                f"{g['plies']} | {g.get('elapsed_wall_s', 0):.1f} | "
                f"{ex.get('mean_s',0)*1000:.0f} ({ex.get('max_s',0)*1000:.0f}) | "
                f"{sf.get('mean_s',0)*1000:.0f} ({sf.get('max_s',0)*1000:.0f}) | "
                f"{res.get('user_cpu_s', 0):.2f} | {g.get('score_points', 0):+d} |\n"
            )
    body_md = "".join(md)
    REPORT_MD.write_text(body_md)
    REPORT_MD_RUNTIME.write_text(body_md)
    print(f"\nreports: {REPORT_MD} (runtime mirror: {REPORT_MD_RUNTIME})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
