#!/usr/bin/env python3
"""Focused score probe for the chess ``experiment 5:nnue`` engine.

This is intentionally narrower than ``game_ai_strength_eval.py``: it runs the
same scoring primitives, but only for exp5, so optimization passes can quickly
tell whether a heuristic actually improves the objective score.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.games.game_ai_strength_eval import (  # noqa: E402
    CHESS_LABELS,
    FixedCase,
    aggregate_fixed,
    aggregate_sparring,
    case_passed,
    choose_fixed_move,
    fixed_cases,
    load_external_replay_cases,
    move_label,
    play_chess_ai_vs_random,
    score_rows,
    stable_seed,
)


EXP5 = "experiment 5:nnue"
from scripts.games.common_paths import chess_results_root  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_REPLAY = DEFAULT_RESULTS_ROOT / "retrain_redo_20260512T224634Z" / "replays" / "carlsen_25_game_level.jsonl"


def run_exp5_fixed_cases(extra_cases: list[FixedCase]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cases = [case for case in [*fixed_cases(), *extra_cases] if case.game_key == "chess"]
    for case in cases:
        actual, raw, valid, validation_error = choose_fixed_move(case, EXP5)
        passed = case_passed(case, actual, valid)
        rows.append(
            {
                "game_key": "chess",
                "game_label": "西洋棋",
                "difficulty": EXP5,
                "difficulty_label": CHESS_LABELS[EXP5],
                "case_id": case.case_id,
                "categories": list(case.categories),
                "description": case.description,
                "correct_direction": case.correct_direction,
                "expected_moves": list(case.expected_moves),
                "avoid_moves": list(case.avoid_moves),
                "max_points": float(case.max_points),
                "actual_move": actual,
                "actual_move_label": move_label("chess", actual),
                "raw_decision": raw,
                "passed": bool(passed),
                "valid": bool(valid),
                "validation_error": validation_error,
            }
        )
    return rows


def run_exp5_sparring(*, games_per_side: int, seed: int, max_plies: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ai_color in ("white", "black"):
        for game_no in range(1, int(games_per_side) + 1):
            match_seed = stable_seed(seed, "chess", EXP5, ai_color, game_no)
            row = play_chess_ai_vs_random(EXP5, ai_color, match_seed, max(1, int(max_plies)))
            row["trial"] = game_no
            rows.append(row)
    return rows


def summarize(fixed_rows: list[dict[str, Any]], sparring_rows: list[dict[str, Any]]) -> dict[str, Any]:
    fixed_summary = aggregate_fixed(fixed_rows)
    sparring_summary = aggregate_sparring(sparring_rows)
    score = score_rows(fixed_summary, sparring_summary)
    exp5_score = next((row for row in score if row.get("game_key") == "chess" and row.get("difficulty") == EXP5), {})
    failed_fixed = [
        {
            "case_id": row["case_id"],
            "expected_moves": row.get("expected_moves"),
            "actual_move": row.get("actual_move"),
            "categories": row.get("categories"),
            "max_points": row.get("max_points"),
            "description": row.get("description"),
        }
        for row in fixed_rows
        if not row.get("passed")
    ]
    return {
        "score_row": exp5_score,
        "fixed_summary": fixed_summary.get(f"chess::{EXP5}", {}),
        "sparring_summary": sparring_summary.get(f"chess::{EXP5}", {}),
        "failed_fixed": failed_fixed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run exp5-only chess score probe.")
    parser.add_argument("--seed", type=int, default=20260513)
    parser.add_argument("--games-per-side", type=int, default=3)
    parser.add_argument("--chess-max-plies", type=int, default=120)
    parser.add_argument("--external-replay-jsonl", action="append", default=[str(DEFAULT_REPLAY)])
    parser.add_argument("--external-case-limit", type=int, default=24)
    parser.add_argument("--output", default=str(ROOT / "docs" / "games" / "2026-05-13_exp5_score_probe.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    external_cases = load_external_replay_cases(args.external_replay_jsonl, limit=max(0, int(args.external_case_limit)))
    fixed_rows = run_exp5_fixed_cases(external_cases)
    sparring_rows = run_exp5_sparring(
        games_per_side=max(1, int(args.games_per_side)),
        seed=int(args.seed),
        max_plies=max(1, int(args.chess_max_plies)),
    )
    artifact = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "engine": EXP5,
        "seed": int(args.seed),
        "external_case_count": len(external_cases),
        "summary": summarize(fixed_rows, sparring_rows),
        "fixed_results": fixed_rows,
        "sparring_results": sparring_rows,
    }
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
