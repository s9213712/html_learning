#!/usr/bin/env python3
"""Promote a validated exp5 candidate into the runtime production artifact.

This script intentionally does not train, regenerate, or mutate any non-exp5
model. It accepts only an exp5 production-readiness summary whose
`production_promote_request_ready` flag is true, verifies the exact candidate
hash, writes rollback artefacts, and then atomically switches the runtime model.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.chess_nnue import EXPERIMENT_NNUE_DIFFICULTY, default_chess_nnue_model_path  # noqa: E402
from services.games.chess_promotion import default_chess_candidate_dir, default_chess_promotion_status_path, production_engine_inventory  # noqa: E402


from scripts.games.common_paths import chess_results_root  # noqa: E402


DEFAULT_RESULTS_ROOT = chess_results_root()
DEFAULT_SUMMARY = DEFAULT_RESULTS_ROOT / "exp5_10_production_readiness" / "summary.json"
DEFAULT_OUTPUT_DIR = DEFAULT_RESULTS_ROOT / "exp5_12_production_promote"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Promote the validated exp5 candidate to runtime production.")
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--expected-candidate-sha256", default="")
    return parser.parse_args()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, tmp)
    tmp.replace(destination)


def _copy_optional(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def _preflight(summary: dict, *, expected_sha: str) -> dict:
    candidate_path = Path(str(summary.get("candidate_model_path") or "")).expanduser().resolve()
    runtime_path = default_chess_nnue_model_path().expanduser().resolve()
    summary_candidate_sha = str(summary.get("candidate_sha256") or "")
    actual_candidate_sha = _sha256_file(candidate_path) if candidate_path.exists() else ""
    policy = summary.get("production_policy") if isinstance(summary.get("production_policy"), dict) else {}
    strength_gate = summary.get("strength_gate") if isinstance(summary.get("strength_gate"), dict) else {}
    strength_summary = strength_gate.get("summary") if isinstance(strength_gate.get("summary"), dict) else {}
    promotion_gate = strength_summary.get("promotion_gate") if isinstance(strength_summary.get("promotion_gate"), dict) else {}
    reasons: list[str] = []
    if not candidate_path.exists():
        reasons.append("candidate_model_missing")
    if not bool(policy.get("production_promote_request_ready")):
        reasons.append("production_promote_request_not_ready")
    if bool(policy.get("runtime_model_mutated")):
        reasons.append("summary_already_reports_runtime_mutated")
    if bool(policy.get("production_promote")):
        reasons.append("summary_already_reports_production_promote")
    if summary_candidate_sha != actual_candidate_sha:
        reasons.append("candidate_sha_mismatch")
    if expected_sha and expected_sha != actual_candidate_sha:
        reasons.append("expected_candidate_sha_mismatch")
    if promotion_gate and not bool(promotion_gate.get("candidate_can_be_production_promoted")):
        reasons.append("strength_gate_not_production_promotable")
    return {
        "pass": not reasons,
        "reasons": reasons,
        "candidate_path": str(candidate_path),
        "runtime_path": str(runtime_path),
        "summary_candidate_sha256": summary_candidate_sha,
        "actual_candidate_sha256": actual_candidate_sha,
        "production_promote_request_ready": bool(policy.get("production_promote_request_ready")),
        "strength_gate_production_promotable": bool(promotion_gate.get("candidate_can_be_production_promoted")) if promotion_gate else None,
    }


def main() -> int:
    args = parse_args()
    summary_path = Path(args.summary_json).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = _load_json(summary_path)
    preflight = _preflight(summary, expected_sha=str(args.expected_candidate_sha256 or "").strip())
    if not preflight["pass"]:
        payload = {
            "ok": False,
            "promoted": False,
            "generated_at": _now(),
            "summary_json": str(summary_path),
            "preflight": preflight,
        }
        _write_json(output_dir / "summary.json", payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    candidate_path = Path(preflight["candidate_path"])
    runtime_path = Path(preflight["runtime_path"])
    staged_path = default_chess_candidate_dir() / "experiment_5_nnue"
    rollback_dir = output_dir / "rollback"
    previous_exists = runtime_path.exists()
    previous_hash = _sha256_file(runtime_path) if previous_exists else ""
    previous_snapshot = rollback_dir / "previous_chess_experiment_5_nnue_experience.json"
    previous_snapshot_created = _copy_optional(runtime_path, previous_snapshot)
    absence_marker = rollback_dir / "previous_runtime_absent.json"
    if not previous_exists:
        _write_json(absence_marker, {
            "runtime_path": str(runtime_path),
            "was_absent": True,
            "recorded_at": _now(),
            "rollback_instruction": "delete the promoted runtime model to restore the pre-promote absent-runtime state",
        })

    _atomic_copy(candidate_path, staged_path)
    staged_hash = _sha256_file(staged_path)
    before_copy_hash = _sha256_file(runtime_path) if runtime_path.exists() else ""
    _atomic_copy(staged_path, runtime_path)
    new_hash = _sha256_file(runtime_path)
    promoted = new_hash == preflight["actual_candidate_sha256"] == staged_hash
    status_path = default_chess_promotion_status_path()
    promotion_timestamp = _now()
    result = {
        "ok": promoted,
        "promoted": promoted,
        "engine": EXPERIMENT_NNUE_DIFFICULTY,
        "promotion_timestamp": promotion_timestamp,
        "summary_json": str(summary_path),
        "exp5_10_summary_path": str(summary_path),
        "candidate_artifact_path": str(candidate_path),
        "candidate_sha256": preflight["actual_candidate_sha256"],
        "staged_candidate_path": str(staged_path),
        "staged_candidate_sha256": staged_hash,
        "previous_runtime_model_path": str(runtime_path),
        "previous_runtime_exists": previous_exists,
        "previous_runtime_sha256": previous_hash,
        "previous_runtime_snapshot_path": str(previous_snapshot) if previous_snapshot_created else "",
        "previous_runtime_absence_marker": str(absence_marker) if not previous_exists else "",
        "new_runtime_model_path": str(runtime_path),
        "new_runtime_sha256": new_hash,
        "runtime_model_mutated": before_copy_hash != new_hash,
        "production_promote": promoted,
        "rollback_instruction": (
            f"copy {previous_snapshot} back to {runtime_path}"
            if previous_snapshot_created
            else f"delete {runtime_path} to restore the pre-promote absent-runtime state"
        ),
        "preflight": preflight,
        "post_promote_smoke_result": None,
        "post_promote_repeatability_result": None,
    }
    _write_json(output_dir / "summary.json", result)
    status = {
        "last_promotion_result": {
            "engine": EXPERIMENT_NNUE_DIFFICULTY,
            "candidate_path": str(staged_path),
            "candidate_artifact_path": str(candidate_path),
            "candidate_sha256": preflight["actual_candidate_sha256"],
            "production_path": str(runtime_path),
            "production_sha256": new_hash,
            "benchmark_report_path": str(summary.get("benchmark_path") or ""),
            "exp5_10_summary_path": str(summary_path),
            "promoted_at": promotion_timestamp,
            "result": "promoted" if promoted else "failed",
            "rollback_instruction": result["rollback_instruction"],
        },
        "current_production": production_engine_inventory(),
        "candidate": None,
        "updated_at": promotion_timestamp,
    }
    _write_json(status_path, status)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if promoted else 1


if __name__ == "__main__":
    raise SystemExit(main())
