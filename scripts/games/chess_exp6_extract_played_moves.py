#!/usr/bin/env python3
"""Build a (game_idx, fen) → played_move_uci map from
quality_1000_games.jsonl. Output: per-line JSON for each
(game_idx, fen, played_move_uci) in the order the quality file
serializes games (matching the curriculum labels.jsonl game_idx).

Use this map in the v7.3 Phase 3 ranking training to know what the
"good" move was at each position.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.games.common_paths import exp6_private_dir  # noqa: E402


DEFAULT_QUALITY_PATH = exp6_private_dir() / "quality_1000_games.jsonl"
DEFAULT_OUT_PATH = exp6_private_dir() / "played_moves.jsonl"

# Must match curriculum's _SHUFFLE_SEED so the game_idx we produce
# here corresponds to the same game ordering as labels.jsonl
# (curriculum's load_games applies this shuffle before assigning idx).
SHUFFLE_SEED = 20260516


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_QUALITY_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument("--max-games", type=int, default=None, help="Cap game count after shuffle.")
    args = ap.parse_args()
    QUALITY_PATH = args.source
    OUT_PATH = args.out
    if not QUALITY_PATH.exists():
        print(f"missing {QUALITY_PATH}")
        return 1
    games = []
    with QUALITY_PATH.open() as f:
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
    print(f"loaded + shuffled {len(games)} games (seed={SHUFFLE_SEED})")
    n_rows = 0
    with OUT_PATH.open("w") as out:
        for game_idx, rec in enumerate(games):
            mh = rec.get("move_history") or []
            # IMPORTANT: labels.jsonl stores FENs from board.fen() AFTER
            # pushing each move (curriculum's collect_positions_latter_half
            # records POST-move positions). The "played move FROM this
            # labeled position" is therefore the NEXT move in the game,
            # not the current move. So we key on fen_after[i] and store
            # move_history[i+1].uci (skip the last move with no successor).
            for i in range(len(mh) - 1):
                fen_after_i = mh[i].get("fen_after")
                next_uci = mh[i + 1].get("uci")
                if not fen_after_i or not next_uci:
                    continue
                out.write(json.dumps({
                    "game_idx": game_idx,
                    "fen": fen_after_i,
                    "played_move": next_uci,
                }) + "\n")
                n_rows += 1
            # Also include the initial position before move 0:
            if mh:
                fen_initial = mh[0].get("fen_before")
                first_uci = mh[0].get("uci")
                if fen_initial and first_uci:
                    out.write(json.dumps({
                        "game_idx": game_idx,
                        "fen": fen_initial,
                        "played_move": first_uci,
                    }) + "\n")
                    n_rows += 1
    print(f"wrote {n_rows} (game_idx, fen, played_move) rows -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
