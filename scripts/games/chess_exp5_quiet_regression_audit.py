#!/usr/bin/env python3
"""Audit exp5 quiet-positional clean regressions without training or promotion."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import chess


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.self_play_training import _teacher_static_eval  # noqa: E402


from scripts.games.common_paths import chess_results_root  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_SUMMARY = DEFAULT_RESULTS_ROOT / "exp5_10_production_readiness" / "summary.json"
DEFAULT_CASES = DEFAULT_RESULTS_ROOT / "exp5_10_production_readiness" / "exp5_10_benchmark_cases.jsonl"
DEFAULT_OUTPUT_DIR = DEFAULT_RESULTS_ROOT / "exp5_11b_quiet_regression_audit"
MULTI_GOOD_CP_WINDOW = 50


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit exp5 quiet-positional clean regressions.")
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY))
    parser.add_argument("--cases-jsonl", default=str(DEFAULT_CASES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--multi-good-cp-window", type=int, default=MULTI_GOOD_CP_WINDOW)
    return parser.parse_args()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _iter_jsonl(path: Path) -> list[dict]:
    rows = []
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


def _case_index(cases_path: Path) -> dict[str, dict]:
    return {str(row.get("id") or ""): row for row in _iter_jsonl(cases_path)}


def _status_names(status: int) -> list[str]:
    names = []
    for name, value in chess.__dict__.items():
        if name.startswith("STATUS_") and isinstance(value, int) and status & value:
            names.append(name)
    return sorted(names) or ["STATUS_VALID"]


def _move_score(board: chess.Board, side: str, uci: str) -> int | None:
    try:
        move = chess.Move.from_uci(uci)
    except Exception:
        return None
    if move not in board.legal_moves:
        return None
    after = board.copy(stack=False)
    after.push(move)
    if after.is_checkmate():
        return 1_000_000
    color_sign = 1 if str(side).lower() == "white" else -1
    return int(color_sign * _teacher_static_eval(after))


def _static_ranking(board: chess.Board, side: str) -> list[dict]:
    rows = []
    for move in board.legal_moves:
        score = _move_score(board, side, move.uci())
        rows.append({"move": move.uci(), "score": int(score if score is not None else -10_000_000)})
    rows.sort(key=lambda row: (-int(row["score"]), str(row["move"])))
    dense_rank = 0
    previous_score: int | None = None
    for index, row in enumerate(rows, start=1):
        score = int(row["score"])
        if previous_score is None or score != previous_score:
            dense_rank += 1
            previous_score = score
        row["rank"] = index
        row["dense_score_rank"] = dense_rank
    return rows


def _lookup_rank(ranking: list[dict], move: str) -> int | None:
    for row in ranking:
        if row["move"] == move:
            return int(row["rank"])
    return None


def _lookup_dense_rank(ranking: list[dict], move: str) -> int | None:
    for row in ranking:
        if row["move"] == move:
            return int(row["dense_score_rank"])
    return None


def _lookup_score(ranking: list[dict], move: str) -> int | None:
    for row in ranking:
        if row["move"] == move:
            return int(row["score"])
    return None


def _classify(row: dict, board: chess.Board, ranking: list[dict], *, multi_good_cp_window: int) -> tuple[str, list[str], bool]:
    reasons: list[str] = []
    if not board.is_valid():
        reasons.append("invalid_fen")
        return "fixture_issue", reasons, False

    teacher_top3 = [str(item).lower() for item in row["teacher_top3"]]
    teacher_top5 = [str(item).lower() for item in row["teacher_top5"]]
    candidate = str(row["candidate_move"]).lower()
    teacher = str(row["teacher_move"]).lower()
    baseline = str(row["baseline_move"]).lower()
    candidate_score = _lookup_score(ranking, candidate)
    teacher_score = _lookup_score(ranking, teacher)
    baseline_score = _lookup_score(ranking, baseline)
    candidate_rank = _lookup_rank(ranking, candidate)

    if str(row.get("label_quality") or "") in {"questionable", "review"}:
        reasons.append(f"label_quality_{row.get('label_quality')}")
        return "label_quality_issue", reasons, False
    if candidate_score is None:
        reasons.append("candidate_move_illegal_or_unscored")
        return "fixture_issue", reasons, False
    if teacher_score is None:
        reasons.append("teacher_move_illegal_or_unscored")
        return "label_quality_issue", reasons, False
    if baseline_score is None:
        reasons.append("baseline_move_illegal_or_unscored")
    if candidate in teacher_top3:
        reasons.append("candidate_in_recorded_teacher_top3")
        return "multi_good_scoring_issue", reasons, True
    if candidate in teacher_top5:
        reasons.append("candidate_in_recorded_teacher_top5")
        return "multi_good_scoring_issue", reasons, True
    if candidate_rank is not None and candidate_rank <= 5:
        reasons.append("candidate_in_recomputed_static_top5")
        return "multi_good_scoring_issue", reasons, True
    if candidate_score - teacher_score >= -multi_good_cp_window:
        reasons.append(f"candidate_within_{multi_good_cp_window}cp_of_teacher_static_eval")
        return "multi_good_scoring_issue", reasons, True
    if baseline in teacher_top3 or baseline in teacher_top5:
        reasons.append("baseline_in_recorded_teacher_topk_candidate_not")
    if baseline_score is not None and candidate_score < baseline_score:
        reasons.append("candidate_static_eval_below_baseline")
    return "true_model_regression", reasons, False


def _audit(summary: dict, cases: dict[str, dict], *, multi_good_cp_window: int) -> list[dict]:
    rows = []
    for row in summary.get("benchmark", {}).get("rows", []):
        if row.get("category") != "quiet_positional":
            continue
        if not row.get("candidate_regressed"):
            continue
        if str(row.get("label_quality") or "") != "clean":
            continue
        case = cases.get(str(row.get("id") or ""), {})
        teacher_top3 = [str(item).lower() for item in (case.get("teacher_top3") or [])]
        teacher_top5 = [str(item).lower() for item in (case.get("teacher_top5") or [])]
        fen = str(row.get("fen") or "")
        side = str(row.get("side") or case.get("side") or "")
        board = chess.Board(fen)
        ranking = _static_ranking(board, side) if board.is_valid() else []
        merged = {
            **row,
            "teacher_top3": teacher_top3,
            "teacher_top5": teacher_top5,
        }
        classification, reasons, multi_good = _classify(merged, board, ranking, multi_good_cp_window=multi_good_cp_window)
        teacher_move = str(row.get("teacher_move") or case.get("teacher_move") or "").lower()
        baseline_move = str(row.get("baseline_move") or "").lower()
        candidate_move = str(row.get("candidate_move") or "").lower()
        teacher_score = _lookup_score(ranking, teacher_move)
        baseline_score = _lookup_score(ranking, baseline_move)
        candidate_score = _lookup_score(ranking, candidate_move)
        rows.append({
            "case_id": str(row.get("id") or ""),
            "fen": fen,
            "side": side,
            "category": str(row.get("category") or ""),
            "subcategory": str(row.get("subcategory") or ""),
            "label_quality": str(row.get("label_quality") or ""),
            "teacher_move": teacher_move,
            "teacher_top3": teacher_top3,
            "teacher_top5": teacher_top5,
            "recomputed_static_top5": [item["move"] for item in ranking[:5]],
            "baseline_move": baseline_move,
            "candidate_move": candidate_move,
            "baseline_pass": bool(row.get("baseline_pass")),
            "candidate_pass": bool(row.get("candidate_pass")),
            "baseline_in_teacher_top3": baseline_move in teacher_top3,
            "baseline_in_teacher_top5": baseline_move in teacher_top5,
            "candidate_in_teacher_top3": candidate_move in teacher_top3,
            "candidate_in_teacher_top5": candidate_move in teacher_top5,
            "baseline_static_rank": _lookup_rank(ranking, baseline_move),
            "candidate_static_rank": _lookup_rank(ranking, candidate_move),
            "candidate_static_dense_score_rank": _lookup_dense_rank(ranking, candidate_move),
            "teacher_static_rank": _lookup_rank(ranking, teacher_move),
            "teacher_static_dense_score_rank": _lookup_dense_rank(ranking, teacher_move),
            "baseline_static_score": baseline_score,
            "candidate_static_score": candidate_score,
            "teacher_static_score": teacher_score,
            "static_eval_delta_candidate_vs_teacher": (
                int(candidate_score - teacher_score)
                if candidate_score is not None and teacher_score is not None
                else None
            ),
            "static_eval_delta_candidate_vs_baseline": (
                int(candidate_score - baseline_score)
                if candidate_score is not None and baseline_score is not None
                else None
            ),
            "board_valid": bool(board.is_valid()),
            "board_status_names": _status_names(int(board.status())),
            "is_multi_good_possible": multi_good,
            "classification": classification,
            "classification_reasons": reasons,
        })
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _summary(rows: list[dict], *, summary_path: Path, cases_path: Path, output_dir: Path) -> dict:
    by_class: dict[str, int] = {}
    for row in rows:
        key = str(row.get("classification") or "unknown")
        by_class[key] = by_class.get(key, 0) + 1
    blocker_cleared = bool(rows) and by_class.get("true_model_regression", 0) == 0 and by_class.get("fixture_issue", 0) == 0
    return {
        "ok": True,
        "generated_at": _now(),
        "source_summary_json": str(summary_path),
        "source_cases_jsonl": str(cases_path),
        "output_dir": str(output_dir),
        "quiet_clean_regression_count": len(rows),
        "classification_counts": by_class,
        "true_model_regression_count": by_class.get("true_model_regression", 0),
        "multi_good_scoring_issue_count": by_class.get("multi_good_scoring_issue", 0),
        "label_quality_issue_count": by_class.get("label_quality_issue", 0),
        "fixture_issue_count": by_class.get("fixture_issue", 0),
        "needs_manual_review_count": by_class.get("needs_manual_review", 0),
        "production_implication": (
            "no quiet clean regressions remain in the source exp5_10 summary"
            if not rows
            else (
                "quiet regression appears to be a multi-good scoring/top-k issue; fix gate or label audit and rerun exp5_10 before production"
                if blocker_cleared and by_class.get("multi_good_scoring_issue", 0) == len(rows)
                else "quiet regression includes unresolved model or fixture risk; do not production-promote"
            )
        ),
        "production_blocker_recommendation": (
            "quiet_positional_clean_regression cleared in the current exp5_10 rerun"
            if not rows
            else (
                "replace quiet_positional_clean_regression with quiet_positional_gate_label_audit_required"
                if blocker_cleared and by_class.get("multi_good_scoring_issue", 0) == len(rows)
                else "keep quiet_positional_clean_regression"
            )
        ),
    }


def _write_md(path: Path, payload: dict, rows: list[dict]) -> None:
    lines = [
        "# exp5_11b quiet positional regression audit",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- quiet_clean_regression_count: `{payload['quiet_clean_regression_count']}`",
        f"- classification_counts: `{payload['classification_counts']}`",
        f"- production_implication: `{payload['production_implication']}`",
        f"- production_blocker_recommendation: `{payload['production_blocker_recommendation']}`",
        "",
        "## Rows",
        "",
        "| case | class | subcategory | teacher | baseline | candidate | top3/top5 | static delta vs teacher | reasons |",
        "|---|---|---|---|---|---|---|---:|---|",
    ]
    for row in rows:
        topk = (
            f"baseline={row['baseline_in_teacher_top3']}/{row['baseline_in_teacher_top5']}; "
            f"candidate={row['candidate_in_teacher_top3']}/{row['candidate_in_teacher_top5']}; "
            f"candidate_static_rank={row['candidate_static_rank']}; "
            f"candidate_dense_rank={row['candidate_static_dense_score_rank']}"
        )
        lines.append(
            "| "
            + " | ".join([
                f"`{row['case_id']}`",
                f"`{row['classification']}`",
                f"`{row['subcategory']}`",
                f"`{row['teacher_move']}`",
                f"`{row['baseline_move']}`",
                f"`{row['candidate_move']}`",
                topk,
                f"`{row['static_eval_delta_candidate_vs_teacher']}`",
                ", ".join(row["classification_reasons"]),
            ])
            + " |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    summary_path = Path(args.summary_json).expanduser().resolve()
    cases_path = Path(args.cases_jsonl).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    cases = _case_index(cases_path)
    rows = _audit(summary, cases, multi_good_cp_window=int(args.multi_good_cp_window))
    audit_path = output_dir / "quiet_regression_rows.jsonl"
    summary_out = output_dir / "summary.json"
    md_path = output_dir / "SUMMARY.md"
    _write_jsonl(audit_path, rows)
    payload = _summary(rows, summary_path=summary_path, cases_path=cases_path, output_dir=output_dir)
    payload["audit_jsonl"] = str(audit_path)
    payload["summary_md"] = str(md_path)
    summary_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_md(md_path, payload, rows)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
