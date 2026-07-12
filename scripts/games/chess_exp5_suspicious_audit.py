#!/usr/bin/env python3
"""Audit exp5 suspicious benchmark rows without training or promotion.

The script reads an exp5 production-readiness summary plus its case JSONL and
classifies each suspicious row as a likely model risk, fixture/audit issue, or
label-quality issue.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import chess


ROOT = Path(__file__).resolve().parents[2]
from scripts.games.common_paths import chess_results_root  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_SUMMARY = DEFAULT_RESULTS_ROOT / "exp5_10_production_readiness" / "summary.json"
DEFAULT_CASES = DEFAULT_RESULTS_ROOT / "exp5_10_production_readiness" / "exp5_10_benchmark_cases.jsonl"
DEFAULT_OUTPUT_DIR = DEFAULT_RESULTS_ROOT / "exp5_11a_suspicious_audit"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit exp5 suspicious rows from a production-readiness summary.")
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY))
    parser.add_argument("--cases-jsonl", default=str(DEFAULT_CASES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser.parse_args()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _status_names(status: int) -> list[str]:
    names = []
    for name, value in chess.__dict__.items():
        if name.startswith("STATUS_") and isinstance(value, int) and status & value:
            names.append(name)
    return sorted(names) or ["STATUS_VALID"]


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


def _move_audit(fen: str, move_uci: str) -> dict:
    board = chess.Board(fen)
    status = int(board.status())
    move_legal = False
    after_status = 0
    after_stalemate = False
    after_checkmate = False
    after_repetition = False
    after_fen = ""
    if move_uci:
        try:
            move = chess.Move.from_uci(move_uci)
            move_legal = move in board.legal_moves
            after = board.copy(stack=False)
            after.push(move)
            after_status = int(after.status())
            after_stalemate = after.is_stalemate()
            after_checkmate = after.is_checkmate()
            after_repetition = after.is_repetition(3)
            after_fen = after.fen()
        except Exception:
            move_legal = False
    return {
        "initial_board_valid": board.is_valid(),
        "initial_status": status,
        "initial_status_names": _status_names(status),
        "side_to_move_in_check": board.is_check(),
        "candidate_legal": move_legal,
        "after_status": after_status,
        "after_status_names": _status_names(after_status),
        "after_stalemate": after_stalemate,
        "after_checkmate": after_checkmate,
        "after_repetition": after_repetition,
        "after_fen": after_fen,
    }


def _classify(row: dict, case: dict, move_audit: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not move_audit["initial_board_valid"]:
        reasons.append("fixture_invalid_initial_position")
    if "STATUS_OPPOSITE_CHECK" in move_audit["initial_status_names"]:
        reasons.append("opposite_side_already_in_check")
    if "STATUS_NO_WHITE_KING" in move_audit["after_status_names"] or "STATUS_NO_BLACK_KING" in move_audit["after_status_names"]:
        reasons.append("candidate_move_captures_king_due_invalid_fixture")
    if not move_audit["candidate_legal"]:
        reasons.append("candidate_illegal_move")
    if move_audit["after_stalemate"]:
        reasons.append("after_move_stalemate")
    if str(row.get("label_quality") or "") in {"questionable", "review"}:
        reasons.append(f"label_quality_{row.get('label_quality')}")

    if "candidate_illegal_move" in reasons and "fixture_invalid_initial_position" not in reasons:
        return "true_model_risk", reasons
    if "fixture_invalid_initial_position" in reasons:
        return "fixture_or_audit_false_positive", reasons
    if str(row.get("label_quality") or "") in {"questionable", "review"}:
        return "label_quality_issue", reasons
    if move_audit["after_stalemate"]:
        return "true_model_risk", reasons
    return "needs_manual_review", reasons


def _audit_rows(summary: dict, cases: dict[str, dict]) -> list[dict]:
    rows = []
    for row in summary.get("benchmark", {}).get("rows", []):
        if not row.get("candidate_suspicious"):
            continue
        case = cases.get(str(row.get("id") or ""), {})
        teacher_top3 = [str(item).lower() for item in (case.get("teacher_top3") or [])]
        teacher_top5 = [str(item).lower() for item in (case.get("teacher_top5") or [])]
        move_audit = _move_audit(str(row.get("fen") or ""), str(row.get("candidate_move") or ""))
        classification, classification_reasons = _classify(row, case, move_audit)
        rows.append({
            "case_id": str(row.get("id") or ""),
            "fen": str(row.get("fen") or ""),
            "category": str(row.get("category") or ""),
            "subcategory": str(row.get("subcategory") or ""),
            "label_quality": str(row.get("label_quality") or ""),
            "baseline_move": str(row.get("baseline_move") or ""),
            "candidate_move": str(row.get("candidate_move") or ""),
            "candidate_reasons": list(row.get("candidate_reasons") or []),
            "suspicious_reason": "candidate_stalemate_after_move" if move_audit["after_stalemate"] else "candidate_suspicious",
            "candidate_legal": bool(move_audit["candidate_legal"]),
            "after_stalemate": bool(move_audit["after_stalemate"]),
            "after_repetition": bool(move_audit["after_repetition"]),
            "teacher_move": str(row.get("teacher_move") or case.get("teacher_move") or ""),
            "teacher_top3": teacher_top3,
            "teacher_top5": teacher_top5,
            "candidate_in_teacher_top3": str(row.get("candidate_move") or "").lower() in teacher_top3,
            "candidate_in_teacher_top5": str(row.get("candidate_move") or "").lower() in teacher_top5,
            "initial_board_valid": bool(move_audit["initial_board_valid"]),
            "initial_status_names": move_audit["initial_status_names"],
            "side_to_move_in_check": bool(move_audit["side_to_move_in_check"]),
            "after_status_names": move_audit["after_status_names"],
            "after_checkmate": bool(move_audit["after_checkmate"]),
            "classification": classification,
            "classification_reasons": classification_reasons,
            "true_model_risk": classification == "true_model_risk",
            "fixture_or_audit_false_positive": classification == "fixture_or_audit_false_positive",
            "label_quality_issue": classification == "label_quality_issue",
        })
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _summary(rows: list[dict], *, summary_path: Path, cases_path: Path, output_dir: Path) -> dict:
    by_class: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for row in rows:
        by_class[row["classification"]] = by_class.get(row["classification"], 0) + 1
        for reason in row["classification_reasons"]:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "ok": True,
        "generated_at": _now(),
        "source_summary_json": str(summary_path),
        "source_cases_jsonl": str(cases_path),
        "output_dir": str(output_dir),
        "suspicious_row_count": len(rows),
        "true_model_risk_count": by_class.get("true_model_risk", 0),
        "fixture_or_audit_false_positive_count": by_class.get("fixture_or_audit_false_positive", 0),
        "label_quality_issue_count": by_class.get("label_quality_issue", 0),
        "needs_manual_review_count": by_class.get("needs_manual_review", 0),
        "classification_counts": by_class,
        "reason_counts": by_reason,
        "production_implication": (
            "no suspicious rows remain in the source exp5_10 summary; suspicious_rate_nonzero is not a current blocker"
            if not rows
            else (
                "suspicious_rate_nonzero appears fixture-driven in this audit; production remains blocked by other exp5_10 reasons unless revalidated with fixed fixtures"
                if by_class.get("fixture_or_audit_false_positive", 0) == len(rows)
                else "suspicious rows include unresolved or true model risk"
            )
        ),
    }


def _write_md(path: Path, payload: dict, rows: list[dict]) -> None:
    lines = [
        "# exp5_11a suspicious-rate audit",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- suspicious_row_count: `{payload['suspicious_row_count']}`",
        f"- true_model_risk_count: `{payload['true_model_risk_count']}`",
        f"- fixture_or_audit_false_positive_count: `{payload['fixture_or_audit_false_positive_count']}`",
        f"- label_quality_issue_count: `{payload['label_quality_issue_count']}`",
        f"- production_implication: `{payload['production_implication']}`",
        "",
        "## Rows",
        "",
        "| case | class | category | label | baseline | candidate | initial status | after status | reasons |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join([
                f"`{row['case_id']}`",
                f"`{row['classification']}`",
                f"`{row['category']}/{row['subcategory']}`",
                f"`{row['label_quality']}`",
                f"`{row['baseline_move']}`",
                f"`{row['candidate_move']}`",
                ", ".join(row["initial_status_names"]),
                ", ".join(row["after_status_names"]),
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
    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    cases = _case_index(cases_path)
    rows = _audit_rows(summary_payload, cases)
    audit_path = output_dir / "suspicious_rows.jsonl"
    summary_path_out = output_dir / "summary.json"
    md_path = output_dir / "SUMMARY.md"
    _write_jsonl(audit_path, rows)
    payload = _summary(rows, summary_path=summary_path, cases_path=cases_path, output_dir=output_dir)
    payload["audit_jsonl"] = str(audit_path)
    payload["summary_md"] = str(md_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_md(md_path, payload, rows)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
