#!/usr/bin/env python3
"""Candidate-only exp5 retrain smoke from normal complete-game play.

The workflow is intentionally conservative:

1. Play normal exp5 gauntlet games with an explicit baseline model path.
2. Extract exp5 positions from AI turns.
3. Distill teacher labels, dropping questionable rows.
4. Retrain a candidate in an isolated output directory.
5. Gate the candidate and optionally run the current advanced score suite.

No production or bundled model is replaced by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chess


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.games.common_paths import runtime_model_path  # noqa: E402


BASELINE_ADVANCED_SCORE_V10 = 84.1482
BASELINE_GAUNTLET_AI_WINS_V10 = 18
BASELINE_TACTICAL_CASES_V10 = 300


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _progress(message: str) -> None:
    print(f"[chess-exp5-normal-retrain] {message}", file=sys.stderr, flush=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_no}: row must be an object")
        rows.append(payload)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _run_command(
    cmd: list[str],
    *,
    env: dict[str, str],
    output_dir: Path,
    name: str,
    expect_json_stdout: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    _progress(f"run {name}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True, capture_output=True, check=False)
    elapsed = round(time.perf_counter() - started, 3)
    stdout_path = output_dir / "logs" / f"{name}.stdout"
    stderr_path = output_dir / "logs" / f"{name}.stderr"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(proc.stdout, encoding="utf-8")
    stderr_path.write_text(proc.stderr, encoding="utf-8")
    record = {
        "name": name,
        "cmd": cmd,
        "returncode": int(proc.returncode),
        "elapsed_seconds": elapsed,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    with (output_dir / "commands.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    if proc.returncode != 0:
        raise RuntimeError(f"{name} failed with exit {proc.returncode}; stderr={proc.stderr[-2000:]}")
    payload: dict[str, Any] = {}
    if expect_json_stdout:
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:
            raise RuntimeError(f"{name} did not emit JSON stdout; see {stdout_path}") from exc
    return {"record": record, "json": payload}


def _side_from_fen(fen: str) -> str:
    parts = fen.split()
    return "white" if len(parts) > 1 and parts[1] == "w" else "black"


def _normal_opening_list(raw: str) -> list[str]:
    openings = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    if not openings:
        return ["start", "open_game", "sicilian", "french", "caro_kann"]
    return openings


def _generate_games(args: argparse.Namespace, output_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    os.environ.update(env)
    from scripts.games import chess_exp5_gauntlet as gauntlet  # noqa: WPS433
    from scripts.games import game_ai_codex_play_eval as codex_eval  # noqa: WPS433

    openings = _normal_opening_list(args.openings)
    rows: list[dict[str, Any]] = []
    for game_index in range(1, int(args.games) + 1):
        opening_id = openings[(game_index - 1) % len(openings)]
        line = gauntlet.OPENING_LINES.get(opening_id)
        if line is None:
            raise ValueError(f"unknown opening id: {opening_id}")
        codex_color = "white" if game_index % 2 == 1 else "black"
        seed = codex_eval.stable_seed(int(args.seed), "normal_retrain", opening_id, game_index)
        _progress(f"phase normal-game {game_index}/{args.games}: opening={opening_id} codex_color={codex_color}")
        rows.append(
            gauntlet.play_game(
                opening_id=opening_id,
                line=line,
                codex_color_name=codex_color,
                seed=seed,
                max_plies=max(1, int(args.max_plies)),
            )
        )

    json_path = output_dir / "normal_games.json"
    jsonl_path = output_dir / "normal_games.jsonl"
    artifact = {
        "generated_at": _iso_now(),
        "baseline_model_path": str(args.baseline_model_path),
        "baseline_model_sha256": _sha256_file(Path(args.baseline_model_path)),
        "method": {
            "games": int(args.games),
            "max_plies": int(args.max_plies),
            "openings": openings,
            "seed": int(args.seed),
            "model_env_pin": "HTML_LEARNING_CHESS_ENGINE_NNUE_MODEL_PATH",
        },
        "summary": gauntlet.summarize(rows),
        "games": rows,
    }
    _write_json(json_path, artifact)
    _write_jsonl(jsonl_path, rows)
    _progress(
        "phase normal-game summary: "
        f"games={len(rows)} ai_wins={artifact['summary']['ai_wins']} "
        f"draws={artifact['summary']['draws']} codex_wins={artifact['summary']['codex_wins']} "
        f"complete={artifact['summary']['complete_game_rate']}"
    )
    return {"json": json_path, "jsonl": jsonl_path, "artifact": artifact}


def _extract_positions(games: list[dict[str, Any]], *, max_positions: int, seed: int, output_dir: Path) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for game_no, game in enumerate(games, start=1):
        for move in game.get("moves") or []:
            if str(move.get("actor") or "") != "ai":
                continue
            fen = str(move.get("fen_before") or "").strip()
            uci = str(move.get("uci") or "").strip().lower()
            if not fen or not uci:
                continue
            try:
                board = chess.Board(fen)
            except Exception:
                continue
            if board.is_game_over(claim_draw=True):
                continue
            side = _side_from_fen(fen)
            key = f"{fen}|{side}"
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "fen": fen,
                    "side": side,
                    "target": 1.0,
                    "weight": 0.8,
                    "source": "exp5_normal_10_game_position",
                    "normal_game_index": game_no,
                    "normal_game_opening": str(game.get("opening_id") or ""),
                    "normal_game_result": str(game.get("result") or ""),
                    "normal_game_reason": str(game.get("reason") or ""),
                    "original_ai_move_uci": uci,
                    "original_ai_move_san": str(move.get("san") or ""),
                    "ply": int(move.get("ply") or 0),
                }
            )
    selected = list(candidates)
    if max_positions > 0 and len(selected) > max_positions:
        rng = random.Random(seed)
        rng.shuffle(selected)
        selected = selected[:max_positions]
        selected.sort(key=lambda row: (int(row.get("normal_game_index") or 0), int(row.get("ply") or 0), str(row.get("fen") or "")))
    path = output_dir / "normal_positions_for_teacher.jsonl"
    _write_jsonl(path, selected)
    summary = {
        "path": str(path),
        "candidate_positions": len(candidates),
        "selected_positions": len(selected),
        "max_positions": int(max_positions),
        "selection": "dedupe_fen_side_then_seeded_shuffle_cap",
    }
    _write_json(output_dir / "normal_positions_summary.json", summary)
    _progress(f"phase extract positions: selected={len(selected)} from={len(candidates)} path={path}")
    return {"path": path, "summary": summary}


def _split_distill_rows(distill_path: Path, output_dir: Path) -> dict[str, Any]:
    rows = _read_jsonl(distill_path)
    train_rows = [row for row in rows if str(row.get("dataset_split_bucket") or "train") != "eval"]
    eval_rows = [row for row in rows if str(row.get("dataset_split_bucket") or "train") == "eval"]
    if len(eval_rows) < 8 and len(train_rows) >= 12:
        train_rows.sort(key=lambda row: str(row.get("position_id") or row.get("fen") or ""))
        needed = max(8 - len(eval_rows), max(1, len(train_rows) // 5))
        moved = train_rows[-needed:]
        train_rows = train_rows[:-needed]
        for row in moved:
            row = dict(row)
            row["dataset_split_bucket"] = "eval_fallback_holdout"
            eval_rows.append(row)
    strength_cases: list[dict[str, Any]] = []
    for index, row in enumerate(eval_rows, start=1):
        move_uci = str(row.get("move_uci") or row.get("teacher_move") or "").strip().lower()
        teacher_top3 = [str(item).strip().lower() for item in (row.get("teacher_top3") or []) if str(item).strip()]
        if move_uci and move_uci not in teacher_top3:
            teacher_top3.insert(0, move_uci)
        strength_cases.append(
            {
                "id": str(row.get("position_id") or f"normal_retrain_eval_{index:04d}"),
                "fen": str(row.get("fen") or ""),
                "side": str(row.get("side") or _side_from_fen(str(row.get("fen") or ""))),
                "category": "normal_game_teacher_holdout",
                "teacher_move": move_uci,
                "teacher_top3": teacher_top3[:3],
                "teacher_top5": [str(item).strip().lower() for item in (row.get("teacher_top5") or []) if str(item).strip()],
                "label_quality": str(row.get("label_quality") or "clean"),
                "source": "exp5_normal_retrain_eval_holdout",
                "confidence": float(row.get("confidence_score_baseline") or 0.42),
            }
        )

    train_path = output_dir / "normal_train_rows.jsonl"
    eval_path = output_dir / "normal_eval_rows.jsonl"
    strength_path = output_dir / "normal_strength_cases.jsonl"
    _write_jsonl(train_path, train_rows)
    _write_jsonl(eval_path, eval_rows)
    _write_jsonl(strength_path, strength_cases)
    summary = {
        "distill_rows": len(rows),
        "train_rows": len(train_rows),
        "eval_rows": len(eval_rows),
        "strength_cases": len(strength_cases),
        "train_rows_jsonl": str(train_path),
        "eval_rows_jsonl": str(eval_path),
        "strength_cases_jsonl": str(strength_path),
    }
    _write_json(output_dir / "normal_distill_split_summary.json", summary)
    _progress(
        "phase split distill: "
        f"distill={len(rows)} train={len(train_rows)} eval={len(eval_rows)} strength={len(strength_cases)}"
    )
    if not train_rows:
        raise RuntimeError("no train rows after teacher distill split")
    if not strength_cases:
        raise RuntimeError("no strength cases after teacher distill split")
    return {"summary": summary, "train_path": train_path, "eval_path": eval_path, "strength_path": strength_path}


def _run_distill(args: argparse.Namespace, output_dir: Path, env: dict[str, str], positions_path: Path) -> dict[str, Any]:
    distill_path = output_dir / "normal_teacher_distill_all.jsonl"
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "games" / "chess_exp5_teacher_distill.py"),
        "--input-jsonl",
        str(positions_path),
        "--output-jsonl",
        str(distill_path),
        "--replace-output",
        "--teacher-depth",
        str(int(args.teacher_depth)),
        "--weight",
        str(float(args.sample_weight)),
        "--source",
        "exp5_normal_10_game_teacher_distill",
        "--baseline-model-path",
        str(args.baseline_model_path),
        "--baseline-probe-profile",
        "fixed_depth_fast",
        "--source-category",
        "user_games",
        "--eval-mod",
        str(int(args.eval_mod)),
        "--teacher-top-k",
        "5",
        "--drop-questionable",
        "--audit-jsonl",
        str(output_dir / "normal_teacher_distill_audit.jsonl"),
        "--quarantine-jsonl",
        str(output_dir / "normal_teacher_distill_quarantine.jsonl"),
    ]
    result = _run_command(cmd, env=env, output_dir=output_dir, name="teacher_distill", expect_json_stdout=True)
    payload = result["json"]
    _write_json(output_dir / "normal_teacher_distill_summary.json", payload)
    _progress(
        "phase teacher distill summary: "
        f"input={payload.get('input_rows')} accepted={payload.get('accepted_samples')} "
        f"quarantine={payload.get('quarantine_rows_written')}"
    )
    return {"path": distill_path, "payload": payload}


def _run_repeatability_gate(
    args: argparse.Namespace,
    output_dir: Path,
    env: dict[str, str],
    train_path: Path,
    strength_path: Path,
    eval_count: int,
) -> dict[str, Any]:
    gate_dir = output_dir / "repeatability_gate"
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "games" / "chess_exp5_repeatability_gate.py"),
        "--baseline-model-path",
        str(args.baseline_model_path),
        "--distill-jsonl",
        str(train_path),
        "--train-rows-jsonl",
        str(train_path),
        "--strength-cases-jsonl",
        str(strength_path),
        "--heldout-source-jsonl",
        str(strength_path),
        "--output-dir",
        str(gate_dir),
        "--run-count",
        str(int(args.repeatability_runs)),
        "--seeds",
        str(args.repeatability_seeds),
        "--heldout-count",
        str(max(1, min(int(args.heldout_count), int(eval_count)))),
        "--smoke-count",
        str(int(args.smoke_count)),
        "--search-profile",
        str(args.search_profile),
        "--epochs",
        str(int(args.epochs)),
        "--auto-hard-negative-topk",
        str(int(args.auto_hard_negative_topk)),
        "--multi-good-margin-cp",
        str(float(args.multi_good_margin_cp)),
        "--label-quality-weight-clean",
        str(float(args.label_quality_weight_clean)),
        "--label-quality-weight-review",
        str(float(args.label_quality_weight_review)),
        "--label-quality-weight-questionable",
        "0.0",
    ]
    result = _run_command(cmd, env=env, output_dir=output_dir, name="repeatability_gate", expect_json_stdout=True)
    payload = result["json"]
    _write_json(output_dir / "repeatability_gate_summary.json", payload)
    _progress(
        "phase repeatability summary: "
        f"tier={payload.get('tier', {}).get('tier')} "
        f"stage={payload.get('tier', {}).get('stage_candidate')} "
        f"mean_delta={payload.get('repeatability', {}).get('mean_delta')}"
    )
    return payload


def _best_candidate_from_repeatability(payload: dict[str, Any]) -> dict[str, Any]:
    runs = list(payload.get("runs") or [])
    if not runs:
        raise RuntimeError("repeatability gate returned no runs")
    runs.sort(key=lambda row: (float(row.get("score_delta") or 0.0), float(row.get("candidate_score") or 0.0)), reverse=True)
    best = dict(runs[0])
    candidate = Path(str(best.get("candidate_model_path") or "")).expanduser().resolve()
    if not candidate.exists():
        raise RuntimeError(f"best candidate missing: {candidate}")
    best["candidate_model_path"] = str(candidate)
    best["candidate_sha256_actual"] = _sha256_file(candidate)
    return best


def _run_advanced_suite(args: argparse.Namespace, output_dir: Path, env: dict[str, str], candidate_path: Path) -> dict[str, Any]:
    candidate_env = dict(env)
    candidate_env["HTML_LEARNING_CHESS_ENGINE_NNUE_MODEL_PATH"] = str(candidate_path)
    score_probe = output_dir / "candidate_score_probe.json"
    tactical = output_dir / "candidate_tactical_suite_300.json"
    gauntlet = output_dir / "candidate_gauntlet_30.json"
    gauntlet_jsonl = output_dir / "candidate_gauntlet_30.jsonl"
    advanced = output_dir / "candidate_advanced_score.json"

    _run_command(
        [
            sys.executable,
            str(ROOT / "scripts" / "games" / "chess_exp5_score_probe.py"),
            "--output",
            str(score_probe),
            "--games-per-side",
            "3",
            "--chess-max-plies",
            "120",
            "--external-case-limit",
            "24",
        ],
        env=candidate_env,
        output_dir=output_dir,
        name="candidate_score_probe",
        expect_json_stdout=False,
    )
    _run_command(
        [
            sys.executable,
            str(ROOT / "scripts" / "games" / "chess_exp5_tactical_suite.py"),
            "--target-cases",
            str(int(args.tactical_cases)),
            "--pgn-case-count",
            str(int(args.pgn_case_count)),
            "--output",
            str(tactical),
        ],
        env=candidate_env,
        output_dir=output_dir,
        name="candidate_tactical_suite",
        expect_json_stdout=False,
    )
    _run_command(
        [
            sys.executable,
            str(ROOT / "scripts" / "games" / "chess_exp5_gauntlet.py"),
            "--games-per-opening",
            "2",
            "--max-plies",
            str(int(args.gauntlet_max_plies)),
            "--openings",
            str(args.gauntlet_openings),
            "--output",
            str(gauntlet),
            "--jsonl-output",
            str(gauntlet_jsonl),
        ],
        env=candidate_env,
        output_dir=output_dir,
        name="candidate_gauntlet",
        expect_json_stdout=False,
    )
    _run_command(
        [
            sys.executable,
            str(ROOT / "scripts" / "games" / "chess_exp5_advanced_score.py"),
            "--score-probe",
            str(score_probe),
            "--tactical-suite",
            str(tactical),
            "--gauntlet",
            str(gauntlet),
            "--output",
            str(advanced),
        ],
        env=candidate_env,
        output_dir=output_dir,
        name="candidate_advanced_score",
        expect_json_stdout=False,
    )
    artifacts = {
        "score_probe": str(score_probe),
        "tactical_suite": str(tactical),
        "gauntlet": str(gauntlet),
        "gauntlet_jsonl": str(gauntlet_jsonl),
        "advanced_score": str(advanced),
    }
    score_payload = _read_json(score_probe)
    tactical_payload = _read_json(tactical)
    gauntlet_payload = _read_json(gauntlet)
    advanced_payload = _read_json(advanced)
    summary = {
        "artifacts": artifacts,
        "score_probe_summary": score_payload.get("summary") or {},
        "tactical_summary": tactical_payload.get("summary") or {},
        "gauntlet_summary": gauntlet_payload.get("summary") or {},
        "advanced_score": advanced_payload,
    }
    _write_json(output_dir / "candidate_advanced_suite_summary.json", summary)
    _progress(
        "phase advanced suite summary: "
        f"score={advanced_payload.get('normalized_100')} "
        f"gauntlet_ai_wins={summary['gauntlet_summary'].get('ai_wins')} "
        f"gauntlet_draws={summary['gauntlet_summary'].get('draws')}"
    )
    return summary


def _promotion_decision(args: argparse.Namespace, best: dict[str, Any], advanced_summary: dict[str, Any] | None) -> dict[str, Any]:
    if not advanced_summary:
        return {
            "promotion_allowed": False,
            "decision": "not_evaluated",
            "reasons": ["advanced_suite_skipped"],
            "default_model_mutated": False,
        }
    advanced = advanced_summary.get("advanced_score") or {}
    gauntlet = advanced_summary.get("gauntlet_summary") or {}
    tactical = advanced_summary.get("tactical_summary") or {}
    score_probe = advanced_summary.get("score_probe_summary") or {}
    fixed = score_probe.get("fixed_summary") or {}
    failed_fixed = score_probe.get("failed_fixed") or []
    tactical_cases = int(tactical.get("cases") or 0)
    tactical_passed = int(tactical.get("passed") or 0)
    candidate_score = float(advanced.get("normalized_100") or 0.0)
    baseline_score = float(args.baseline_advanced_score)
    reasons: list[str] = []
    if candidate_score < baseline_score + float(args.min_advanced_score_improvement):
        reasons.append("advanced_score_not_above_required_margin")
    if failed_fixed:
        reasons.append("fixed_probe_regression")
    if tactical_cases < BASELINE_TACTICAL_CASES_V10 or tactical_passed < tactical_cases:
        reasons.append("tactical_suite_not_fully_passed")
    if int(gauntlet.get("codex_wins") or 0) > 0:
        reasons.append("complete_gauntlet_loss")
    if int(gauntlet.get("ai_wins") or 0) < BASELINE_GAUNTLET_AI_WINS_V10:
        reasons.append("gauntlet_win_count_below_v10")
    if float(gauntlet.get("threefold_rate") or 0.0) > 0.45:
        reasons.append("threefold_rate_too_high")
    if not bool(best.get("candidate_can_be_staged")):
        reasons.append("repeatability_stage_gate_failed")
    return {
        "promotion_allowed": not reasons,
        "decision": "candidate_better_and_gate_clean" if not reasons else "report_only_rejected",
        "reasons": reasons,
        "default_model_mutated": False,
        "baseline_advanced_score": baseline_score,
        "candidate_advanced_score": candidate_score,
        "candidate_score_delta_vs_baseline": round(candidate_score - baseline_score, 4),
        "fixed_probe_total_cases": int(fixed.get("total_cases") or 0),
        "failed_fixed_count": len(failed_fixed),
        "tactical_cases": tactical_cases,
        "tactical_passed": tactical_passed,
        "gauntlet_ai_wins": int(gauntlet.get("ai_wins") or 0),
        "gauntlet_draws": int(gauntlet.get("draws") or 0),
        "gauntlet_losses": int(gauntlet.get("codex_wins") or 0),
        "repeatability_candidate_can_be_staged": bool(best.get("candidate_can_be_staged")),
        "candidate_model_path": str(best.get("candidate_model_path") or ""),
        "candidate_sha256": str(best.get("candidate_sha256_actual") or ""),
    }


def _write_markdown_report(output_dir: Path, summary: dict[str, Any]) -> Path:
    report_path = output_dir / "SUMMARY.md"
    normal = summary.get("normal_games") or {}
    distill = summary.get("teacher_distill", {}).get("quality_audit") or {}
    split = summary.get("distill_split") or {}
    repeatability = summary.get("repeatability", {}).get("repeatability") or {}
    tier = summary.get("repeatability", {}).get("tier") or {}
    decision = summary.get("promotion_decision") or {}
    lines = [
        "# Exp5 Normal-Game Retrain Smoke",
        "",
        f"- generated_at: `{summary.get('generated_at')}`",
        f"- baseline_model_path: `{summary.get('baseline_model_path')}`",
        f"- baseline_model_sha256: `{summary.get('baseline_model_sha256')}`",
        f"- default_model_mutated: `{decision.get('default_model_mutated')}`",
        "",
        "## Phase Summary",
        f"- normal games: `{normal.get('games')}` games, AI `{normal.get('ai_wins')}`W/`{normal.get('draws')}`D/`{normal.get('codex_wins')}`L, complete_game_rate `{normal.get('complete_game_rate')}`",
        f"- extracted positions: `{summary.get('positions', {}).get('selected_positions')}` selected from `{summary.get('positions', {}).get('candidate_positions')}`",
        f"- teacher distill: accepted `{summary.get('teacher_distill', {}).get('accepted_samples')}` / input `{summary.get('teacher_distill', {}).get('input_rows')}`, quarantine `{summary.get('teacher_distill', {}).get('quarantine_rows_written')}`",
        f"- split: train `{split.get('train_rows')}`, eval `{split.get('eval_rows')}`, strength `{split.get('strength_cases')}`",
        f"- repeatability: mean_delta `{repeatability.get('mean_delta')}`, stage_candidate `{tier.get('stage_candidate')}`, shadow_candidate `{tier.get('shadow_candidate')}`, production_promote `{tier.get('production_promote')}`",
        f"- advanced score: candidate `{decision.get('candidate_advanced_score')}`, baseline_ref `{decision.get('baseline_advanced_score')}`, delta `{decision.get('candidate_score_delta_vs_baseline')}`",
        f"- promotion decision: `{decision.get('decision')}`, allowed `{decision.get('promotion_allowed')}`, reasons `{decision.get('reasons')}`",
        "",
        "## Artifacts",
        f"- normal_games_jsonl: `{summary.get('artifacts', {}).get('normal_games_jsonl')}`",
        f"- teacher_distill_jsonl: `{summary.get('artifacts', {}).get('teacher_distill_jsonl')}`",
        f"- train_rows_jsonl: `{summary.get('artifacts', {}).get('train_rows_jsonl')}`",
        f"- strength_cases_jsonl: `{summary.get('artifacts', {}).get('strength_cases_jsonl')}`",
        f"- repeatability_report_dir: `{summary.get('artifacts', {}).get('repeatability_report_dir')}`",
        f"- candidate_model_path: `{decision.get('candidate_model_path')}`",
        f"- advanced_score_json: `{summary.get('artifacts', {}).get('advanced_score_json')}`",
        "",
        "## Notes",
        "- The script pins `HTML_LEARNING_CHESS_ENGINE_NNUE_MODEL_PATH` for both baseline games and candidate validation.",
        "- Teacher `teacher_top3` / `teacher_top5` metadata is one-ply static ranking; questionable labels are quarantined, not trained.",
        "- This report is candidate-only. If a candidate is later promoted, snapshot the previous default model first with the user-approved versioned snapshot name.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run candidate-only exp5 retrain smoke after normal games.")
    parser.add_argument(
        "--baseline-model-path",
        default=str(runtime_model_path("chess_experiment_5_nnue_experience.json")),
    )
    parser.add_argument("--output-dir", default=str(ROOT / "docs" / "games" / f"2026-05-13_exp5_normal_retrain_smoke_{_utc_stamp()}"))
    parser.add_argument("--games", type=int, default=10)
    parser.add_argument("--openings", default="start,open_game,sicilian,french,caro_kann")
    parser.add_argument("--max-plies", type=int, default=240)
    parser.add_argument("--seed", type=int, default=20260513)
    parser.add_argument("--max-positions", type=int, default=180)
    parser.add_argument("--teacher-depth", type=int, default=2)
    parser.add_argument("--sample-weight", type=float, default=0.8)
    parser.add_argument("--eval-mod", type=int, default=5)
    parser.add_argument("--repeatability-runs", type=int, default=1)
    parser.add_argument("--repeatability-seeds", default="31")
    parser.add_argument("--heldout-count", type=int, default=24)
    parser.add_argument("--smoke-count", type=int, default=4)
    parser.add_argument("--search-profile", default="fixed_depth_strong")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--auto-hard-negative-topk", type=int, default=2)
    parser.add_argument("--multi-good-margin-cp", type=float, default=35.0)
    parser.add_argument("--label-quality-weight-clean", type=float, default=0.75)
    parser.add_argument("--label-quality-weight-review", type=float, default=0.3)
    parser.add_argument("--tactical-cases", type=int, default=300)
    parser.add_argument("--pgn-case-count", type=int, default=240)
    parser.add_argument("--gauntlet-max-plies", type=int, default=220)
    parser.add_argument(
        "--gauntlet-openings",
        default="start,open_game,sicilian,french,caro_kann,scandinavian,queen_pawn,queens_gambit,kings_indian,english,reti,fianchetto,kings_gambit,flank_probe,early_queen_probe",
    )
    parser.add_argument("--skip-advanced-suite", action="store_true")
    parser.add_argument("--baseline-advanced-score", type=float, default=BASELINE_ADVANCED_SCORE_V10)
    parser.add_argument("--min-advanced-score-improvement", type=float, default=0.25)
    parser.add_argument("--qa-server-url", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    baseline_path = Path(args.baseline_model_path).expanduser().resolve()
    if not baseline_path.exists():
        raise FileNotFoundError(f"baseline model missing: {baseline_path}")
    args.baseline_model_path = str(baseline_path)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["HTML_LEARNING_CHESS_ENGINE_NNUE_MODEL_PATH"] = str(baseline_path)
    generated_at = _iso_now()
    _progress(f"output_dir={output_dir}")
    _progress(f"baseline={baseline_path} sha256={_sha256_file(baseline_path)}")

    games_result = _generate_games(args, output_dir, env)
    positions = _extract_positions(
        list((games_result["artifact"] or {}).get("games") or []),
        max_positions=int(args.max_positions),
        seed=int(args.seed),
        output_dir=output_dir,
    )
    distill = _run_distill(args, output_dir, env, Path(positions["path"]))
    split = _split_distill_rows(Path(distill["path"]), output_dir)
    repeatability = _run_repeatability_gate(
        args,
        output_dir,
        env,
        Path(split["train_path"]),
        Path(split["strength_path"]),
        int(split["summary"]["eval_rows"]),
    )
    best = _best_candidate_from_repeatability(repeatability)
    _write_json(output_dir / "best_candidate.json", best)
    advanced_summary: dict[str, Any] | None = None
    if not bool(args.skip_advanced_suite):
        advanced_summary = _run_advanced_suite(args, output_dir, env, Path(best["candidate_model_path"]))
    decision = _promotion_decision(args, best, advanced_summary)

    summary = {
        "ok": True,
        "generated_at": generated_at,
        "finished_at": _iso_now(),
        "script": str(Path(__file__).resolve()),
        "qa_server_url": str(args.qa_server_url or ""),
        "baseline_model_path": str(baseline_path),
        "baseline_model_sha256": _sha256_file(baseline_path),
        "default_model_mutated": False,
        "normal_games": (games_result["artifact"] or {}).get("summary") or {},
        "positions": positions["summary"],
        "teacher_distill": distill["payload"],
        "distill_split": split["summary"],
        "repeatability": repeatability,
        "best_candidate": best,
        "advanced_suite": advanced_summary or {},
        "promotion_decision": decision,
        "artifacts": {
            "normal_games_json": str(games_result["json"]),
            "normal_games_jsonl": str(games_result["jsonl"]),
            "positions_jsonl": str(positions["path"]),
            "teacher_distill_jsonl": str(distill["path"]),
            "train_rows_jsonl": str(split["train_path"]),
            "eval_rows_jsonl": str(split["eval_path"]),
            "strength_cases_jsonl": str(split["strength_path"]),
            "repeatability_report_dir": str(output_dir / "repeatability_gate"),
            "advanced_score_json": str((output_dir / "candidate_advanced_score.json")) if advanced_summary else "",
            "summary_json": str(output_dir / "summary.json"),
            "summary_md": str(output_dir / "SUMMARY.md"),
            "commands_jsonl": str(output_dir / "commands.jsonl"),
        },
        "configuration": {
            "games": int(args.games),
            "max_plies": int(args.max_plies),
            "max_positions": int(args.max_positions),
            "teacher_depth": int(args.teacher_depth),
            "sample_weight": float(args.sample_weight),
            "epochs": int(args.epochs),
            "auto_hard_negative_topk": int(args.auto_hard_negative_topk),
            "label_quality_weight_clean": float(args.label_quality_weight_clean),
            "label_quality_weight_review": float(args.label_quality_weight_review),
            "baseline_advanced_score_reference": float(args.baseline_advanced_score),
        },
    }
    _write_json(output_dir / "summary.json", summary)
    report_path = _write_markdown_report(output_dir, summary)
    summary["artifacts"]["summary_md"] = str(report_path)
    _write_json(output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _progress(f"FAIL: {exc}")
        raise
