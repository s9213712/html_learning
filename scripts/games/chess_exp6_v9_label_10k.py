#!/usr/bin/env python3
"""Stockfish-label the 10K quality dataset for v9 ranking pipeline.

Output JSONL with same schema as curriculum_labels.jsonl:
  {game_idx, fen, cp_white, outcome_white, label_depth, outcome_blend}

Uses the same SHUFFLE_SEED as curriculum so game_idx maps consistently
to chess_exp6_extract_played_moves.py output.

Single-threaded Stockfish at depth 4. ~3-5 hr for 10K games (estimated
1M positions). Resumable: if output already has N rows, continue from
the next un-labeled position.
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

from services.games.chess_stockfish_teacher import (  # noqa: E402
    UciStockfish, analysis_limit, resolve_stockfish_path,
)
from scripts.games.common_paths import exp6_private_dir  # noqa: E402

EXP6_PRIVATE_DIR = exp6_private_dir()
SOURCE_PATH = EXP6_PRIVATE_DIR / "quality_10k_games.jsonl"
OUT_PATH = EXP6_PRIVATE_DIR / "curriculum_labels_10k.jsonl"
SHUFFLE_SEED = 20260516              # must match curriculum's seed for game_idx consistency
STOCKFISH_DEPTH = 4
LATTER_HALF_FRACTION = 1.0           # match v4+ curriculum (all plies)
PROGRESS_EVERY = 500


def _game_outcome_white(rec: dict) -> float:
    winner = (rec.get("winner_color") or "").lower().strip()
    if winner == "white":
        return 1.0
    if winner == "black":
        return -1.0
    result = (rec.get("result") or "").strip()
    if result == "1-0":
        return 1.0
    if result == "0-1":
        return -1.0
    return 0.0


def collect_positions(games: list[dict]):
    """Returns list of (game_idx, fen, outcome_white)."""
    out = []
    for gi, rec in enumerate(games):
        moves = rec.get("move_history") or []
        n = len(moves)
        if n < 8:
            continue
        outcome_w = _game_outcome_white(rec)
        cutoff = max(1, int(n * (1.0 - LATTER_HALF_FRACTION)))
        board = chess.Board()
        for i in range(cutoff):
            uci = moves[i].get("uci") if isinstance(moves[i], dict) else str(moves[i])
            try:
                mv = chess.Move.from_uci(uci)
                if mv not in board.legal_moves:
                    break
                board.push(mv)
            except Exception:
                break
        for i in range(cutoff, n):
            uci = moves[i].get("uci") if isinstance(moves[i], dict) else str(moves[i])
            try:
                mv = chess.Move.from_uci(uci)
                if mv not in board.legal_moves:
                    break
                board.push(mv)
            except Exception:
                break
            out.append((gi, board.fen(), outcome_w))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=SOURCE_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--depth", type=int, default=STOCKFISH_DEPTH)
    ap.add_argument("--max-games", type=int, default=None, help="Cap games count for testing")
    ap.add_argument("--resume", action="store_true", help="If output exists, count rows and skip already-labeled positions")
    ap.add_argument("--shard", default="0/1", help="Worker shard 'id/N' — process positions[id::N], write to <out>.part<id>")
    args = ap.parse_args()
    try:
        shard_id, shard_n = (int(x) for x in args.shard.split("/"))
    except Exception:
        print(f"bad --shard '{args.shard}', expected id/N")
        return 1
    if shard_n > 1:
        args.out = args.out.parent / f"{args.out.stem}.part{shard_id}{args.out.suffix}"

    if not args.source.exists():
        print(f"missing source {args.source}")
        return 1

    games = []
    with args.source.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                games.append(json.loads(line))
            except Exception:
                continue
    rng = random.Random(SHUFFLE_SEED)
    rng.shuffle(games)
    if args.max_games and args.max_games < len(games):
        games = games[:args.max_games]
    print(f"loaded + shuffled {len(games)} games", flush=True)

    positions = collect_positions(games)
    print(f"extracted {len(positions)} positions (all plies)", flush=True)
    if shard_n > 1:
        positions = positions[shard_id::shard_n]
        print(f"  shard {shard_id}/{shard_n}: {len(positions)} positions assigned", flush=True)

    # Resume handling: count existing rows in output
    skip_n = 0
    if args.resume and args.out.exists():
        with args.out.open() as f:
            for _ in f:
                skip_n += 1
        print(f"resume: skipping first {skip_n} positions already labeled", flush=True)

    sf_path = resolve_stockfish_path()
    if not sf_path:
        print("Stockfish binary not found; abort")
        return 2

    limit = analysis_limit(depth=args.depth, movetime_ms=0)
    t0 = time.perf_counter()
    n_labeled = skip_n
    mode = "a" if args.resume and skip_n > 0 else "w"
    tag = f"shard{shard_id}" if shard_n > 1 else "single"
    with args.out.open(mode) as out_f, UciStockfish(sf_path) as engine:
        for i, (game_idx, fen, outcome_w) in enumerate(positions):
            if i < skip_n:
                continue
            board = chess.Board(fen)
            try:
                pv = engine.analyse(board, limit=limit, multipv=1)
            except Exception:
                continue
            if not pv:
                continue
            cp = pv[0].get("teacher_eval_cp")
            if cp is None:
                continue
            cp_white = float(cp) if board.turn == chess.WHITE else -float(cp)
            out_f.write(json.dumps({
                "game_idx": game_idx,
                "fen": fen,
                "cp_white": cp_white,
                "outcome_white": outcome_w,
                "blended_cp": cp_white,  # v9 uses pure cp (no outcome blend at label time)
                "label_depth": args.depth,
                "outcome_blend": 0.0,
            }) + "\n")
            out_f.flush()
            n_labeled += 1
            if n_labeled % PROGRESS_EVERY == 0:
                dt = time.perf_counter() - t0
                rate = (n_labeled - skip_n) / max(dt, 1e-6)
                remain = (len(positions) - n_labeled) / max(rate, 1e-6)
                print(f"  [{tag}] labeled {n_labeled}/{len(positions)} ({dt:.1f}s elapsed, {remain/60:.1f} min remain est)", flush=True)
    print(f"[{tag}] done: {n_labeled} labels written -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
