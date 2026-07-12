#!/usr/bin/env python3
"""Audit exp4 guarded-overlay unsafe overrides from existing validation artifacts.

This script does not retrain and does not call production runtime mutation.
It reconstructs guarded overlay choices from saved before/after sanity rows.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import chess
except Exception:  # pragma: no cover - tests run with python-chess installed.
    chess = None

from services.games.chess_pv_guarded_overlay import exp4_runtime_overlay_allows_final


ROOT = Path(__file__).resolve().parents[2]
from scripts.games.common_paths import chess_results_root  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_RESULT_DIR = Path(
    DEFAULT_RESULTS_ROOT
    / "exp4_23_guarded_overlay_broad_sanity_gate_full"
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _move_properties(fen: str, move_uci: str) -> dict[str, Any]:
    props = {
        "legal": False,
        "is_castling": False,
        "is_en_passant": False,
        "is_capture": False,
        "is_promotion": False,
        "promotion_piece": None,
        "special_rule_family": "ordinary",
    }
    if not chess or not fen or not move_uci:
        return props
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(move_uci)
    except Exception:
        return props
    props["legal"] = move in board.legal_moves
    if not props["legal"]:
        return props
    props["is_castling"] = board.is_castling(move)
    props["is_en_passant"] = board.is_en_passant(move)
    props["is_capture"] = board.is_capture(move)
    props["is_promotion"] = move.promotion is not None
    props["promotion_piece"] = chess.piece_symbol(move.promotion) if move.promotion else None
    if props["is_castling"]:
        props["special_rule_family"] = "castling"
    elif props["is_en_passant"]:
        props["special_rule_family"] = "en_passant"
    elif props["is_promotion"]:
        props["special_rule_family"] = "promotion"
    elif props["is_capture"]:
        props["special_rule_family"] = "capture"
    return props


def _case_semantic(case: dict[str, Any]) -> str:
    return str(case.get("semantic_class") or case.get("expected_semantic") or "")


def _case_difficulty(case: dict[str, Any]) -> str:
    return str(case.get("difficulty") or case.get("variant_difficulty") or "")


def _case_category(case: dict[str, Any]) -> str:
    case_id = str(case.get("case_id") or "")
    semantic = _case_semantic(case)
    if "promotion" in case_id:
        return "special_rule"
    if "castle" in case_id or "castling" in case_id:
        return "special_rule"
    if "en_passant" in case_id:
        return "special_rule"
    if semantic:
        return semantic
    if case_id.startswith("gate_"):
        return "clean_gate"
    if case_id.startswith("sanity:"):
        return "sanity_variant"
    return ""


def _cluster_root_causes(row: dict[str, Any]) -> list[str]:
    causes: list[str] = []
    detail = row.get("guard_detail") or {}
    score_delta = detail.get("score_delta")
    baseline_score = detail.get("baseline_score_cp")
    final_score = detail.get("final_score_cp")
    final_props = row.get("final_move_properties") or {}
    semantic = str(row.get("semantic_class") or "")
    category = str(row.get("category") or "")
    expected_in_top3 = bool(row.get("expected_in_top3"))

    if baseline_score is None or final_score is None or score_delta is None:
        causes.append("missing_or_weak_score_margin")
    else:
        try:
            if abs(float(score_delta)) <= 25:
                causes.append("missing_or_weak_score_margin")
        except (TypeError, ValueError):
            causes.append("missing_or_weak_score_margin")

    if row.get("baseline_correct") and not row.get("final_correct"):
        causes.append("baseline_already_correct_but_overlay_replaced_it")

    if final_props.get("special_rule_family") in {"castling", "promotion", "en_passant"}:
        causes.append("special_rule_guard_gap")
    elif final_props.get("special_rule_family") == "capture":
        causes.append("promotion_en_passant_capture_priority_interaction")
    elif category in {"opening", "clean_gate", "sanity_variant"} or "central_break" in semantic:
        causes.append("broad_ordinary_opening_override")

    if expected_in_top3 and not row.get("final_correct"):
        causes.append("multi_good_opening_label_false_negative_or_topk_conflict")

    if "validation" in str(row.get("case_id") or "") and row.get("split") == "unseen":
        causes.append("final_move_correct_only_on_exact_training_fen_but_unsafe_on_variant")

    if row.get("simulator_runtime_mismatch_risk"):
        causes.append("simulator_runtime_decision_mismatch_risk")

    return sorted(set(causes)) or ["unclassified_guard_gap"]


def _join_by_case_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("case_id") or ""): row for row in rows if row.get("case_id")}


def _iter_checkpoint_guarded_rows(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    trusted = checkpoint.get("trusted_count") or checkpoint.get("trusted_replays")
    probe = checkpoint.get("sanity_learning_probe") or {}
    rows: list[dict[str, Any]] = []
    for split, split_data in (
        ("seen", (probe.get("seen_variants") or {})),
        ("unseen", (probe.get("unseen_variants") or {})),
    ):
        before_by_id = _join_by_case_id(split_data.get("before") or [])
        after_by_id = _join_by_case_id(split_data.get("after") or [])
        case_by_id = _join_by_case_id(split_data.get("cases") or [])
        raw_by_id = _join_by_case_id(split_data.get("raw_policy_after") or [])
        for case_id, before in before_by_id.items():
            after = after_by_id.get(case_id) or {}
            case = case_by_id.get(case_id) or {}
            raw = raw_by_id.get(case_id) or {}
            baseline_move = str(before.get("top1") or "")
            final_move = str(after.get("top1") or "")
            fen = str(before.get("fen") or after.get("fen") or case.get("fen") or "")
            side = str(before.get("side") or after.get("side") or case.get("side") or "")
            allowed, guard_reason, guard_detail = exp4_runtime_overlay_allows_final(
                fen=fen,
                side=side,
                baseline_move_uci=baseline_move,
                final_move_uci=final_move,
                baseline_score_cp=None,
                final_score_cp=None,
                final_illegal=not bool(final_move),
            )
            selected_source = "final" if allowed and final_move != baseline_move else "baseline"
            selected_move = final_move if selected_source == "final" else baseline_move
            expected = str(before.get("expected_move") or after.get("expected_move") or case.get("expected_move") or "")
            baseline_correct = bool(before.get("expected_is_top1"))
            final_correct = bool(after.get("expected_is_top1"))
            guarded_correct = selected_move == expected
            row = {
                "trusted_count": trusted,
                "split": split,
                "case_id": case_id,
                "fen": fen,
                "side": side,
                "expected_move": expected,
                "expected_uci_any": [expected] if expected else [],
                "baseline_move": baseline_move,
                "final_move": final_move,
                "guarded_selected_move": selected_move,
                "selected_source": selected_source,
                "baseline_correct": baseline_correct,
                "final_correct": final_correct,
                "guarded_correct": guarded_correct,
                "expected_in_top3": bool(before.get("expected_in_top3") or after.get("expected_in_top3")),
                "guard_reason": guard_reason,
                "guard_detail": guard_detail,
                "semantic_class": _case_semantic(case),
                "category": _case_category(case),
                "subcategory": str(case.get("flank_reason_tag") or case.get("source") or ""),
                "difficulty": _case_difficulty(case),
                "static_score": guard_detail.get("final_score_cp"),
                "baseline_static_score": guard_detail.get("baseline_score_cp"),
                "static_margin": guard_detail.get("score_delta"),
                "raw_policy_after": {
                    "top1": raw.get("raw_policy_top1"),
                    "top3": raw.get("raw_policy_top3"),
                    "expected_rank": raw.get("expected_rank"),
                    "expected_probability": raw.get("expected_probability"),
                    "expected_logit": raw.get("expected_logit"),
                },
                "final_move_properties": _move_properties(fen, final_move),
                "baseline_move_properties": _move_properties(fen, baseline_move),
                "variant_split": str(case.get("variant_split") or split),
                "normalized_fen_hash": case.get("normalized_fen_hash"),
                "board_semantics_features": case.get("board_semantics_features") or {},
                "flank_context_features": case.get("flank_context_features") or {},
            }
            row["unsafe_override"] = bool(baseline_correct and not final_correct)
            row["selected_unsafe_override"] = bool(selected_source == "final" and baseline_correct and not final_correct)
            row["regression_row"] = bool(baseline_correct and not final_correct)
            row["root_causes"] = _cluster_root_causes(row) if row["unsafe_override"] or row["regression_row"] else []
            rows.append(row)
    return rows


def build_audit(result_dir: Path) -> dict[str, Any]:
    engine_dir = result_dir / "exp4"
    summary_path = engine_dir / "summary.json"
    summary = _load_json(summary_path)
    checkpoints = []
    all_rows: list[dict[str, Any]] = []
    for checkpoint_path in sorted((engine_dir / "checkpoints").glob("*/before_after_eval.json")):
        checkpoint = _load_json(checkpoint_path)
        rows = _iter_checkpoint_guarded_rows(checkpoint)
        trusted = checkpoint.get("trusted_count") or checkpoint.get("trusted_replays") or checkpoint_path.parent.name
        unsafe = [row for row in rows if row.get("unsafe_override")]
        regressions = [row for row in rows if row.get("regression_row")]
        split_counts = Counter(row["split"] for row in unsafe)
        checkpoints.append(
            {
                "trusted_count": trusted,
                "rows_checked": len(rows),
                "unsafe_override_count": len(unsafe),
                "regression_row_count": len(regressions),
                "unsafe_by_split": dict(sorted(split_counts.items())),
                "regression_by_split": dict(sorted(Counter(row["split"] for row in regressions).items())),
            }
        )
        all_rows.extend(rows)

    unsafe_rows = [row for row in all_rows if row.get("unsafe_override")]
    regression_rows = [row for row in all_rows if row.get("regression_row")]
    root_counter: Counter[str] = Counter()
    guard_counter: Counter[str] = Counter()
    semantic_counter: Counter[str] = Counter()
    for row in unsafe_rows:
        guard_counter[str(row.get("guard_reason") or "")] += 1
        semantic_counter[str(row.get("semantic_class") or row.get("category") or "")] += 1
        for cause in row.get("root_causes") or []:
            root_counter[cause] += 1

    gate = (summary.get("promotion_gate") or {}).get("guarded_overlay_broad_sanity_gate") or {}
    return {
        "source": "exp4_24_guarded_overlay_unsafe_override_audit",
        "result_dir": str(result_dir),
        "summary_path": str(summary_path),
        "promotion": False,
        "retrain_attempted": False,
        "runtime_mutated": False,
        "guarded_overlay_broad_sanity_gate": gate,
        "actual_runtime_guarded_overlay": summary.get("exp4_actual_runtime_guarded_overlay") or {},
        "checkpoint_summary": checkpoints,
        "unsafe_override_count": len(unsafe_rows),
        "regression_row_count": len(regression_rows),
        "unsafe_by_trusted": dict(sorted(Counter(str(row["trusted_count"]) for row in unsafe_rows).items())),
        "unsafe_by_split": dict(sorted(Counter(row["split"] for row in unsafe_rows).items())),
        "unsafe_by_guard_reason": dict(sorted(guard_counter.items())),
        "unsafe_by_semantic": dict(sorted(semantic_counter.items())),
        "root_cause_counts": dict(sorted(root_counter.items())),
        "unsafe_rows": unsafe_rows,
        "regression_rows": regression_rows,
        "recommendations": [
            "No-score or zero-margin ordinary rows must not pass guarded overlay unless a special-rule oracle strongly supports the move.",
            "Require stricter static/top-k margin for ordinary non-special moves.",
            "Add a second-check or blacklist for guard reasons that produced unsafe overrides, especially runtime_static_and_rule_guard_passed on broad variants.",
            "Avoid overriding baseline when baseline is already correct/safe in broad sanity variants.",
            "Preserve deterministic +0.0538 only after broad unsafe_override_count reaches 0.",
        ],
    }


def _md_table(rows: list[list[Any]]) -> list[str]:
    if not rows:
        return []
    header = rows[0]
    out = ["| " + " | ".join(str(x) for x in header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows[1:]:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return out


def write_markdown(path: Path, audit: dict[str, Any]) -> None:
    gate = audit.get("guarded_overlay_broad_sanity_gate") or {}
    actual = audit.get("actual_runtime_guarded_overlay") or {}
    lines = [
        "# exp4_24：Guarded Overlay Unsafe Override Audit（2026-05-12）",
        "",
        "## 結論",
        "",
        "promotion=false；retrain_attempted=false；runtime_mutated=false。",
        "",
        "本輪只讀 exp4_23 full diagnostic artifact，沒有重新訓練，也沒有改 production runtime。",
        "",
        "## 輸入",
        "",
        f"- result_dir: `{audit.get('result_dir')}`",
        f"- summary: `{audit.get('summary_path')}`",
        "",
        "## exp4_23 Guarded Overlay 狀態",
        "",
        f"- baseline score: `{actual.get('baseline_score')}`",
        f"- final replacement score: `{actual.get('final_score')}`",
        f"- actual runtime guarded score: `{actual.get('actual_runtime_guarded_score')}`",
        f"- delta vs baseline: `{actual.get('delta_vs_baseline')}`",
        f"- deterministic unsafe override: `{actual.get('unsafe_override_count')}`",
        f"- simulator mismatch: `{actual.get('simulator_selected_mismatch_count')}`",
        f"- guarded_overlay_broad_sanity_gate.passed: `{gate.get('passed')}`",
        f"- gate reasons: `{gate.get('reasons')}`",
        "",
        "## Unsafe Override Summary",
        "",
    ]
    lines.extend(
        _md_table(
            [
                ["checkpoint", "rows", "unsafe", "regression_rows", "unsafe_by_split"],
                *[
                    [
                        row.get("trusted_count"),
                        row.get("rows_checked"),
                        row.get("unsafe_override_count"),
                        row.get("regression_row_count"),
                        row.get("unsafe_by_split"),
                    ]
                    for row in audit.get("checkpoint_summary") or []
                ],
            ]
        )
    )
    lines.extend(
        [
            "",
            f"- total unsafe_override_count: `{audit.get('unsafe_override_count')}`",
            f"- total regression_row_count: `{audit.get('regression_row_count')}`",
            f"- unsafe_by_split: `{audit.get('unsafe_by_split')}`",
            f"- unsafe_by_guard_reason: `{audit.get('unsafe_by_guard_reason')}`",
            f"- unsafe_by_semantic: `{audit.get('unsafe_by_semantic')}`",
            "",
            "## Root Cause Clusters",
            "",
        ]
    )
    lines.extend(_md_table([["root_cause", "count"], *audit.get("root_cause_counts", {}).items()]))
    lines.extend(["", "## Unsafe Rows（摘要）", ""])
    unsafe_rows = audit.get("unsafe_rows") or []
    lines.extend(
        _md_table(
            [
                [
                    "trusted",
                    "split",
                    "case_id",
                    "expected",
                    "baseline",
                    "final",
                    "guard",
                    "semantic",
                    "difficulty",
                    "root_causes",
                ],
                *[
                    [
                        row.get("trusted_count"),
                        row.get("split"),
                        row.get("case_id"),
                        row.get("expected_move"),
                        row.get("baseline_move"),
                        row.get("final_move"),
                        row.get("guard_reason"),
                        row.get("semantic_class") or row.get("category"),
                        row.get("difficulty"),
                        ", ".join(row.get("root_causes") or []),
                    ]
                    for row in unsafe_rows[:40]
                ],
            ]
        )
    )
    if len(unsafe_rows) > 40:
        lines.append(f"\n只列前 40 筆；完整 rows 見 JSON。總 unsafe rows: `{len(unsafe_rows)}`。")
    lines.extend(["", "## 建議", ""])
    lines.extend([f"- {item}" for item in audit.get("recommendations") or []])
    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "exp4_25 應收緊 guarded runtime guard，先用這批 unsafe rows 做 targeted replay，unsafe_override_count 必須降到 0 才值得重跑 full broad sanity。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", type=Path, default=DEFAULT_RESULT_DIR)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--output-md", type=Path, default=None)
    args = parser.parse_args(argv)
    audit = build_audit(args.result_dir)
    output_json = args.output_json or (args.result_dir / "exp4" / "audits" / "exp4_guarded_overlay_unsafe_override_audit.json")
    output_md = args.output_md or (args.result_dir / "exp4" / "audits" / "exp4_guarded_overlay_unsafe_override_audit.md")
    _write_json(output_json, audit)
    write_markdown(output_md, audit)
    print(
        json.dumps(
            {
                "ok": True,
                "output_json": str(output_json),
                "output_md": str(output_md),
                "unsafe_override_count": audit["unsafe_override_count"],
                "regression_row_count": audit["regression_row_count"],
                "promotion": audit["promotion"],
                "retrain_attempted": audit["retrain_attempted"],
                "runtime_mutated": audit["runtime_mutated"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
