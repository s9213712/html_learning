#!/usr/bin/env python3
"""Replay exp4 guarded-overlay unsafe rows against the current runtime guard.

This is a targeted guard-regression check. It does not retrain and does not
mutate production runtime.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from services.games.chess_pv_guarded_overlay import exp4_runtime_overlay_allows_final


ROOT = Path(__file__).resolve().parents[2]
from scripts.games.common_paths import chess_results_root  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_AUDIT_JSON = Path(
    DEFAULT_RESULTS_ROOT
    / "exp4_23_guarded_overlay_broad_sanity_gate_full"
    / "exp4"
    / "audits"
    / "exp4_guarded_overlay_unsafe_override_audit.json"
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def replay_unsafe_rows(audit_json: Path) -> dict[str, Any]:
    audit = _load_json(audit_json)
    replay_rows = []
    for row in audit.get("unsafe_rows") or []:
        allowed, reason, detail = exp4_runtime_overlay_allows_final(
            fen=str(row.get("fen") or ""),
            side=str(row.get("side") or ""),
            baseline_move_uci=str(row.get("baseline_move") or ""),
            final_move_uci=str(row.get("final_move") or ""),
            baseline_score_cp=None,
            final_score_cp=None,
            final_illegal=not bool(row.get("final_move")),
        )
        selected_source = "final" if allowed and row.get("final_move") != row.get("baseline_move") else "baseline"
        selected_move = row.get("final_move") if selected_source == "final" else row.get("baseline_move")
        guarded_correct_after = selected_move == row.get("expected_move")
        still_unsafe = bool(selected_source == "final" and row.get("baseline_correct") and not row.get("final_correct"))
        replay_rows.append(
            {
                "trusted_count": row.get("trusted_count"),
                "split": row.get("split"),
                "case_id": row.get("case_id"),
                "expected_move": row.get("expected_move"),
                "baseline_move": row.get("baseline_move"),
                "final_move": row.get("final_move"),
                "selected_move_after_tightening": selected_move,
                "selected_source_after_tightening": selected_source,
                "guard_allowed_after_tightening": bool(allowed and row.get("final_move") != row.get("baseline_move")),
                "guard_reason_before": row.get("guard_reason"),
                "guard_reason_after": reason,
                "guard_detail_after": detail,
                "baseline_correct": bool(row.get("baseline_correct")),
                "final_correct": bool(row.get("final_correct")),
                "guarded_correct_after": guarded_correct_after,
                "was_unsafe_before": bool(row.get("unsafe_override")),
                "still_unsafe_after": still_unsafe,
                "semantic_class": row.get("semantic_class"),
                "category": row.get("category"),
                "difficulty": row.get("difficulty"),
                "root_causes_before": row.get("root_causes") or [],
            }
        )

    blocked = [row for row in replay_rows if row["was_unsafe_before"] and not row["still_unsafe_after"]]
    still_unsafe = [row for row in replay_rows if row["still_unsafe_after"]]
    positive_preserved = [
        row
        for row in replay_rows
        if row["selected_source_after_tightening"] == "final" and row["final_correct"] and not row["baseline_correct"]
    ]
    selected_baseline = [row for row in replay_rows if row["selected_source_after_tightening"] == "baseline"]
    selected_final = [row for row in replay_rows if row["selected_source_after_tightening"] == "final"]

    return {
        "source": "exp4_25_guarded_overlay_targeted_replay",
        "audit_json": str(audit_json),
        "promotion": False,
        "retrain_attempted": False,
        "runtime_mutated": False,
        "unsafe_rows_total": len(audit.get("unsafe_rows") or []),
        "blocked_after_guard_tightening": len(blocked),
        "still_unsafe": len(still_unsafe),
        "regression_rows_after_guard": len(still_unsafe),
        "positive_override_preserved_count": len(positive_preserved),
        "baseline_selected_count": len(selected_baseline),
        "final_selected_count": len(selected_final),
        "guard_reason_after_counts": dict(sorted(Counter(row["guard_reason_after"] for row in replay_rows).items())),
        "still_unsafe_by_trusted": dict(sorted(Counter(str(row["trusted_count"]) for row in still_unsafe).items())),
        "rows": replay_rows,
        "passed": len(still_unsafe) == 0 and len(replay_rows) == len(audit.get("unsafe_rows") or []),
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# exp4_25：Guarded Overlay Targeted Unsafe Replay（2026-05-12）",
        "",
        "## 結論",
        "",
        f"- promotion: `{report.get('promotion')}`",
        f"- retrain_attempted: `{report.get('retrain_attempted')}`",
        f"- runtime_mutated: `{report.get('runtime_mutated')}`",
        f"- unsafe_rows_total: `{report.get('unsafe_rows_total')}`",
        f"- blocked_after_guard_tightening: `{report.get('blocked_after_guard_tightening')}`",
        f"- still_unsafe: `{report.get('still_unsafe')}`",
        f"- regression_rows_after_guard: `{report.get('regression_rows_after_guard')}`",
        f"- positive_override_preserved_count: `{report.get('positive_override_preserved_count')}`",
        f"- baseline_selected_count: `{report.get('baseline_selected_count')}`",
        f"- final_selected_count: `{report.get('final_selected_count')}`",
        f"- passed: `{report.get('passed')}`",
        "",
        "## Guard Reasons After Tightening",
        "",
        "| reason | count |",
        "| --- | ---: |",
    ]
    for reason, count in (report.get("guard_reason_after_counts") or {}).items():
        lines.append(f"| `{reason}` | {count} |")
    lines.extend(["", "## Rows", "", "| trusted | split | case_id | expected | baseline | final | selected | reason_after | still_unsafe |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
    for row in report.get("rows") or []:
        lines.append(
            "| "
            + " | ".join(
                str(x)
                for x in [
                    row.get("trusted_count"),
                    row.get("split"),
                    row.get("case_id"),
                    row.get("expected_move"),
                    row.get("baseline_move"),
                    row.get("final_move"),
                    row.get("selected_move_after_tightening"),
                    row.get("guard_reason_after"),
                    row.get("still_unsafe_after"),
                ]
            )
            + " |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-json", type=Path, default=DEFAULT_AUDIT_JSON)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--output-md", type=Path, default=None)
    args = parser.parse_args(argv)
    report = replay_unsafe_rows(args.audit_json)
    output_json = args.output_json or args.audit_json.with_name("exp4_guarded_overlay_targeted_replay_after_tightening.json")
    output_md = args.output_md or args.audit_json.with_name("exp4_guarded_overlay_targeted_replay_after_tightening.md")
    _write_json(output_json, report)
    write_markdown(output_md, report)
    print(
        json.dumps(
            {
                "ok": True,
                "output_json": str(output_json),
                "output_md": str(output_md),
                "unsafe_rows_total": report["unsafe_rows_total"],
                "blocked_after_guard_tightening": report["blocked_after_guard_tightening"],
                "still_unsafe": report["still_unsafe"],
                "passed": report["passed"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
