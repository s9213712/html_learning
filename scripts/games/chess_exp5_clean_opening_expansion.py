#!/usr/bin/env python3
"""Build a clean curated opening held-out/curriculum pool for exp5.

This script is data construction only: no retraining, staging, promotion, or
runtime mutation. It converts curated SAN opening positions into verified FEN
rows with multi-good expected moves and overlap audits against existing exp5
train/benchmark artifacts.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

import chess


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.games.chess_exp5_production_readiness import _position_id  # noqa: E402
from scripts.games.chess_exp5_strength_gate import _train_row_signature  # noqa: E402
from services.games.chess_nnue import choose_experiment_nnue_move  # noqa: E402


from scripts.games.common_paths import chess_results_root, runtime_model_path  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_TRAIN_ROWS = DEFAULT_RESULTS_ROOT / "exp5_08_clean_pool" / "inputs" / "exp5_08_train_clean_only.jsonl"
DEFAULT_BENCHMARK_SUMMARY = DEFAULT_RESULTS_ROOT / "exp5_13_rule_smoke_stalemate_fix_check" / "summary.json"
DEFAULT_OUTPUT_DIR = DEFAULT_RESULTS_ROOT / "exp5_14b_clean_opening_heldout"
DEFAULT_PRODUCTION_MODEL = runtime_model_path("chess_experiment_5_nnue_experience.json")
DEFAULT_PROMOTED_STAGE_CANDIDATE = DEFAULT_RESULTS_ROOT / "exp5_08_stage_candidate" / "chess_experiment_5_nnue_stage_candidate.json"
DEFAULT_BASELINE_MODEL = DEFAULT_PRODUCTION_MODEL
DEFAULT_SEARCH_PROFILE = "fixed_depth_strong"
MIN_CLEAN_ROWS = 30


# The first expected move is the preferred book move. Additional expected moves
# are accepted multi-good alternatives from mainstream opening principles.
BOOK_POSITIONS: tuple[dict, ...] = (
    {"id": "exp5_14b_start_white", "name": "starting position", "line": [], "expected": ["e4", "d4", "Nf3", "c4"]},
    {"id": "exp5_14b_after_e4_black", "name": "open game reply", "line": ["e4"], "expected": ["e5", "c5", "e6", "c6"]},
    {"id": "exp5_14b_after_d4_black", "name": "closed game reply", "line": ["d4"], "expected": ["Nf6", "d5", "e6", "g6"]},
    {"id": "exp5_14b_after_nf3_black", "name": "reti reply", "line": ["Nf3"], "expected": ["Nf6", "d5", "c5", "g6"]},
    {"id": "exp5_14b_after_c4_black", "name": "english reply", "line": ["c4"], "expected": ["e5", "Nf6", "c5", "g6"]},
    {"id": "exp5_14b_open_game_white_2", "name": "open game development", "line": ["e4", "e5"], "expected": ["Nf3", "Nc3", "Bc4", "d4"]},
    {"id": "exp5_14b_open_game_black_2", "name": "after Nf3", "line": ["e4", "e5", "Nf3"], "expected": ["Nc6", "Nf6", "d6"]},
    {"id": "exp5_14b_italian_white_3", "name": "italian choice", "line": ["e4", "e5", "Nf3", "Nc6"], "expected": ["Bb5", "Bc4", "d4", "Nc3"]},
    {"id": "exp5_14b_italian_black_3", "name": "italian reply", "line": ["e4", "e5", "Nf3", "Nc6", "Bc4"], "expected": ["Bc5", "Nf6", "Be7", "d6"]},
    {"id": "exp5_14b_italian_white_4", "name": "giuoco piano", "line": ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"], "expected": ["c3", "d3", "O-O", "Nc3"]},
    {"id": "exp5_14b_italian_black_4", "name": "giuoco piano reply", "line": ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3"], "expected": ["Nf6", "d6", "Bb6", "a6"]},
    {"id": "exp5_14b_ruy_black_3", "name": "ruy lopez reply", "line": ["e4", "e5", "Nf3", "Nc6", "Bb5"], "expected": ["a6", "Nf6", "d6", "Bc5"]},
    {"id": "exp5_14b_ruy_white_4", "name": "ruy after a6", "line": ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"], "expected": ["Ba4", "Bxc6"]},
    {"id": "exp5_14b_ruy_black_4", "name": "ruy development", "line": ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4"], "expected": ["Nf6", "d6", "b5"]},
    {"id": "exp5_14b_sicilian_white_2", "name": "sicilian white second", "line": ["e4", "c5"], "expected": ["Nf3", "Nc3", "c3", "d4"]},
    {"id": "exp5_14b_sicilian_black_2", "name": "open sicilian reply", "line": ["e4", "c5", "Nf3"], "expected": ["d6", "Nc6", "e6", "g6"]},
    {"id": "exp5_14b_sicilian_white_3", "name": "open sicilian", "line": ["e4", "c5", "Nf3", "d6"], "expected": ["d4", "Bb5+", "Nc3", "c3"]},
    {"id": "exp5_14b_sicilian_black_3", "name": "sicilian central exchange", "line": ["e4", "c5", "Nf3", "d6", "d4"], "expected": ["cxd4", "Nf6", "g6"]},
    {"id": "exp5_14b_french_white_2", "name": "french white second", "line": ["e4", "e6"], "expected": ["d4", "Nf3", "Nc3"]},
    {"id": "exp5_14b_french_black_2", "name": "french central", "line": ["e4", "e6", "d4"], "expected": ["d5", "c5", "Nf6"]},
    {"id": "exp5_14b_french_white_3", "name": "french advance/exchange", "line": ["e4", "e6", "d4", "d5"], "expected": ["Nc3", "Nd2", "e5", "exd5"]},
    {"id": "exp5_14b_caro_white_2", "name": "caro white second", "line": ["e4", "c6"], "expected": ["d4", "Nf3", "Nc3"]},
    {"id": "exp5_14b_caro_black_2", "name": "caro central", "line": ["e4", "c6", "d4"], "expected": ["d5", "g6", "d6"]},
    {"id": "exp5_14b_caro_white_3", "name": "caro choice", "line": ["e4", "c6", "d4", "d5"], "expected": ["Nc3", "Nd2", "e5", "exd5"]},
    {"id": "exp5_14b_qgd_black_2", "name": "queen gambit declined/accepted", "line": ["d4", "d5", "c4"], "expected": ["e6", "c6", "dxc4", "Nf6"]},
    {"id": "exp5_14b_qgd_white_3", "name": "qgd white development", "line": ["d4", "d5", "c4", "e6"], "expected": ["Nc3", "Nf3", "g3"]},
    {"id": "exp5_14b_qgd_black_3", "name": "qgd black development", "line": ["d4", "d5", "c4", "e6", "Nc3"], "expected": ["Nf6", "Be7", "c6"]},
    {"id": "exp5_14b_qgd_white_4", "name": "qgd main development", "line": ["d4", "d5", "c4", "e6", "Nc3", "Nf6"], "expected": ["Bg5", "Nf3", "cxd5"]},
    {"id": "exp5_14b_slav_white_3", "name": "slav white development", "line": ["d4", "d5", "c4", "c6"], "expected": ["Nf3", "Nc3", "e3"]},
    {"id": "exp5_14b_slav_black_3", "name": "slav black development", "line": ["d4", "d5", "c4", "c6", "Nf3"], "expected": ["Nf6", "e6", "dxc4"]},
    {"id": "exp5_14b_nimzo_black_3", "name": "nimzo/bogo choice", "line": ["d4", "Nf6", "c4", "e6", "Nc3"], "expected": ["Bb4", "d5", "b6"]},
    {"id": "exp5_14b_nimzo_white_4", "name": "nimzo white choice", "line": ["d4", "Nf6", "c4", "e6", "Nc3", "Bb4"], "expected": ["e3", "Qc2", "Nf3", "a3"]},
    {"id": "exp5_14b_kid_black_3", "name": "king indian setup", "line": ["d4", "Nf6", "c4", "g6", "Nc3"], "expected": ["Bg7", "d6"]},
    {"id": "exp5_14b_kid_white_4", "name": "king indian white center", "line": ["d4", "Nf6", "c4", "g6", "Nc3", "Bg7"], "expected": ["e4", "Nf3", "g3"]},
    {"id": "exp5_14b_english_black_2", "name": "english symmetric/reversed", "line": ["c4", "e5", "Nc3"], "expected": ["Nf6", "Nc6", "Bb4", "g6"]},
    {"id": "exp5_14b_english_white_3", "name": "english development", "line": ["c4", "e5", "Nc3", "Nf6"], "expected": ["Nf3", "g3", "e3"]},
    {"id": "exp5_14b_reti_black_2", "name": "reti black reply", "line": ["Nf3", "d5", "c4"], "expected": ["e6", "c6", "d4", "Nf6"]},
    {"id": "exp5_14b_reti_white_3", "name": "reti white setup", "line": ["Nf3", "d5", "c4", "e6"], "expected": ["g3", "d4", "e3"]},
    {"id": "exp5_14b_london_black_3", "name": "london black development", "line": ["d4", "Nf6", "Bf4"], "expected": ["d5", "e6", "g6", "c5"]},
    {"id": "exp5_14b_london_white_4", "name": "london white development", "line": ["d4", "Nf6", "Bf4", "d5"], "expected": ["e3", "Nf3", "c3"]},
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build exp5 clean opening held-out/curriculum rows.")
    parser.add_argument("--train-rows-jsonl", action="append", default=[str(DEFAULT_TRAIN_ROWS)])
    parser.add_argument("--benchmark-summary", default=str(DEFAULT_BENCHMARK_SUMMARY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--production-model-path", default=str(DEFAULT_PRODUCTION_MODEL))
    parser.add_argument("--fallback-production-model-path", default=str(DEFAULT_PROMOTED_STAGE_CANDIDATE))
    parser.add_argument("--baseline-model-path", default=str(DEFAULT_BASELINE_MODEL))
    parser.add_argument("--search-profile", default=DEFAULT_SEARCH_PROFILE)
    parser.add_argument("--min-clean-rows", type=int, default=MIN_CLEAN_ROWS)
    return parser.parse_args()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _iter_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _read_summary_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [dict(row) for row in (((payload.get("benchmark") or {}).get("rows")) or []) if isinstance(row, dict)]


def _hash_rows(rows: list[dict]) -> str:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _apply_san_line(line: list[str]) -> chess.Board:
    board = chess.Board()
    for san in line:
        move = board.parse_san(san)
        board.push(move)
    return board


def _expected_uci(board: chess.Board, expected_san: list[str]) -> list[str]:
    expected: list[str] = []
    for san in expected_san:
        move = board.parse_san(san)
        uci = move.uci()
        if uci not in expected:
            expected.append(uci)
    return expected


def _side_name(board: chess.Board) -> str:
    return "white" if board.turn == chess.WHITE else "black"


def _engine_move(model_path: Path, fen: str, side: str, *, search_profile: str) -> str:
    if not model_path.exists():
        return ""
    try:
        move = choose_experiment_nnue_move({"__fen__": fen}, side, model_path=model_path, search_profile=search_profile)
    except Exception:
        return ""
    if not move:
        return ""
    return f"{move.get('from', '')}{move.get('to', '')}{move.get('promotion') or ''}".lower()


def _resolve_evaluation_model(production_model: Path, fallback_model: Path) -> tuple[Path, str]:
    if production_model.exists():
        return production_model, "runtime_production"
    if fallback_model.exists():
        return fallback_model, "promoted_stage_candidate_fallback"
    return production_model, "missing"


def _build_curated_rows() -> list[dict]:
    rows: list[dict] = []
    for item in BOOK_POSITIONS:
        board = _apply_san_line(list(item["line"]))
        expected = _expected_uci(board, list(item["expected"]))
        side = _side_name(board)
        fen = board.fen()
        row = {
            "id": item["id"],
            "fen": fen,
            "side": side,
            "category": "opening",
            "subcategory": str(item["name"]),
            "label_quality": "clean",
            "source": "curated_opening_book_v1",
            "source_line_san": list(item["line"]),
            "expected_san_any": list(item["expected"]),
            "expected_uci_any": expected,
            "teacher_move": expected[0],
            "teacher_top3": expected[:3],
            "teacher_top5": expected[:5],
            "multi_good": len(expected) > 1,
            "confidence": 0.95,
            "true_heldout": True,
            "position_id": _position_id(fen, side),
        }
        rows.append(row)
    return rows


def _dedupe_and_audit(rows: list[dict], *, train_rows: list[dict], benchmark_rows: list[dict]) -> tuple[list[dict], dict]:
    train_signatures = {_train_row_signature(row) for row in train_rows}
    train_position_ids = {
        _position_id(
            str(row.get("fen") or row.get("board_fen") or ""),
            str(row.get("side") or ("white" if " w " in str(row.get("fen") or row.get("board_fen") or "") else "black")).strip().lower(),
        )
        for row in train_rows
        if str(row.get("fen") or row.get("board_fen") or "").strip()
    }
    benchmark_signatures = {
        _train_row_signature({"fen": str(row.get("fen") or ""), "side": str(row.get("side") or "")})
        for row in benchmark_rows
        if str(row.get("fen") or "").strip()
    }
    benchmark_position_ids = {
        str(row.get("position_id") or _position_id(str(row.get("fen") or ""), str(row.get("side") or "")))
        for row in benchmark_rows
        if str(row.get("fen") or "").strip()
    }

    seen_signatures: set[str] = set()
    seen_position_ids: set[str] = set()
    kept: list[dict] = []
    skipped: list[dict] = []
    train_overlap_count = 0
    benchmark_overlap_count = 0
    position_overlap_count = 0
    for row in rows:
        signature = _train_row_signature(row)
        position_id = str(row["position_id"])
        reason = ""
        if signature in seen_signatures or position_id in seen_position_ids:
            reason = "duplicate_curated_position"
        elif signature in train_signatures or position_id in train_position_ids:
            reason = "train_overlap"
            train_overlap_count += 1
            position_overlap_count += int(position_id in train_position_ids)
        elif signature in benchmark_signatures or position_id in benchmark_position_ids:
            reason = "benchmark_overlap"
            benchmark_overlap_count += 1
            position_overlap_count += int(position_id in benchmark_position_ids)
        if reason:
            skipped.append({"id": row["id"], "reason": reason, "fen": row["fen"], "side": row["side"]})
            continue
        seen_signatures.add(signature)
        seen_position_ids.add(position_id)
        kept.append(row)

    return kept, {
        "raw_rows": len(rows),
        "kept_rows": len(kept),
        "skipped_rows": len(skipped),
        "skipped": skipped,
        "train_signature_count": len(train_signatures),
        "benchmark_signature_count": len(benchmark_signatures),
        "raw_train_vs_curated_overlap_count": train_overlap_count,
        "raw_benchmark_vs_curated_overlap_count": benchmark_overlap_count,
        "raw_position_id_overlap_count": position_overlap_count,
        "train_vs_curated_overlap_count": 0,
        "benchmark_vs_curated_overlap_count": 0,
        "position_id_overlap_count": 0,
        "curated_duplicate_position_count": len(rows) - len({str(row["position_id"]) for row in rows}),
    }


def _evaluate_rows(rows: list[dict], *, production_model: Path, baseline_model: Path, search_profile: str) -> dict:
    detailed: list[dict] = []
    for row in rows:
        expected = set(row["expected_uci_any"])
        production_move = _engine_move(production_model, row["fen"], row["side"], search_profile=search_profile)
        baseline_move = _engine_move(baseline_model, row["fen"], row["side"], search_profile=search_profile)
        detailed.append({
            **row,
            "production_move": production_move,
            "production_pass": production_move in expected,
            "baseline_move": baseline_move,
            "baseline_pass": baseline_move in expected,
        })
    total = max(1, len(detailed))
    production_passed = sum(1 for row in detailed if row["production_pass"])
    baseline_passed = sum(1 for row in detailed if row["baseline_pass"])
    return {
        "rows": detailed,
        "overall": {
            "cases": len(detailed),
            "production_passed": production_passed,
            "production_score": round(production_passed / total, 6),
            "baseline_passed": baseline_passed,
            "baseline_score": round(baseline_passed / total, 6),
            "score_delta": round((production_passed - baseline_passed) / total, 6),
        },
        "by_side": {
            side: {
                "cases": sum(1 for row in detailed if row["side"] == side),
                "production_passed": sum(1 for row in detailed if row["side"] == side and row["production_pass"]),
                "baseline_passed": sum(1 for row in detailed if row["side"] == side and row["baseline_pass"]),
            }
            for side in ("white", "black")
        },
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _write_summary_md(path: Path, summary: dict) -> None:
    lines = [
        "# exp5_14b clean opening held-out expansion",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- clean_opening_rows: `{summary['clean_opening_rows']}`",
        f"- min_clean_rows: `{summary['min_clean_rows']}`",
        f"- pass: `{summary['pass']}`",
        f"- production_model_path: `{summary['production_model_path']}`",
        f"- production_model_exists: `{summary['production_model_exists']}`",
        f"- evaluation_model_path: `{summary['evaluation_model_path']}`",
        f"- evaluation_model_source: `{summary['evaluation_model_source']}`",
        f"- search_profile: `{summary['search_profile']}`",
        "",
        "## Overlap",
        "",
        f"- train_vs_curated_overlap_count: `{summary['overlap']['train_vs_curated_overlap_count']}`",
        f"- benchmark_vs_curated_overlap_count: `{summary['overlap']['benchmark_vs_curated_overlap_count']}`",
        f"- position_id_overlap_count: `{summary['overlap']['position_id_overlap_count']}`",
        f"- raw_train_vs_curated_overlap_count: `{summary['overlap']['raw_train_vs_curated_overlap_count']}`",
        f"- raw_benchmark_vs_curated_overlap_count: `{summary['overlap']['raw_benchmark_vs_curated_overlap_count']}`",
        f"- skipped_rows: `{summary['overlap']['skipped_rows']}`",
        "",
        "## Evaluation",
        "",
        f"- production_score: `{summary['evaluation']['overall']['production_score']}`",
        f"- baseline_score: `{summary['evaluation']['overall']['baseline_score']}`",
        f"- score_delta: `{summary['evaluation']['overall']['score_delta']}`",
        "",
        "## Artifacts",
        "",
    ]
    for name, artifact in sorted(summary["artifacts"].items()):
        lines.append(f"- {name}: `{artifact}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_clean_opening_expansion(
    *,
    train_paths: list[Path],
    benchmark_summary: Path,
    output_dir: Path,
    production_model: Path,
    fallback_production_model: Path = DEFAULT_PROMOTED_STAGE_CANDIDATE,
    baseline_model: Path,
    search_profile: str = DEFAULT_SEARCH_PROFILE,
    min_clean_rows: int = MIN_CLEAN_ROWS,
) -> dict:
    train_rows: list[dict] = []
    for path in train_paths:
        train_rows.extend(_iter_jsonl(path))
    benchmark_rows = _read_summary_rows(benchmark_summary)
    curated = _build_curated_rows()
    clean_rows, overlap = _dedupe_and_audit(curated, train_rows=train_rows, benchmark_rows=benchmark_rows)
    evaluation_model, evaluation_model_source = _resolve_evaluation_model(production_model, fallback_production_model)
    evaluation = _evaluate_rows(clean_rows, production_model=evaluation_model, baseline_model=baseline_model, search_profile=search_profile)

    quality = Counter(str(row.get("label_quality") or "unspecified") for row in clean_rows)
    source = Counter(str(row.get("source") or "unspecified") for row in clean_rows)
    artifacts = {
        "clean_opening_cases_jsonl": str(output_dir / "clean_opening_cases.jsonl"),
        "clean_opening_heldout_jsonl": str(output_dir / "clean_opening_heldout.jsonl"),
        "clean_opening_curriculum_jsonl": str(output_dir / "clean_opening_curriculum.jsonl"),
        "evaluation_json": str(output_dir / "clean_opening_evaluation.json"),
        "summary_json": str(output_dir / "summary.json"),
        "summary_md": str(output_dir / "SUMMARY.md"),
    }
    result = {
        "ok": True,
        "generated_at": _now(),
        "pass": len(clean_rows) >= min_clean_rows,
        "min_clean_rows": min_clean_rows,
        "clean_opening_rows": len(clean_rows),
        "true_heldout_rows": sum(1 for row in clean_rows if row.get("true_heldout")),
        "multi_good_rows": sum(1 for row in clean_rows if row.get("multi_good")),
        "label_quality_counts": dict(sorted(quality.items())),
        "source_counts": dict(sorted(source.items())),
        "train_paths": [str(path) for path in train_paths],
        "benchmark_summary": str(benchmark_summary),
        "production_model_path": str(production_model),
        "production_model_exists": production_model.exists(),
        "fallback_production_model_path": str(fallback_production_model),
        "fallback_production_model_exists": fallback_production_model.exists(),
        "evaluation_model_path": str(evaluation_model),
        "evaluation_model_exists": evaluation_model.exists(),
        "evaluation_model_source": evaluation_model_source,
        "baseline_model_path": str(baseline_model),
        "baseline_model_exists": baseline_model.exists(),
        "search_profile": search_profile,
        "dataset_hash": _hash_rows(clean_rows),
        "overlap": overlap,
        "evaluation": evaluation,
        "artifacts": artifacts,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(Path(artifacts["clean_opening_cases_jsonl"]), clean_rows)
    _write_jsonl(Path(artifacts["clean_opening_heldout_jsonl"]), clean_rows)
    _write_jsonl(Path(artifacts["clean_opening_curriculum_jsonl"]), clean_rows)
    _write_json(Path(artifacts["evaluation_json"]), evaluation)
    _write_json(Path(artifacts["summary_json"]), result)
    _write_summary_md(Path(artifacts["summary_md"]), result)
    return result


def main() -> int:
    args = parse_args()
    result = build_clean_opening_expansion(
        train_paths=[Path(path) for path in args.train_rows_jsonl],
        benchmark_summary=Path(args.benchmark_summary),
        output_dir=Path(args.output_dir),
        production_model=Path(args.production_model_path),
        fallback_production_model=Path(args.fallback_production_model_path),
        baseline_model=Path(args.baseline_model_path),
        search_profile=args.search_profile,
        min_clean_rows=args.min_clean_rows,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
