#!/usr/bin/env python3
"""Audit exp5 opening labels before using them for the next candidate search.

This is intentionally read-only with respect to models and runtime artifacts.
It consumes an exp5 production-readiness summary, classifies opening misses,
and writes rows that can be reviewed before any opening curriculum is built.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys

import chess


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.games.chess_exp5_production_readiness import _static_move_ranking, _static_move_lookup  # noqa: E402


from scripts.games.common_paths import chess_results_root  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_INPUT = DEFAULT_RESULTS_ROOT / "exp5_13_rule_smoke_stalemate_fix_check" / "summary.json"
DEFAULT_OUTPUT_DIR = DEFAULT_RESULTS_ROOT / "exp5_14_opening_label_audit"
STATIC_EQUIVALENT_CP = 50


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit exp5 opening label quality from a production-readiness summary.")
    parser.add_argument("--input-summary", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--static-equivalent-cp", type=int, default=STATIC_EQUIVALENT_CP)
    return parser.parse_args()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _read_summary(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _opening_rows(summary: dict) -> list[dict]:
    rows = (((summary.get("benchmark") or {}).get("rows")) or [])
    return [dict(row) for row in rows if str(row.get("category") or "").strip().lower() == "opening"]


def _move_static_detail(ranking: list[dict], move: str, *, best_score: int) -> dict:
    row = _static_move_lookup(ranking, move)
    if not row:
        return {
            "move": move,
            "available": False,
            "score": None,
            "delta_vs_best": None,
            "ordinal_rank": None,
            "dense_score_rank": None,
        }
    score = int(row["score"])
    return {
        "move": move,
        "available": True,
        "score": score,
        "delta_vs_best": score - best_score,
        "ordinal_rank": int(row["ordinal_rank"]),
        "dense_score_rank": int(row["dense_score_rank"]),
    }


def _expected_static_summary(ranking: list[dict], expected: list[str], *, best_score: int) -> dict:
    details = [_move_static_detail(ranking, move, best_score=best_score) for move in expected]
    available = [detail for detail in details if detail["available"]]
    if not available:
        return {
            "moves": details,
            "best_expected_score": None,
            "best_expected_move": "",
            "best_expected_delta_vs_best": None,
            "best_expected_dense_rank": None,
        }
    best = max(available, key=lambda detail: (int(detail["score"]), str(detail["move"])))
    return {
        "moves": details,
        "best_expected_score": best["score"],
        "best_expected_move": best["move"],
        "best_expected_delta_vs_best": best["delta_vs_best"],
        "best_expected_dense_rank": best["dense_score_rank"],
    }


def _classify_opening_row(row: dict, *, static_equivalent_cp: int) -> dict:
    fen = str(row.get("fen") or "").strip()
    side = str(row.get("side") or ("white" if " w " in fen else "black")).strip().lower()
    label_quality = str(row.get("label_quality") or "unspecified").strip().lower()
    expected = [str(item).strip().lower() for item in (row.get("expected_uci_any") or []) if str(item).strip()]
    teacher = str(row.get("teacher_move") or (expected[0] if expected else "")).strip().lower()
    baseline = str(row.get("baseline_move") or "").strip().lower()
    candidate = str(row.get("candidate_move") or "").strip().lower()
    baseline_pass = bool(row.get("baseline_pass"))
    candidate_pass = bool(row.get("candidate_pass"))
    candidate_regressed = bool(row.get("candidate_regressed"))

    try:
        board = chess.Board(fen)
        ranking = _static_move_ranking(board, side)
        legal = True
        invalid_reason = ""
    except Exception as exc:
        ranking = []
        legal = False
        invalid_reason = str(exc)

    best_score = int(ranking[0]["score"]) if ranking else 0
    top5 = [str(item["move"]) for item in ranking[:5]]
    top10 = [str(item["move"]) for item in ranking[:10]]
    expected_summary = _expected_static_summary(ranking, expected, best_score=best_score)
    teacher_detail = _move_static_detail(ranking, teacher, best_score=best_score) if teacher else {}
    baseline_detail = _move_static_detail(ranking, baseline, best_score=best_score) if baseline else {}
    candidate_detail = _move_static_detail(ranking, candidate, best_score=best_score) if candidate else {}

    best_expected_score = expected_summary["best_expected_score"]
    candidate_delta_vs_expected = None
    baseline_delta_vs_expected = None
    if best_expected_score is not None and candidate_detail.get("available"):
        candidate_delta_vs_expected = int(candidate_detail["score"]) - int(best_expected_score)
    if best_expected_score is not None and baseline_detail.get("available"):
        baseline_delta_vs_expected = int(baseline_detail["score"]) - int(best_expected_score)

    candidate_near_expected = (
        candidate_delta_vs_expected is not None
        and candidate_delta_vs_expected >= -static_equivalent_cp
        and int(candidate_detail.get("delta_vs_best") or -999999) >= -static_equivalent_cp
    )
    baseline_near_expected = (
        baseline_delta_vs_expected is not None
        and baseline_delta_vs_expected >= -static_equivalent_cp
        and int(baseline_detail.get("delta_vs_best") or -999999) >= -static_equivalent_cp
    )
    teacher_static_weak = bool(teacher_detail and teacher_detail.get("available") and int(teacher_detail.get("delta_vs_best") or 0) < -static_equivalent_cp)
    candidate_in_expected = candidate in expected
    baseline_in_expected = baseline in expected
    candidate_in_top5 = candidate in top5
    baseline_in_top5 = baseline in top5

    if not legal:
        classification = "fixture_issue"
        recommended_action = "fix_invalid_fen_before_training_or_gate"
    elif label_quality != "clean":
        if candidate_near_expected or candidate_in_top5:
            classification = "multi_good_opening_equivalent"
            recommended_action = "do_not_gate_as_failure; consider audited multi-good label"
        elif teacher_static_weak:
            classification = "teacher_label_too_narrow"
            recommended_action = "refresh_with_stronger_teacher_or_opening_book"
        else:
            classification = "questionable_label_do_not_gate"
            recommended_action = "exclude from production blockers; send to stronger-teacher audit"
    elif candidate_regressed and not candidate_near_expected and not candidate_in_top5:
        classification = "true_opening_regression"
        recommended_action = "block production candidate until repaired"
    elif not candidate_pass and (candidate_near_expected or candidate_in_top5):
        classification = "multi_good_opening_equivalent"
        recommended_action = "add audited multi-good label or soft opening policy"
    elif teacher_static_weak:
        classification = "teacher_label_too_narrow"
        recommended_action = "replace label before training"
    else:
        classification = "needs_stronger_teacher"
        recommended_action = "send to stronger teacher before use in exp5_15"

    return {
        "case_id": str(row.get("id") or ""),
        "fen": fen,
        "side": side,
        "subcategory": str(row.get("subcategory") or ""),
        "label_quality": label_quality,
        "true_heldout": bool(row.get("true_heldout")),
        "teacher_move": teacher,
        "expected_uci_any": expected,
        "baseline_move": baseline,
        "candidate_move": candidate,
        "baseline_pass": baseline_pass,
        "candidate_pass": candidate_pass,
        "candidate_improved": bool(row.get("candidate_improved")),
        "candidate_regressed": candidate_regressed,
        "candidate_reasons": list(row.get("candidate_reasons") or []),
        "classification": classification,
        "recommended_action": recommended_action,
        "candidate_in_expected": candidate_in_expected,
        "baseline_in_expected": baseline_in_expected,
        "candidate_in_static_top5": candidate_in_top5,
        "baseline_in_static_top5": baseline_in_top5,
        "candidate_near_expected_static": candidate_near_expected,
        "baseline_near_expected_static": baseline_near_expected,
        "teacher_static_weak": teacher_static_weak,
        "candidate_delta_vs_best": candidate_detail.get("delta_vs_best"),
        "candidate_delta_vs_best_expected": candidate_delta_vs_expected,
        "baseline_delta_vs_best": baseline_detail.get("delta_vs_best"),
        "baseline_delta_vs_best_expected": baseline_delta_vs_expected,
        "teacher_static": teacher_detail,
        "baseline_static": baseline_detail,
        "candidate_static": candidate_detail,
        "expected_static": expected_summary,
        "static_top5": top5,
        "static_top10": top10,
        "invalid_reason": invalid_reason,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _write_summary_md(path: Path, summary: dict) -> None:
    lines = [
        "# exp5_14 opening label audit",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- input_summary: `{summary['input_summary']}`",
        f"- rows: `{summary['opening_rows']}`",
        f"- failed_rows: `{summary['opening_failed_rows']}`",
        f"- candidate_regressed_rows: `{summary['candidate_regressed_rows']}`",
        f"- clean_opening_rows: `{summary['clean_opening_rows']}`",
        f"- clean_true_opening_regressions: `{summary['clean_true_opening_regressions']}`",
        f"- questionable_rows: `{summary['questionable_rows']}`",
        f"- questionable_rows_block_production: `{summary['questionable_rows_block_production']}`",
        "",
        "## Classification",
        "",
    ]
    for name, count in sorted(summary["classification_counts"].items()):
        lines.append(f"- {name}: `{count}`")
    lines.extend([
        "",
        "## Decision",
        "",
        f"- production_blocker: `{summary['production_blocker']}`",
        f"- exp5_15_clean_opening_curriculum_rows: `{summary['exp5_15_clean_opening_curriculum_rows']}`",
        f"- next_action: `{summary['next_action']}`",
        "",
        "## Artifacts",
        "",
        f"- opening_label_audit_jsonl: `{summary['artifacts']['opening_label_audit_jsonl']}`",
        f"- opening_fail_rows_jsonl: `{summary['artifacts']['opening_fail_rows_jsonl']}`",
        f"- clean_opening_curriculum_jsonl: `{summary['artifacts']['clean_opening_curriculum_jsonl']}`",
        f"- summary_json: `{summary['artifacts']['summary_json']}`",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(input_summary: Path, output_dir: Path, *, static_equivalent_cp: int = STATIC_EQUIVALENT_CP) -> dict:
    summary = _read_summary(input_summary)
    opening = _opening_rows(summary)
    audited = [_classify_opening_row(row, static_equivalent_cp=static_equivalent_cp) for row in opening]
    failed = [row for row in audited if not row["candidate_pass"] or not row["baseline_pass"]]
    clean_curriculum = [
        {
            "fen": row["fen"],
            "side": row["side"],
            "teacher_move": row["teacher_move"],
            "teacher_top5": row["expected_uci_any"],
            "source_case_id": row["case_id"],
            "label_quality": "clean",
            "category": "opening",
            "subcategory": row["subcategory"],
        }
        for row in audited
        if row["label_quality"] == "clean" and row["classification"] not in {"true_opening_regression", "fixture_issue"}
    ]

    class_counts = Counter(row["classification"] for row in audited)
    quality_counts = Counter(row["label_quality"] for row in audited)
    clean_true_regressions = sum(1 for row in audited if row["classification"] == "true_opening_regression" and row["label_quality"] == "clean")
    artifacts = {
        "opening_label_audit_jsonl": str(output_dir / "opening_label_audit.jsonl"),
        "opening_fail_rows_jsonl": str(output_dir / "opening_fail_rows.jsonl"),
        "clean_opening_curriculum_jsonl": str(output_dir / "clean_opening_curriculum.jsonl"),
        "summary_json": str(output_dir / "summary.json"),
        "summary_md": str(output_dir / "SUMMARY.md"),
    }
    result = {
        "ok": True,
        "generated_at": _now(),
        "input_summary": str(input_summary),
        "static_equivalent_cp": static_equivalent_cp,
        "opening_rows": len(audited),
        "opening_failed_rows": len(failed),
        "candidate_regressed_rows": sum(1 for row in audited if row["candidate_regressed"]),
        "clean_opening_rows": quality_counts.get("clean", 0),
        "questionable_rows": quality_counts.get("questionable", 0),
        "classification_counts": dict(sorted(class_counts.items())),
        "label_quality_counts": dict(sorted(quality_counts.items())),
        "clean_true_opening_regressions": clean_true_regressions,
        "questionable_rows_block_production": False,
        "production_blocker": clean_true_regressions > 0,
        "exp5_15_clean_opening_curriculum_rows": len(clean_curriculum),
        "next_action": "build_curated_opening_book_or_stronger_teacher_labels_before_training",
        "artifacts": artifacts,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(Path(artifacts["opening_label_audit_jsonl"]), audited)
    _write_jsonl(Path(artifacts["opening_fail_rows_jsonl"]), failed)
    _write_jsonl(Path(artifacts["clean_opening_curriculum_jsonl"]), clean_curriculum)
    _write_json(Path(artifacts["summary_json"]), result)
    _write_summary_md(Path(artifacts["summary_md"]), result)
    return result


def main() -> int:
    args = parse_args()
    result = run_audit(Path(args.input_summary), Path(args.output_dir), static_equivalent_cp=args.static_equivalent_cp)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
