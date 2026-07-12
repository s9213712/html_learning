#!/usr/bin/env python3
"""exp5_17 staging/readiness validation for the exp5_16 opening overlay.

This validates an already-built opening overlay candidate. It does not train,
stage, promote, or mutate runtime production.
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
from services.games.chess_nnue import choose_experiment_nnue_move  # noqa: E402


from scripts.games.common_paths import chess_results_root, runtime_model_path  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_CANDIDATE = DEFAULT_RESULTS_ROOT / "exp5_16_opening_overlay_candidate" / "chess_experiment_5_nnue_opening_overlay_candidate.json"
DEFAULT_OPENING_CURRICULUM = DEFAULT_RESULTS_ROOT / "exp5_14b_clean_opening_heldout" / "clean_opening_curriculum.jsonl"
DEFAULT_EXP5_13_SUMMARY = DEFAULT_RESULTS_ROOT / "exp5_13_rule_smoke_stalemate_fix_check" / "summary.json"
DEFAULT_RUNTIME_PRODUCTION = runtime_model_path("chess_experiment_5_nnue_experience.json")
DEFAULT_PROMOTED_STAGE = DEFAULT_RESULTS_ROOT / "exp5_08_stage_candidate" / "chess_experiment_5_nnue_stage_candidate.json"
DEFAULT_OUTPUT_DIR = DEFAULT_RESULTS_ROOT / "exp5_17_opening_overlay_staging_validation"
DEFAULT_SEARCH_PROFILE = "fixed_depth_strong"
CURRENT_PRODUCTION_SHA = "c47ef752aa69d7b8c813b587468228593f44d69c9b947313325e03797e4450dc"
EXP5_16_CANDIDATE_SHA = "d35c04707fd7c10e8b6efe07740e69da533dea524d31aa63308afea891d006c9"


FRESH_OPENING_LINES: tuple[tuple[str, list[str]], ...] = (
    ("ruy_main_5", ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6"]),
    ("open_sicilian_4", ["e4", "c5", "Nf3", "Nc6", "d4", "cxd4"]),
    ("qgd_deeper_4", ["d4", "d5", "c4", "e6", "Nc3", "Nf6", "Bg5", "Be7"]),
    ("french_classical_4", ["e4", "e6", "d4", "d5", "Nc3", "Nf6"]),
    ("caro_main_4", ["e4", "c6", "d4", "d5", "Nc3", "dxe4"]),
    ("english_reversed_4", ["c4", "e5", "Nc3", "Nf6", "g3", "d5"]),
    ("reti_kia_4", ["Nf3", "d5", "g3", "Nf6", "Bg2", "e6"]),
    ("london_deeper_4", ["d4", "Nf6", "Bf4", "d5", "e3", "e6"]),
    ("kid_main_4", ["d4", "Nf6", "c4", "g6", "Nc3", "Bg7", "e4", "d6"]),
    ("slav_deeper_4", ["d4", "d5", "c4", "c6", "Nf3", "Nf6", "Nc3", "dxc4"]),
    ("italian_castled_5", ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "O-O"]),
    ("nimzo_deeper_4", ["d4", "Nf6", "c4", "e6", "Nc3", "Bb4", "e3", "O-O"]),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate exp5_16 opening overlay candidate for staging/readiness.")
    parser.add_argument("--candidate-model-path", default=str(DEFAULT_CANDIDATE))
    parser.add_argument("--opening-curriculum-jsonl", default=str(DEFAULT_OPENING_CURRICULUM))
    parser.add_argument("--exp5-13-summary", default=str(DEFAULT_EXP5_13_SUMMARY))
    parser.add_argument("--production-model-path", default=str(DEFAULT_RUNTIME_PRODUCTION))
    parser.add_argument("--fallback-production-model-path", default=str(DEFAULT_PROMOTED_STAGE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--search-profile", default=DEFAULT_SEARCH_PROFILE)
    parser.add_argument("--repeatability-seeds", default="11,12,13,14,15")
    return parser.parse_args()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _move_uci(move: dict | None) -> str:
    if not move:
        return ""
    return f"{move.get('from', '')}{move.get('to', '')}{move.get('promotion') or ''}".lower()


def _choose_uci(model_path: Path, fen: str, side: str, *, search_profile: str) -> str:
    return _move_uci(choose_experiment_nnue_move({"__fen__": fen}, side, model_path=model_path, search_profile=search_profile))


def _load_model(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _overlay_positions(candidate_model: Path) -> dict:
    payload = _load_model(candidate_model)
    overlay = payload.get("opening_overlay") if isinstance(payload.get("opening_overlay"), dict) else {}
    positions = overlay.get("positions") if isinstance(overlay.get("positions"), dict) else {}
    return {str(key): value for key, value in positions.items() if isinstance(value, dict)}


def _apply_san_line(line: list[str]) -> chess.Board:
    board = chess.Board()
    for san in line:
        board.push(board.parse_san(san))
    return board


def _side_name(board: chess.Board) -> str:
    return "white" if board.turn == chess.WHITE else "black"


def build_fresh_non_overlay_opening_probes(overlay_position_ids: set[str]) -> list[dict]:
    probes: list[dict] = []
    for label, line in FRESH_OPENING_LINES:
        board = _apply_san_line(list(line))
        fen = board.fen()
        side = _side_name(board)
        position_id = _position_id(fen, side)
        probes.append({
            "id": f"exp5_17_fresh_{label}",
            "fen": fen,
            "side": side,
            "position_id": position_id,
            "source_line_san": list(line),
            "position_id_overlaps_overlay": position_id in overlay_position_ids,
        })
    return probes


def overlay_activation_audit(
    opening_rows: list[dict],
    *,
    current_model: Path,
    candidate_model: Path,
    search_profile: str,
) -> dict:
    positions = _overlay_positions(candidate_model)
    detail: list[dict] = []
    for row in opening_rows:
        fen = str(row.get("fen") or "")
        side = str(row.get("side") or "")
        position_id = str(row.get("position_id") or _position_id(fen, side))
        entry = positions.get(position_id) or {}
        preferred = ""
        if entry:
            moves = entry.get("moves") if isinstance(entry.get("moves"), list) else []
            preferred = str((moves[0] or {}).get("uci") or "").lower() if moves else ""
        expected = [str(item).lower() for item in (row.get("expected_uci_any") or [])]
        current_move = _choose_uci(current_model, fen, side, search_profile=search_profile)
        candidate_move = _choose_uci(candidate_model, fen, side, search_profile=search_profile)
        detail.append({
            "id": str(row.get("id") or ""),
            "position_id": position_id,
            "overlay_position_present": bool(entry),
            "preferred_overlay_move": preferred,
            "current_move": current_move,
            "candidate_move": candidate_move,
            "expected_uci_any": expected,
            "candidate_in_expected": candidate_move in expected,
            "candidate_matches_preferred": bool(preferred and candidate_move == preferred),
            "candidate_regressed": current_move in expected and candidate_move not in expected,
        })
    return {
        "rows": detail,
        "summary": {
            "cases": len(detail),
            "overlay_position_present_count": sum(1 for row in detail if row["overlay_position_present"]),
            "candidate_in_expected_count": sum(1 for row in detail if row["candidate_in_expected"]),
            "candidate_matches_preferred_count": sum(1 for row in detail if row["candidate_matches_preferred"]),
            "candidate_regressed_count": sum(1 for row in detail if row["candidate_regressed"]),
            "pass": bool(detail)
            and all(row["overlay_position_present"] for row in detail)
            and all(row["candidate_in_expected"] for row in detail)
            and all(row["candidate_matches_preferred"] for row in detail)
            and not any(row["candidate_regressed"] for row in detail),
        },
    }


def fresh_non_overlay_audit(
    probes: list[dict],
    *,
    current_model: Path,
    candidate_model: Path,
    search_profile: str,
) -> dict:
    rows: list[dict] = []
    for probe in probes:
        current_move = _choose_uci(current_model, str(probe["fen"]), str(probe["side"]), search_profile=search_profile)
        candidate_move = _choose_uci(candidate_model, str(probe["fen"]), str(probe["side"]), search_profile=search_profile)
        rows.append({
            **probe,
            "current_move": current_move,
            "candidate_move": candidate_move,
            "fallback_unchanged": current_move == candidate_move,
        })
    return {
        "rows": rows,
        "summary": {
            "cases": len(rows),
            "overlay_overlap_count": sum(1 for row in rows if row["position_id_overlaps_overlay"]),
            "fallback_unchanged_count": sum(1 for row in rows if row["fallback_unchanged"]),
            "pass": bool(rows)
            and not any(row["position_id_overlaps_overlay"] for row in rows)
            and all(row["fallback_unchanged"] for row in rows),
        },
    }


def _clone_without_overlay(current_model: Path, output_dir: Path) -> Path:
    output = output_dir / "model_without_opening_overlay.json"
    payload = _load_model(current_model)
    payload.pop("opening_overlay", None)
    output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return output


def model_without_overlay_audit(
    rows: list[dict],
    *,
    current_model: Path,
    output_dir: Path,
    search_profile: str,
) -> dict:
    clone = _clone_without_overlay(current_model, output_dir)
    detail = []
    for row in rows:
        fen = str(row.get("fen") or "")
        side = str(row.get("side") or "")
        current_move = _choose_uci(current_model, fen, side, search_profile=search_profile)
        clone_move = _choose_uci(clone, fen, side, search_profile=search_profile)
        detail.append({
            "id": str(row.get("id") or ""),
            "fen": fen,
            "side": side,
            "current_move": current_move,
            "clone_without_overlay_move": clone_move,
            "unchanged": current_move == clone_move,
        })
    return {
        "model_without_overlay_path": str(clone),
        "rows": detail,
        "summary": {
            "cases": len(detail),
            "unchanged_count": sum(1 for row in detail if row["unchanged"]),
            "pass": bool(detail) and all(row["unchanged"] for row in detail),
        },
    }


def _move_satisfies(board: chess.Board, move_uci: str, expectation: str) -> bool:
    move = chess.Move.from_uci(move_uci)
    after = board.copy(stack=False)
    after.push(move)
    if expectation == "checkmate":
        return after.is_checkmate()
    if expectation == "promotion":
        return bool(move.promotion)
    if expectation == "en_passant":
        return board.is_en_passant(move)
    if expectation == "high_value_capture":
        captured = board.piece_at(move.to_square)
        return bool(captured and captured.piece_type in {chess.ROOK, chess.QUEEN})
    if expectation == "opening_overlay_overrides_castle":
        return move_uci in {"b5a4", "b5c6"}
    return False


def _bad_overlay_move(fen: str, side: str, *, forbidden: set[str], expectation: str = "") -> str:
    board = chess.Board(fen)
    board.turn = chess.WHITE if side == "white" else chess.BLACK
    for move in sorted(board.legal_moves, key=lambda item: item.uci()):
        if move.uci() in forbidden:
            continue
        if expectation and _move_satisfies(board, move.uci(), expectation):
            continue
        return move.uci()
    return sorted(board.legal_moves, key=lambda item: item.uci())[0].uci()


def runtime_priority_safety_audit(
    *,
    current_model: Path,
    candidate_model: Path,
    output_dir: Path,
    search_profile: str,
) -> dict:
    cases = [
        {
            "id": "forced_mate_not_overridden",
            "fen": "6k1/5Q2/6K1/8/8/8/8/8 w - - 0 1",
            "side": "white",
            "expectation": "checkmate",
        },
        {
            "id": "promotion_not_overridden",
            "fen": "8/P7/8/8/8/8/8/k1K5 w - - 0 1",
            "side": "white",
            "expectation": "promotion",
        },
        {
            "id": "en_passant_not_overridden",
            "fen": "7k/8/8/3pP3/8/8/8/4K3 w - d6 0 1",
            "side": "white",
            "expectation": "en_passant",
        },
        {
            "id": "high_value_capture_not_overridden",
            "fen": "4k3/8/8/7q/8/8/8/4K2R w K - 0 1",
            "side": "white",
            "expectation": "high_value_capture",
        },
    ]
    payload = _load_model(current_model)
    positions = {}
    for case in cases:
        board = chess.Board(case["fen"])
        side = str(case["side"])
        board.turn = chess.WHITE if side == "white" else chess.BLACK
        current_move = _choose_uci(current_model, case["fen"], side, search_profile=search_profile)
        bad = _bad_overlay_move(case["fen"], side, forbidden={current_move}, expectation=str(case["expectation"]))
        positions[_position_id(case["fen"], side)] = {
            "id": case["id"],
            "fen": case["fen"],
            "side": side,
            "moves": [{"uci": bad, "weight": 100}],
        }
        case["bad_overlay_move"] = bad
    payload["opening_overlay"] = {
        "enabled": True,
        "version": "exp5_opening_overlay_v1",
        "mode": "adversarial_priority_safety_probe",
        "max_fullmove": 99,
        "positions": positions,
    }
    adversarial_model = output_dir / "adversarial_overlay_priority_probe.json"
    adversarial_model.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    rows = []
    for case in cases:
        chosen = _choose_uci(adversarial_model, case["fen"], case["side"], search_profile=search_profile)
        board = chess.Board(case["fen"])
        board.turn = chess.WHITE if case["side"] == "white" else chess.BLACK
        rows.append({
            **case,
            "chosen_move": chosen,
            "bad_overlay_not_chosen": chosen != case["bad_overlay_move"],
            "expectation_satisfied": _move_satisfies(board, chosen, case["expectation"]),
        })
    castle_case = {
        "id": "exact_opening_overlay_may_override_castle",
        "fen": "r1bqkbnr/1ppp1ppp/p1n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4",
        "side": "white",
        "expectation": "opening_overlay_overrides_castle",
    }
    castle_move = _choose_uci(candidate_model, castle_case["fen"], castle_case["side"], search_profile=search_profile)
    board = chess.Board(castle_case["fen"])
    rows.append({
        **castle_case,
        "chosen_move": castle_move,
        "bad_overlay_move": "",
        "bad_overlay_not_chosen": True,
        "expectation_satisfied": _move_satisfies(board, castle_move, castle_case["expectation"]),
    })
    return {
        "adversarial_overlay_model_path": str(adversarial_model),
        "rows": rows,
        "summary": {
            "cases": len(rows),
            "hard_priority_cases": len(cases),
            "bad_overlay_blocked_count": sum(1 for row in rows if row["bad_overlay_not_chosen"]),
            "expectation_satisfied_count": sum(1 for row in rows if row["expectation_satisfied"]),
            "pass": bool(rows)
            and all(row["bad_overlay_not_chosen"] for row in rows)
            and all(row["expectation_satisfied"] for row in rows),
        },
    }


def _repeatability(exp5_rows: list[dict], *, candidate_model: Path, search_profile: str, seeds: list[int]) -> dict:
    runs = []
    for seed in seeds:
        rows = list(exp5_rows)
        random.Random(seed).shuffle(rows)
        retention = evaluate_retention(rows, candidate_model=candidate_model, search_profile=search_profile)
        overall = retention["overall"]
        smoke = retention["clusters"].get("smoke", {})
        endgame = retention["clusters"].get("endgame", {})
        special = retention["clusters"].get("special_rule", {})
        tactic = retention["clusters"].get("tactic", {})
        passed = (
            int(overall["clean_regressed_count"]) == 0
            and float(overall["illegal_rate"]) == 0.0
            and float(overall["suspicious_rate"]) == 0.0
            and float(endgame.get("score_delta", 0.0)) >= 0.0
            and float(smoke.get("score_delta", 0.0)) >= 0.0
            and float(special.get("score_delta", 0.0)) >= 0.0
            and float(tactic.get("score_delta", 0.0)) >= 0.0
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
            "tactic_delta": tactic.get("score_delta", 0.0),
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
        "# exp5_17 opening overlay staging validation",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- baseline_sha: `{summary['current_production_sha_actual']}`",
        f"- candidate_sha: `{summary['candidate_sha256']}`",
        f"- current_model_source: `{summary['current_model_source']}`",
        f"- runtime_mutated: `{summary['runtime_mutated']}`",
        f"- stage_promote_attempted: `{summary['stage_promote_attempted']}`",
        f"- ready_for_exp5_18_promotion_review: `{summary['ready_for_exp5_18_promotion_review']}`",
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
        f"- clean_regressed_count: `{retention['clean_regressed_count']}`",
        f"- illegal_rate: `{retention['illegal_rate']}`",
        f"- suspicious_rate: `{retention['suspicious_rate']}`",
        "",
        "## Audits",
        "",
        f"- overlay_activation_pass: `{summary['overlay_activation_audit']['summary']['pass']}`",
        f"- fresh_non_overlay_pass: `{summary['fresh_non_overlay_opening_audit']['summary']['pass']}`",
        f"- runtime_priority_safety_pass: `{summary['runtime_priority_safety_audit']['summary']['pass']}`",
        f"- model_without_overlay_pass: `{summary['model_without_overlay_audit']['summary']['pass']}`",
        f"- repeatability_pass: `{summary['repeatability']['pass']}`",
        "",
        "## Artifacts",
        "",
    ]
    for key, value in sorted(summary["artifacts"].items()):
        lines.append(f"- {key}: `{value}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_validation(
    *,
    candidate_model: Path,
    opening_curriculum: Path,
    exp5_13_summary: Path,
    production_model: Path,
    fallback_production_model: Path,
    output_dir: Path,
    search_profile: str,
    repeatability_seeds: list[int],
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    current_model, current_source = resolve_current_production_model(production_model, fallback_production_model)
    current_hash = _sha256_file(current_model)
    candidate_hash = _sha256_file(candidate_model)
    opening_rows = _iter_jsonl(opening_curriculum)
    exp5_rows = _read_summary_rows(exp5_13_summary)
    overlay_positions = _overlay_positions(candidate_model)
    fresh_probes = build_fresh_non_overlay_opening_probes(set(overlay_positions))

    opening_eval = evaluate_opening_pool(opening_rows, current_model=current_model, candidate_model=candidate_model, search_profile=search_profile)
    retention_eval = evaluate_retention(exp5_rows, candidate_model=candidate_model, search_profile=search_profile)
    activation = overlay_activation_audit(opening_rows, current_model=current_model, candidate_model=candidate_model, search_profile=search_profile)
    fresh = fresh_non_overlay_audit(fresh_probes, current_model=current_model, candidate_model=candidate_model, search_profile=search_profile)
    no_overlay = model_without_overlay_audit(opening_rows + fresh_probes, current_model=current_model, output_dir=output_dir, search_profile=search_profile)
    safety = runtime_priority_safety_audit(current_model=current_model, candidate_model=candidate_model, output_dir=output_dir, search_profile=search_profile)
    repeatability = _repeatability(exp5_rows, candidate_model=candidate_model, search_profile=search_profile, seeds=repeatability_seeds)

    artifacts = {
        "summary_json": str(output_dir / "summary.json"),
        "summary_md": str(output_dir / "SUMMARY.md"),
        "opening_evaluation": str(output_dir / "opening_evaluation.json"),
        "retention_evaluation": str(output_dir / "retention_evaluation.json"),
        "overlay_activation_audit": str(output_dir / "overlay_activation_audit.json"),
        "fresh_non_overlay_opening_audit": str(output_dir / "fresh_non_overlay_opening_audit.json"),
        "model_without_overlay_audit": str(output_dir / "model_without_overlay_audit.json"),
        "runtime_priority_safety_audit": str(output_dir / "runtime_priority_safety_audit.json"),
        "repeatability_5_seed": str(output_dir / "repeatability_5_seed.json"),
    }
    for path_key, payload in [
        ("opening_evaluation", opening_eval),
        ("retention_evaluation", retention_eval),
        ("overlay_activation_audit", activation),
        ("fresh_non_overlay_opening_audit", fresh),
        ("model_without_overlay_audit", no_overlay),
        ("runtime_priority_safety_audit", safety),
        ("repeatability_5_seed", repeatability),
    ]:
        _write_json(Path(artifacts[path_key]), payload)

    opening_overall = opening_eval["overall"]
    retention_overall = retention_eval["overall"]
    clusters = retention_eval["clusters"]
    smoke = clusters.get("smoke", {})
    endgame = clusters.get("endgame", {})
    special = clusters.get("special_rule", {})
    tactic = clusters.get("tactic", {})
    ready = (
        candidate_hash == EXP5_16_CANDIDATE_SHA
        and current_hash == CURRENT_PRODUCTION_SHA
        and int(opening_overall["candidate_passed"]) == int(opening_overall["cases"]) == 31
        and int(opening_overall["regressed_count"]) == 0
        and int(retention_overall["clean_regressed_count"]) == 0
        and float(retention_overall["illegal_rate"]) == 0.0
        and float(retention_overall["suspicious_rate"]) == 0.0
        and float(endgame.get("score_delta", 0.0)) >= 0.0
        and float(smoke.get("score_delta", 0.0)) >= 0.0
        and float(special.get("score_delta", 0.0)) >= 0.0
        and float(tactic.get("score_delta", 0.0)) >= 0.0
        and activation["summary"]["pass"]
        and fresh["summary"]["pass"]
        and no_overlay["summary"]["pass"]
        and safety["summary"]["pass"]
        and repeatability["pass"]
    )
    block_reasons = [] if ready else [
        reason
        for reason, flag in [
            ("candidate_sha_mismatch", candidate_hash != EXP5_16_CANDIDATE_SHA),
            ("baseline_sha_mismatch", current_hash != CURRENT_PRODUCTION_SHA),
            ("opening_not_31_of_31", int(opening_overall["candidate_passed"]) != 31),
            ("opening_regression", int(opening_overall["regressed_count"]) > 0),
            ("retention_clean_regression", int(retention_overall["clean_regressed_count"]) > 0),
            ("illegal_rate_nonzero", float(retention_overall["illegal_rate"]) != 0.0),
            ("suspicious_rate_nonzero", float(retention_overall["suspicious_rate"]) != 0.0),
            ("endgame_regression", float(endgame.get("score_delta", 0.0)) < 0.0),
            ("smoke_regression", float(smoke.get("score_delta", 0.0)) < 0.0),
            ("special_rule_regression", float(special.get("score_delta", 0.0)) < 0.0),
            ("tactic_regression", float(tactic.get("score_delta", 0.0)) < 0.0),
            ("overlay_activation_audit_failed", not activation["summary"]["pass"]),
            ("fresh_non_overlay_audit_failed", not fresh["summary"]["pass"]),
            ("model_without_overlay_audit_failed", not no_overlay["summary"]["pass"]),
            ("runtime_priority_safety_failed", not safety["summary"]["pass"]),
            ("repeatability_not_passed", not repeatability["pass"]),
        ]
        if flag
    ]
    summary = {
        "ok": True,
        "generated_at": _now(),
        "engine": "experiment 5:nnue",
        "validation_round": "exp5_17",
        "candidate_type": "opening_exact_position_overlay",
        "stage_promote_attempted": False,
        "runtime_mutated": False,
        "current_production_sha_expected": CURRENT_PRODUCTION_SHA,
        "current_production_sha_actual": current_hash,
        "candidate_sha_expected": EXP5_16_CANDIDATE_SHA,
        "candidate_sha256": candidate_hash,
        "current_model_path": str(current_model),
        "current_model_source": current_source,
        "candidate_model_path": str(candidate_model),
        "opening_curriculum": str(opening_curriculum),
        "exp5_13_summary": str(exp5_13_summary),
        "search_profile": search_profile,
        "opening_evaluation": {"overall": opening_eval["overall"], "detail_path": artifacts["opening_evaluation"]},
        "retention_evaluation": {"overall": retention_eval["overall"], "clusters": retention_eval["clusters"], "detail_path": artifacts["retention_evaluation"]},
        "overlay_activation_audit": {"summary": activation["summary"], "detail_path": artifacts["overlay_activation_audit"]},
        "fresh_non_overlay_opening_audit": {"summary": fresh["summary"], "detail_path": artifacts["fresh_non_overlay_opening_audit"]},
        "model_without_overlay_audit": {"summary": no_overlay["summary"], "detail_path": artifacts["model_without_overlay_audit"]},
        "runtime_priority_safety_audit": {"summary": safety["summary"], "detail_path": artifacts["runtime_priority_safety_audit"]},
        "repeatability": repeatability,
        "w6_dryrun_pipeline_smoke": {
            "run": False,
            "reason": "not run from exp5_17 validator because chess_pipeline_dryrun.py has unrelated local WIP; keep exp5_17 artifact-only and non-mutating",
        },
        "ready_for_exp5_18_promotion_review": ready,
        "verdict": "ready_for_exp5_18_promotion_review" if ready else "blocked",
        "block_reasons": block_reasons,
        "artifacts": artifacts,
    }
    _write_json(Path(artifacts["summary_json"]), summary)
    _write_summary_md(Path(artifacts["summary_md"]), summary)
    return summary


def main() -> int:
    args = parse_args()
    seeds = [int(item.strip()) for item in str(args.repeatability_seeds).split(",") if item.strip()]
    result = run_validation(
        candidate_model=Path(args.candidate_model_path),
        opening_curriculum=Path(args.opening_curriculum_jsonl),
        exp5_13_summary=Path(args.exp5_13_summary),
        production_model=Path(args.production_model_path),
        fallback_production_model=Path(args.fallback_production_model_path),
        output_dir=Path(args.output_dir),
        search_profile=str(args.search_profile),
        repeatability_seeds=seeds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
