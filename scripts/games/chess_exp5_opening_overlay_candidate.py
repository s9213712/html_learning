#!/usr/bin/env python3
"""exp5_16 opening overlay candidate builder.

This creates an isolated model artifact with an exact-position opening overlay
from the exp5_14b clean opening curriculum. It does not retrain, stage,
promote, or mutate the runtime production model.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import shutil
import sys

import chess


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.games.chess_exp5_opening_candidate_search import (  # noqa: E402
    _iter_jsonl,
    _read_summary_rows,
    _sha256_file,
    evaluate_opening_pool,
    evaluate_retention,
    resolve_current_production_model,
)
from scripts.games.chess_exp5_production_readiness import _position_id  # noqa: E402
from services.games.chess_nnue import (  # noqa: E402
    experiment_nnue_model_template,
    normalize_experiment_nnue_model_payload,
    save_experiment_nnue_experience_delta,
)


from scripts.games.common_paths import chess_results_root, runtime_model_path  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_OPENING_CURRICULUM = DEFAULT_RESULTS_ROOT / "exp5_14b_clean_opening_heldout" / "clean_opening_curriculum.jsonl"
DEFAULT_EXP5_13_SUMMARY = DEFAULT_RESULTS_ROOT / "exp5_13_rule_smoke_stalemate_fix_check" / "summary.json"
DEFAULT_RUNTIME_PRODUCTION = runtime_model_path("chess_experiment_5_nnue_experience.json")
DEFAULT_PROMOTED_STAGE = DEFAULT_RESULTS_ROOT / "exp5_08_stage_candidate" / "chess_experiment_5_nnue_stage_candidate.json"
DEFAULT_OUTPUT_DIR = DEFAULT_RESULTS_ROOT / "exp5_16_opening_overlay_candidate"
DEFAULT_SEARCH_PROFILE = "fixed_depth_strong"
CURRENT_PRODUCTION_SHA = "c47ef752aa69d7b8c813b587468228593f44d69c9b947313325e03797e4450dc"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and gate an isolated exp5 opening overlay candidate.")
    parser.add_argument("--opening-curriculum-jsonl", default=str(DEFAULT_OPENING_CURRICULUM))
    parser.add_argument("--exp5-13-summary", default=str(DEFAULT_EXP5_13_SUMMARY))
    parser.add_argument("--production-model-path", default=str(DEFAULT_RUNTIME_PRODUCTION))
    parser.add_argument("--fallback-production-model-path", default=str(DEFAULT_PROMOTED_STAGE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--search-profile", default=DEFAULT_SEARCH_PROFILE)
    parser.add_argument("--repeatability-seeds", default="11,12,13,14,15")
    parser.add_argument("--max-fullmove", type=int, default=12)
    return parser.parse_args()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _hash_rows(rows: list[dict]) -> str:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _legal_expected_moves(row: dict) -> list[str]:
    fen = str(row.get("fen") or "")
    side = str(row.get("side") or "").strip().lower()
    try:
        board = chess.Board(fen)
    except Exception:
        return []
    board.turn = chess.WHITE if side == "white" else chess.BLACK
    moves: list[str] = []
    for item in row.get("expected_uci_any") or []:
        uci = str(item or "").strip().lower()
        try:
            move = chess.Move.from_uci(uci)
        except Exception:
            continue
        if move in board.legal_moves and uci not in moves:
            moves.append(uci)
    return moves


def build_opening_overlay(rows: list[dict], *, max_fullmove: int = 12) -> dict:
    positions: dict[str, dict] = {}
    skipped: list[dict] = []
    for row in rows:
        fen = str(row.get("fen") or "").strip()
        side = str(row.get("side") or "").strip().lower()
        if str(row.get("label_quality") or "").lower() != "clean" or side not in {"white", "black"}:
            skipped.append({"id": str(row.get("id") or ""), "reason": "not_clean_or_bad_side"})
            continue
        expected = _legal_expected_moves(row)
        if not fen or not expected:
            skipped.append({"id": str(row.get("id") or ""), "reason": "no_legal_expected_moves"})
            continue
        position_id = str(row.get("position_id") or _position_id(fen, side))
        positions[position_id] = {
            "id": str(row.get("id") or position_id),
            "fen": fen,
            "side": side,
            "source": str(row.get("source") or "curated_opening_book_v1"),
            "label_quality": "clean",
            "moves": [
                {"uci": move, "weight": 100 - index}
                for index, move in enumerate(expected)
            ],
        }
    return {
        "enabled": True,
        "version": "exp5_opening_overlay_v1",
        "mode": "exact_position_book_prior",
        "max_fullmove": int(max_fullmove),
        "positions": positions,
        "build_summary": {
            "input_rows": len(rows),
            "position_count": len(positions),
            "skipped_rows": len(skipped),
            "skipped": skipped,
            "dataset_hash": _hash_rows(rows),
        },
    }


def build_candidate_model(*, base_model: Path, output_dir: Path, opening_rows: list[dict], max_fullmove: int) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_model = output_dir / "chess_experiment_5_nnue_opening_overlay_candidate.json"
    overlay_path = output_dir / "opening_overlay.json"
    if base_model.exists():
        shutil.copyfile(base_model, candidate_model)
    else:
        candidate_model.write_text(
            json.dumps(experiment_nnue_model_template(), ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    payload = json.loads(candidate_model.read_text(encoding="utf-8"))
    overlay = build_opening_overlay(opening_rows, max_fullmove=max_fullmove)
    payload["opening_overlay"] = overlay
    payload["updated_at"] = _now()
    normalized = normalize_experiment_nnue_model_payload(payload)
    if normalized is None:
        raise RuntimeError(f"failed to normalize candidate model: {candidate_model}")
    save_experiment_nnue_experience_delta(candidate_model, normalized)
    _write_json(overlay_path, overlay)
    return {
        "candidate_model_path": str(candidate_model),
        "opening_overlay_path": str(overlay_path),
        "candidate_sha256": _sha256_file(candidate_model),
        "opening_overlay": overlay,
    }


def _repeatability(exp5_rows: list[dict], *, candidate_model: Path, search_profile: str, seeds: list[int]) -> dict:
    runs: list[dict] = []
    for seed in seeds:
        rows = list(exp5_rows)
        random.Random(seed).shuffle(rows)
        retention = evaluate_retention(rows, candidate_model=candidate_model, search_profile=search_profile)
        overall = retention["overall"]
        smoke = retention["clusters"].get("smoke", {})
        endgame = retention["clusters"].get("endgame", {})
        special = retention["clusters"].get("special_rule", {})
        passed = (
            int(overall["clean_regressed_count"]) == 0
            and float(overall["illegal_rate"]) == 0.0
            and float(overall["suspicious_rate"]) == 0.0
            and int(smoke.get("candidate_passed", 0)) >= int(smoke.get("current_passed", 0))
            and float(endgame.get("score_delta", 0.0)) >= 0.0
            and float(special.get("score_delta", 0.0)) >= 0.0
        )
        runs.append({
            "seed": seed,
            "retention_score_delta": overall["score_delta"],
            "clean_regressed_count": overall["clean_regressed_count"],
            "illegal_rate": overall["illegal_rate"],
            "suspicious_rate": overall["suspicious_rate"],
            "endgame_delta": endgame.get("score_delta", 0.0),
            "smoke_delta": smoke.get("score_delta", 0.0),
            "special_rule_delta": special.get("score_delta", 0.0),
            "pass": passed,
        })
    return {
        "repeatability_type": "case_order_repeatability",
        "model_training_repeated": False,
        "seeds": seeds,
        "runs": runs,
        "pass_count": sum(1 for row in runs if row["pass"]),
        "run_count": len(runs),
        "pass": bool(runs and all(row["pass"] for row in runs)),
    }


def _write_summary_md(path: Path, summary: dict) -> None:
    opening = summary["opening_evaluation"]["overall"]
    retention = summary["retention_evaluation"]["overall"]
    lines = [
        "# exp5_16 opening overlay candidate",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- current_production_sha_expected: `{summary['current_production_sha_expected']}`",
        f"- current_production_sha_actual: `{summary['current_production_sha_actual']}`",
        f"- current_model_source: `{summary['current_model_source']}`",
        f"- runtime_mutated: `{summary['runtime_mutated']}`",
        f"- stage_promote_attempted: `{summary['stage_promote_attempted']}`",
        f"- candidate_can_stage_for_exp5_17: `{summary['candidate_can_stage_for_exp5_17']}`",
        f"- verdict: `{summary['verdict']}`",
        "",
        "## Opening",
        "",
        f"- current: `{opening['current_passed']}/{opening['cases']} = {opening['current_score']}`",
        f"- candidate: `{opening['candidate_passed']}/{opening['cases']} = {opening['candidate_score']}`",
        f"- delta: `{opening['score_delta']}`",
        "",
        "## Retention",
        "",
        f"- current: `{retention['current_passed']}/{retention['cases']} = {retention['current_score']}`",
        f"- candidate: `{retention['candidate_passed']}/{retention['cases']} = {retention['candidate_score']}`",
        f"- delta: `{retention['score_delta']}`",
        f"- clean_regressed_count: `{retention['clean_regressed_count']}`",
        f"- illegal_rate: `{retention['illegal_rate']}`",
        f"- suspicious_rate: `{retention['suspicious_rate']}`",
        "",
        "## Artifacts",
        "",
    ]
    for key, value in sorted(summary["artifacts"].items()):
        lines.append(f"- {key}: `{value}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_overlay_candidate(
    *,
    opening_curriculum: Path,
    exp5_13_summary: Path,
    production_model: Path,
    fallback_production_model: Path,
    output_dir: Path,
    search_profile: str,
    repeatability_seeds: list[int],
    max_fullmove: int,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    current_model, current_source = resolve_current_production_model(production_model, fallback_production_model)
    current_hash = _sha256_file(current_model)
    opening_rows = _iter_jsonl(opening_curriculum)
    exp5_rows = _read_summary_rows(exp5_13_summary)
    candidate = build_candidate_model(
        base_model=current_model,
        output_dir=output_dir,
        opening_rows=opening_rows,
        max_fullmove=max_fullmove,
    )
    candidate_path = Path(candidate["candidate_model_path"])
    opening_eval = evaluate_opening_pool(opening_rows, current_model=current_model, candidate_model=candidate_path, search_profile=search_profile)
    retention_eval = evaluate_retention(exp5_rows, candidate_model=candidate_path, search_profile=search_profile)
    repeatability = _repeatability(exp5_rows, candidate_model=candidate_path, search_profile=search_profile, seeds=repeatability_seeds)

    opening_overall = opening_eval["overall"]
    retention_overall = retention_eval["overall"]
    clusters = retention_eval["clusters"]
    smoke = clusters.get("smoke", {})
    endgame = clusters.get("endgame", {})
    special = clusters.get("special_rule", {})
    tactic = clusters.get("tactic", {})
    passed = (
        float(opening_overall["score_delta"]) > 0.0
        and int(retention_overall["clean_regressed_count"]) == 0
        and float(retention_overall["illegal_rate"]) == 0.0
        and float(retention_overall["suspicious_rate"]) == 0.0
        and int(smoke.get("candidate_passed", 0)) >= int(smoke.get("current_passed", 0))
        and float(endgame.get("score_delta", 0.0)) >= 0.0
        and float(special.get("score_delta", 0.0)) >= 0.0
        and float(tactic.get("score_delta", 0.0)) >= 0.0
        and bool(repeatability["pass"])
    )
    block_reasons = [] if passed else [
        reason
        for reason, flag in [
            ("opening_score_not_improved", float(opening_overall["score_delta"]) <= 0.0),
            ("retention_clean_regression", int(retention_overall["clean_regressed_count"]) > 0),
            ("illegal_rate_nonzero", float(retention_overall["illegal_rate"]) != 0.0),
            ("suspicious_rate_nonzero", float(retention_overall["suspicious_rate"]) != 0.0),
            ("smoke_regression", int(smoke.get("candidate_passed", 0)) < int(smoke.get("current_passed", 0))),
            ("endgame_regression", float(endgame.get("score_delta", 0.0)) < 0.0),
            ("special_rule_regression", float(special.get("score_delta", 0.0)) < 0.0),
            ("tactic_regression", float(tactic.get("score_delta", 0.0)) < 0.0),
            ("repeatability_not_passed", not bool(repeatability["pass"])),
        ]
        if flag
    ]
    artifacts = {
        "summary_json": str(output_dir / "summary.json"),
        "summary_md": str(output_dir / "SUMMARY.md"),
        "candidate_model": str(candidate_path),
        "opening_overlay": candidate["opening_overlay_path"],
        "opening_evaluation": str(output_dir / "opening_evaluation.json"),
        "retention_evaluation": str(output_dir / "retention_evaluation.json"),
        "repeatability_5_seed": str(output_dir / "repeatability_5_seed.json"),
    }
    _write_json(Path(artifacts["opening_evaluation"]), opening_eval)
    _write_json(Path(artifacts["retention_evaluation"]), retention_eval)
    _write_json(Path(artifacts["repeatability_5_seed"]), repeatability)
    summary = {
        "ok": True,
        "generated_at": _now(),
        "engine": "experiment 5:nnue",
        "candidate_type": "opening_exact_position_overlay",
        "stage_promote_attempted": False,
        "runtime_mutated": False,
        "current_production_sha_expected": CURRENT_PRODUCTION_SHA,
        "current_production_sha_actual": current_hash,
        "current_production_sha_matches_expected": current_hash == CURRENT_PRODUCTION_SHA,
        "current_model_path": str(current_model),
        "current_model_source": current_source,
        "opening_curriculum": str(opening_curriculum),
        "opening_curriculum_rows": len(opening_rows),
        "exp5_13_summary": str(exp5_13_summary),
        "search_profile": search_profile,
        "candidate_model_path": str(candidate_path),
        "candidate_sha256": candidate["candidate_sha256"],
        "opening_overlay_summary": candidate["opening_overlay"]["build_summary"],
        "opening_evaluation": {
            "overall": opening_eval["overall"],
            "detail_path": artifacts["opening_evaluation"],
        },
        "retention_evaluation": {
            "overall": retention_eval["overall"],
            "clusters": retention_eval["clusters"],
            "detail_path": artifacts["retention_evaluation"],
        },
        "repeatability": repeatability,
        "candidate_can_stage_for_exp5_17": passed,
        "verdict": "stage_for_exp5_17" if passed else "blocked",
        "block_reasons": block_reasons,
        "artifacts": artifacts,
    }
    _write_json(Path(artifacts["summary_json"]), summary)
    _write_summary_md(Path(artifacts["summary_md"]), summary)
    return summary


def main() -> int:
    args = parse_args()
    seeds = [int(item.strip()) for item in str(args.repeatability_seeds).split(",") if item.strip()]
    result = run_overlay_candidate(
        opening_curriculum=Path(args.opening_curriculum_jsonl),
        exp5_13_summary=Path(args.exp5_13_summary),
        production_model=Path(args.production_model_path),
        fallback_production_model=Path(args.fallback_production_model_path),
        output_dir=Path(args.output_dir),
        search_profile=str(args.search_profile),
        repeatability_seeds=seeds,
        max_fullmove=int(args.max_fullmove),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
