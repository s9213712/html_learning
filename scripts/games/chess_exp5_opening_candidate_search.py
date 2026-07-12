#!/usr/bin/env python3
"""exp5_15 opening curriculum staging candidate search.

This script trains isolated exp5 candidate artifacts from the clean opening
pool built in exp5_14b. It never stages, promotes, or mutates the runtime model.
Candidates are compared against the current production artifact and screened for
opening improvement plus deterministic retention regressions.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

import chess


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.games.chess_exp5_production_readiness import _evaluate, _normalize_case  # noqa: E402
from services.games.chess_nnue import (  # noqa: E402
    choose_experiment_nnue_move,
    rank_experiment_nnue_policy_moves,
    train_experiment_nnue_from_replay_samples,
)


from scripts.games.common_paths import chess_results_root, runtime_model_path  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_OPENING_CURRICULUM = DEFAULT_RESULTS_ROOT / "exp5_14b_clean_opening_heldout" / "clean_opening_curriculum.jsonl"
DEFAULT_RETENTION_TRAIN = DEFAULT_RESULTS_ROOT / "exp5_08_clean_pool" / "inputs" / "exp5_08_train_clean_only.jsonl"
DEFAULT_EXP5_13_SUMMARY = DEFAULT_RESULTS_ROOT / "exp5_13_rule_smoke_stalemate_fix_check" / "summary.json"
DEFAULT_RUNTIME_PRODUCTION = runtime_model_path("chess_experiment_5_nnue_experience.json")
DEFAULT_PROMOTED_STAGE = DEFAULT_RESULTS_ROOT / "exp5_08_stage_candidate" / "chess_experiment_5_nnue_stage_candidate.json"
DEFAULT_OUTPUT_DIR = DEFAULT_RESULTS_ROOT / "exp5_15_opening_candidate_search"
DEFAULT_SEARCH_PROFILE = "fixed_depth_strong"
CURRENT_PRODUCTION_SHA = "c47ef752aa69d7b8c813b587468228593f44d69c9b947313325e03797e4450dc"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run exp5_15 opening candidate search without touching runtime.")
    parser.add_argument("--opening-curriculum-jsonl", default=str(DEFAULT_OPENING_CURRICULUM))
    parser.add_argument("--retention-train-jsonl", default=str(DEFAULT_RETENTION_TRAIN))
    parser.add_argument("--exp5-13-summary", default=str(DEFAULT_EXP5_13_SUMMARY))
    parser.add_argument("--production-model-path", default=str(DEFAULT_RUNTIME_PRODUCTION))
    parser.add_argument("--fallback-production-model-path", default=str(DEFAULT_PROMOTED_STAGE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--search-profile", default=DEFAULT_SEARCH_PROFILE)
    parser.add_argument("--max-retention-rows", type=int, default=80)
    parser.add_argument("--auto-hard-negative-topk", type=int, default=4)
    return parser.parse_args()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_rows(rows: list[dict]) -> str:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_base_candidate_if_needed(base_model: Path, candidate_model: Path) -> None:
    if base_model.exists():
        shutil.copyfile(base_model, candidate_model)


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
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [dict(row) for row in (((payload.get("benchmark") or {}).get("rows")) or []) if isinstance(row, dict)]


def resolve_current_production_model(runtime_path: Path, fallback_path: Path) -> tuple[Path, str]:
    if runtime_path.exists():
        return runtime_path, "runtime_production"
    if fallback_path.exists():
        return fallback_path, "promoted_stage_candidate_fallback"
    return runtime_path, "missing"


def _move_uci(move: dict | None) -> str:
    if not move:
        return ""
    return f"{move.get('from', '')}{move.get('to', '')}{move.get('promotion') or ''}".lower()


def opening_rows_to_samples(rows: list[dict], *, weight: float = 2.0, source: str = "exp5_15_opening") -> list[dict]:
    samples: list[dict] = []
    for row in rows:
        fen = str(row.get("fen") or "").strip()
        side = str(row.get("side") or "").strip().lower()
        expected = [str(item).strip().lower() for item in (row.get("expected_uci_any") or []) if str(item).strip()]
        if not fen or side not in {"white", "black"} or not expected:
            continue
        for move_uci in expected:
            samples.append({
                "fen": fen,
                "side": side,
                "move_uci": move_uci,
                "target": 1.0,
                "weight": weight / max(1, len(expected)),
                "source": source,
                "label_quality": "clean",
                "teacher_top3": expected[:3],
                "teacher_top5": expected[:5],
                "search_profile": "fast",
                "source_case_id": str(row.get("id") or ""),
                "category": "opening",
            })
    return samples


def retention_rows_to_samples(rows: list[dict], *, max_rows: int, weight: float = 0.6) -> list[dict]:
    samples: list[dict] = []
    for row in rows:
        if str(row.get("label_quality") or "").lower() != "clean":
            continue
        fen = str(row.get("fen") or "").strip()
        move_uci = str(row.get("move_uci") or "").strip().lower()
        side = str(row.get("side") or "").strip().lower()
        if not fen or not move_uci or side not in {"white", "black"}:
            continue
        try:
            board = chess.Board(fen)
        except Exception:
            continue
        non_king = sum(1 for piece in board.piece_map().values() if piece.piece_type != chess.KING)
        if non_king > 6:
            continue
        samples.append({
            "fen": fen,
            "side": side,
            "move_uci": move_uci,
            "target": 1.0,
            "weight": weight,
            "source": "exp5_15_endgame_retention",
            "label_quality": "clean",
            "teacher_top3": list(row.get("teacher_top3") or []),
            "teacher_top5": list(row.get("teacher_top5") or []),
            "search_profile": str(row.get("search_profile") or "fast"),
            "source_case_id": str(row.get("position_id") or ""),
            "category": "retention_endgame",
        })
        if max_rows > 0 and len(samples) >= max_rows:
            break
    return samples


def inject_hard_negatives(samples: list[dict], *, source_model: Path, topk: int) -> tuple[list[dict], dict]:
    injected_rows: list[dict] = []
    summary = {
        "topk": topk,
        "samples_total": len(samples),
        "samples_with_hard_negatives": 0,
        "hard_negative_count": 0,
        "excluded_expected_count": 0,
        "source_model_path": str(source_model),
    }
    if topk <= 0 or not source_model.exists():
        return [dict(sample) for sample in samples], summary
    for sample in samples:
        item = dict(sample)
        expected = {
            str(move).strip().lower()
            for move in (item.get("teacher_top5") or item.get("teacher_top3") or [item.get("move_uci")])
            if str(move).strip()
        }
        expected.add(str(item.get("move_uci") or "").strip().lower())
        hard_negatives: list[str] = []
        rows = rank_experiment_nnue_policy_moves(
            {"__fen__": str(item.get("fen") or "")},
            str(item.get("side") or ""),
            model_path=source_model,
            search_profile=str(item.get("search_profile") or "fast"),
        )
        for ranked in rows:
            move = str(ranked.get("move") or "").strip().lower()
            if not move:
                continue
            if move in expected:
                summary["excluded_expected_count"] += 1
                continue
            hard_negatives.append(move)
            if len(hard_negatives) >= topk:
                break
        item["hard_negatives"] = hard_negatives
        if hard_negatives:
            summary["samples_with_hard_negatives"] += 1
            summary["hard_negative_count"] += len(hard_negatives)
        injected_rows.append(item)
    return injected_rows, summary


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def evaluate_opening_pool(rows: list[dict], *, current_model: Path, candidate_model: Path, search_profile: str) -> dict:
    detail: list[dict] = []
    for row in rows:
        expected = {str(item).strip().lower() for item in (row.get("expected_uci_any") or []) if str(item).strip()}
        fen = str(row.get("fen") or "")
        side = str(row.get("side") or "")
        current = _move_uci(choose_experiment_nnue_move({"__fen__": fen}, side, model_path=current_model, search_profile=search_profile))
        candidate = _move_uci(choose_experiment_nnue_move({"__fen__": fen}, side, model_path=candidate_model, search_profile=search_profile))
        current_pass = current in expected
        candidate_pass = candidate in expected
        detail.append({
            "id": str(row.get("id") or ""),
            "fen": fen,
            "side": side,
            "expected_uci_any": sorted(expected),
            "current_move": current,
            "candidate_move": candidate,
            "current_pass": current_pass,
            "candidate_pass": candidate_pass,
            "candidate_improved": candidate_pass and not current_pass,
            "candidate_regressed": current_pass and not candidate_pass,
        })
    total = max(1, len(detail))
    current_passed = sum(1 for row in detail if row["current_pass"])
    candidate_passed = sum(1 for row in detail if row["candidate_pass"])
    return {
        "rows": detail,
        "overall": {
            "cases": len(detail),
            "current_passed": current_passed,
            "candidate_passed": candidate_passed,
            "current_score": round(current_passed / total, 6),
            "candidate_score": round(candidate_passed / total, 6),
            "score_delta": round((candidate_passed - current_passed) / total, 6),
            "improved_count": sum(1 for row in detail if row["candidate_improved"]),
            "regressed_count": sum(1 for row in detail if row["candidate_regressed"]),
        },
    }


def evaluate_retention(rows: list[dict], *, candidate_model: Path, search_profile: str) -> dict:
    detail: list[dict] = []
    for raw in rows:
        case = _normalize_case(raw)
        candidate = _evaluate(candidate_model, case, search_profile=search_profile)
        current_pass = bool(raw.get("candidate_pass"))
        candidate_pass = bool(candidate.get("pass"))
        detail.append({
            "id": case["id"],
            "category": case["category"],
            "subcategory": case["subcategory"],
            "label_quality": case["label_quality"],
            "fen": case["fen"],
            "side": case["side"],
            "expected_uci_any": list(case.get("expected_uci_any") or []),
            "current_move": str(raw.get("candidate_move") or ""),
            "current_pass": current_pass,
            "candidate_move": str(candidate.get("chosen_move") or ""),
            "candidate_pass": candidate_pass,
            "candidate_reasons": list(candidate.get("reasons") or []),
            "candidate_improved": candidate_pass and not current_pass,
            "candidate_regressed": current_pass and not candidate_pass,
            "candidate_illegal": not bool(candidate.get("legal")),
            "candidate_suspicious": bool(candidate.get("is_stalemate")) or not bool(candidate.get("legal")),
        })
    clusters: dict[str, list[dict]] = {}
    for row in detail:
        clusters.setdefault(row["category"], []).append(row)
    total = max(1, len(detail))
    passed = sum(1 for row in detail if row["candidate_pass"])
    current_passed = sum(1 for row in detail if row["current_pass"])
    cluster_summary = {}
    for name, group in sorted(clusters.items()):
        denom = max(1, len(group))
        cluster_summary[name] = {
            "cases": len(group),
            "current_passed": sum(1 for row in group if row["current_pass"]),
            "candidate_passed": sum(1 for row in group if row["candidate_pass"]),
            "current_score": round(sum(1 for row in group if row["current_pass"]) / denom, 6),
            "candidate_score": round(sum(1 for row in group if row["candidate_pass"]) / denom, 6),
            "score_delta": round((sum(1 for row in group if row["candidate_pass"]) - sum(1 for row in group if row["current_pass"])) / denom, 6),
            "clean_regressed_count": sum(1 for row in group if row["candidate_regressed"] and row["label_quality"] == "clean"),
        }
    return {
        "rows": detail,
        "overall": {
            "cases": len(detail),
            "current_passed": current_passed,
            "candidate_passed": passed,
            "current_score": round(current_passed / total, 6),
            "candidate_score": round(passed / total, 6),
            "score_delta": round((passed - current_passed) / total, 6),
            "clean_regressed_count": sum(1 for row in detail if row["candidate_regressed"] and row["label_quality"] == "clean"),
            "illegal_rate": round(sum(1 for row in detail if row["candidate_illegal"]) / total, 6),
            "suspicious_rate": round(sum(1 for row in detail if row["candidate_suspicious"]) / total, 6),
        },
        "clusters": cluster_summary,
    }


def train_candidate(
    *,
    name: str,
    config: dict,
    base_model: Path,
    output_dir: Path,
    opening_samples: list[dict],
    retention_samples: list[dict],
    hard_negative_source: Path,
    topk: int,
) -> dict:
    candidate_dir = output_dir / name
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate_model = candidate_dir / "chess_experiment_5_nnue_candidate.json"
    candidate_replay = candidate_dir / "chess_experiment_5_nnue_candidate_replay.jsonl"
    _write_base_candidate_if_needed(base_model, candidate_model)
    samples = list(opening_samples)
    if config.get("include_retention"):
        samples.extend(retention_samples)
    samples, hn_summary = inject_hard_negatives(samples, source_model=hard_negative_source, topk=topk)
    samples_path = candidate_dir / "train_samples.jsonl"
    _write_jsonl(samples_path, samples)
    started = time.perf_counter()
    trainer = train_experiment_nnue_from_replay_samples(
        samples,
        model_path=candidate_model,
        replay_path=candidate_replay,
        replace_replay=True,
        epochs=int(config.get("epochs") or 1),
    )
    seconds = round(time.perf_counter() - started, 6)
    return {
        "name": name,
        "config": config,
        "candidate_model_path": str(candidate_model),
        "candidate_replay_path": str(candidate_replay),
        "train_samples_path": str(samples_path),
        "candidate_sha256": _sha256_file(candidate_model),
        "train_samples_hash": _hash_rows(samples),
        "train_sample_count": len(samples),
        "source_breakdown": dict(Counter(str(sample.get("source") or "") for sample in samples)),
        "hard_negative_summary": hn_summary,
        "trainer_result": trainer,
        "retrain_seconds": seconds,
    }


def _write_summary_md(path: Path, summary: dict) -> None:
    lines = [
        "# exp5_15 opening curriculum candidate search",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- current_production_sha_expected: `{summary['current_production_sha_expected']}`",
        f"- current_production_sha_actual: `{summary['current_production_sha_actual']}`",
        f"- current_model_source: `{summary['current_model_source']}`",
        f"- runtime_mutated: `{summary['runtime_mutated']}`",
        f"- stage_promote_attempted: `{summary['stage_promote_attempted']}`",
        f"- best_candidate: `{summary['best_candidate']}`",
        "",
        "## Candidates",
        "",
        "| candidate | opening score | opening delta | retention clean regressions | illegal | suspicious | verdict |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in summary["candidates"]:
        opening = row["opening_evaluation"]["overall"]
        retention = row["retention_evaluation"]["overall"]
        lines.append(
            "| "
            + " | ".join([
                row["name"],
                f"{opening['candidate_passed']}/{opening['cases']} = {opening['candidate_score']}",
                str(opening["score_delta"]),
                str(retention["clean_regressed_count"]),
                str(retention["illegal_rate"]),
                str(retention["suspicious_rate"]),
                row["verdict"],
            ])
            + " |"
        )
    lines.extend([
        "",
        "## Artifacts",
        "",
    ])
    for key, value in sorted(summary["artifacts"].items()):
        lines.append(f"- {key}: `{value}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_search(
    *,
    opening_curriculum: Path,
    retention_train: Path,
    exp5_13_summary: Path,
    production_model: Path,
    fallback_production_model: Path,
    output_dir: Path,
    search_profile: str = DEFAULT_SEARCH_PROFILE,
    max_retention_rows: int = 80,
    hard_negative_topk: int = 4,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    current_model, current_source = resolve_current_production_model(production_model, fallback_production_model)
    current_hash = _sha256_file(current_model)
    opening_rows = _iter_jsonl(opening_curriculum)
    retention_rows = _iter_jsonl(retention_train)
    exp5_rows = _read_summary_rows(exp5_13_summary)
    opening_samples = opening_rows_to_samples(opening_rows, weight=2.0)
    retention_samples = retention_rows_to_samples(retention_rows, max_rows=max_retention_rows, weight=0.6)
    _write_jsonl(output_dir / "opening_train_samples.jsonl", opening_samples)
    _write_jsonl(output_dir / "retention_train_samples.jsonl", retention_samples)

    configs = {
        "A_opening_only_e8_hn4": {"epochs": 8, "include_retention": False},
        "B_opening_retention_e8_hn4": {"epochs": 8, "include_retention": True},
    }
    candidates: list[dict] = []
    for name, config in configs.items():
        trained = train_candidate(
            name=name,
            config=config,
            base_model=current_model,
            output_dir=output_dir,
            opening_samples=opening_samples,
            retention_samples=retention_samples,
            hard_negative_source=current_model,
            topk=hard_negative_topk,
        )
        candidate_path = Path(trained["candidate_model_path"])
        opening_eval = evaluate_opening_pool(opening_rows, current_model=current_model, candidate_model=candidate_path, search_profile=search_profile)
        retention_eval = evaluate_retention(exp5_rows, candidate_model=candidate_path, search_profile=search_profile)
        opening_path = output_dir / name / "opening_evaluation.json"
        retention_path = output_dir / name / "retention_evaluation.json"
        _write_json(opening_path, opening_eval)
        _write_json(retention_path, retention_eval)
        opening_delta = float(opening_eval["overall"]["score_delta"])
        retention_overall = retention_eval["overall"]
        smoke = retention_eval["clusters"].get("smoke", {})
        endgame = retention_eval["clusters"].get("endgame", {})
        passed = (
            opening_delta > 0
            and int(retention_overall["clean_regressed_count"]) == 0
            and float(retention_overall["illegal_rate"]) == 0.0
            and float(retention_overall["suspicious_rate"]) == 0.0
            and int(smoke.get("candidate_passed", 0)) >= int(smoke.get("current_passed", 0))
            and float(endgame.get("score_delta", 0.0)) >= 0.0
        )
        trained.update({
            "opening_evaluation": opening_eval,
            "retention_evaluation": {
                "overall": retention_eval["overall"],
                "clusters": retention_eval["clusters"],
                "detail_path": str(retention_path),
            },
            "opening_evaluation_path": str(opening_path),
            "retention_evaluation_path": str(retention_path),
            "candidate_can_stage_for_exp5_16": passed,
            "verdict": "stage_for_exp5_16" if passed else "blocked",
            "block_reasons": [] if passed else [
                reason
                for reason, flag in [
                    ("opening_score_not_improved", opening_delta <= 0),
                    ("retention_clean_regression", int(retention_overall["clean_regressed_count"]) > 0),
                    ("illegal_rate_nonzero", float(retention_overall["illegal_rate"]) != 0.0),
                    ("suspicious_rate_nonzero", float(retention_overall["suspicious_rate"]) != 0.0),
                    ("smoke_regression", int(smoke.get("candidate_passed", 0)) < int(smoke.get("current_passed", 0))),
                    ("endgame_regression", float(endgame.get("score_delta", 0.0)) < 0.0),
                ]
                if flag
            ],
        })
        candidates.append(trained)

    selectable = [row for row in candidates if row["candidate_can_stage_for_exp5_16"]]
    if selectable:
        best = max(
            selectable,
            key=lambda row: (
                float(row["opening_evaluation"]["overall"]["score_delta"]),
                float(row["retention_evaluation"]["clusters"].get("endgame", {}).get("score_delta", 0.0)),
                -int(row["retention_evaluation"]["overall"]["clean_regressed_count"]),
            ),
        )
        best_name = best["name"]
    else:
        best_name = ""
    artifacts = {
        "summary_json": str(output_dir / "summary.json"),
        "summary_md": str(output_dir / "SUMMARY.md"),
        "opening_train_samples": str(output_dir / "opening_train_samples.jsonl"),
        "retention_train_samples": str(output_dir / "retention_train_samples.jsonl"),
    }
    summary = {
        "ok": True,
        "generated_at": _now(),
        "engine": "experiment 5:nnue",
        "stage_promote_attempted": False,
        "runtime_mutated": False,
        "current_production_sha_expected": CURRENT_PRODUCTION_SHA,
        "current_production_sha_actual": current_hash,
        "current_production_sha_matches_expected": current_hash == CURRENT_PRODUCTION_SHA,
        "current_model_path": str(current_model),
        "current_model_source": current_source,
        "opening_curriculum": str(opening_curriculum),
        "opening_curriculum_rows": len(opening_rows),
        "opening_train_samples": len(opening_samples),
        "retention_train_rows": len(retention_rows),
        "retention_train_samples": len(retention_samples),
        "exp5_13_summary": str(exp5_13_summary),
        "search_profile": search_profile,
        "hard_negative_topk": hard_negative_topk,
        "best_candidate": best_name,
        "candidates": candidates,
        "artifacts": artifacts,
    }
    _write_json(Path(artifacts["summary_json"]), summary)
    _write_summary_md(Path(artifacts["summary_md"]), summary)
    return summary


def main() -> int:
    args = parse_args()
    result = run_search(
        opening_curriculum=Path(args.opening_curriculum_jsonl),
        retention_train=Path(args.retention_train_jsonl),
        exp5_13_summary=Path(args.exp5_13_summary),
        production_model=Path(args.production_model_path),
        fallback_production_model=Path(args.fallback_production_model_path),
        output_dir=Path(args.output_dir),
        search_profile=str(args.search_profile),
        max_retention_rows=int(args.max_retention_rows),
        hard_negative_topk=int(args.auto_hard_negative_topk),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
