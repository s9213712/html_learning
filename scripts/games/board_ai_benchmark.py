#!/usr/bin/env python3
"""Run non-chess board-game AI strength benchmarks.

This benchmark is intentionally separate from the chess self-play pipeline.
It plays Reversi, 9x9 Go, and Gomoku engines against each other, estimates an
in-pool Elo, and records deterministic skill probes before any future training
or neural model promotion is trusted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.board_arena import (  # noqa: E402
    BOARD_ARENA_ENGINES,
    DEFAULT_BOARD_ARENA_GAMES,
    run_board_ai_benchmark,
    write_board_ai_benchmark_report,
)
from scripts.test_artifacts import test_artifact_path  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Reversi/Go/Gomoku local board-game AI strength.")
    parser.add_argument(
        "--games",
        default=",".join(DEFAULT_BOARD_ARENA_GAMES),
        help="Comma-separated game keys. Default: reversi,go,gomoku.",
    )
    parser.add_argument(
        "--engines",
        default=",".join(BOARD_ARENA_ENGINES),
        help="Comma-separated engines. Supported: random,easy,normal,hard.",
    )
    parser.add_argument("--rounds", type=int, default=1, help="Rounds per unordered engine pair; each round swaps colors.")
    parser.add_argument("--max-plies", type=int, default=0, help="Optional max plies per match; 0 uses per-game defaults.")
    parser.add_argument("--seed", type=int, default=20260513)
    parser.add_argument(
        "--output-dir",
        default="",
        help="Report directory. Default: /tmp/hackme_web_test_artifacts/games/board_ai_benchmark.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full report JSON to stdout.")
    return parser.parse_args()


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _progress(message: str) -> None:
    sys.stderr.write(f"[board-ai-benchmark] {message}\n")
    sys.stderr.flush()


def main() -> int:
    args = parse_args()
    games = _split_csv(args.games)
    engines = _split_csv(args.engines)
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else test_artifact_path(
        "games", "board_ai_benchmark"
    )
    _progress(
        "started "
        f"games={','.join(games)} engines={','.join(engines)} "
        f"rounds={max(1, int(args.rounds))} seed={int(args.seed)}"
    )
    report = run_board_ai_benchmark(
        game_keys=games,
        engines=engines,
        rounds=max(1, int(args.rounds)),
        max_plies=int(args.max_plies) or None,
        seed=int(args.seed),
    )
    path = write_board_ai_benchmark_report(report, output_dir=output_dir)
    _progress(f"finished games_played={report['games_played']} artifact={path}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"artifact: {path}")
        print("standings:")
        for row in report.get("standings", []):
            print(
                f"- {row['engine']}: score_rate={row['score_rate']} "
                f"score={row['score']} games={row['games']} illegal={row['illegal_moves']}"
            )
        print("elo:")
        for row in report.get("elo", []):
            print(f"- {row['engine']}: elo={row['elo']} games={row['games']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
