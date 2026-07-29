"""Warm-start and promotion helpers for chess engine artifacts."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from services.games.chess_dl import bundled_chess_dl_model_path, default_chess_dl_model_path, experiment_dl_model_template
from services.games.chess_engine import ChessExperimentStore, bundled_chess_engine_db_path, default_chess_engine_db_path
from services.games.chess_model_registry import ensure_runtime_model_from_bundle, runtime_chess_models_dir
from services.games.chess_nn import bundled_chess_nn_model_path, default_chess_nn_model_path, experiment_nn_model_template
from services.games.chess_nnue import default_chess_nnue_model_path, exp5_model_artifact_policy, experiment_nnue_model_template
from services.games.chess_pv import bundled_chess_pv_model_path, default_chess_pv_model_path, experiment_pv_model_template
from services.server.runtime import default_runtime_root_path


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def default_chess_candidate_dir() -> Path:
    return runtime_chess_models_dir() / "candidates"


def default_chess_promotion_status_path() -> Path:
    runtime_dir = os.environ.get("HACKME_RUNTIME_DIR", "").strip() or str(default_runtime_root_path())
    reports_root = os.environ.get("HTML_LEARNING_REPORTS_DIR", "").strip() or os.path.join(runtime_dir, "reports")
    return Path(reports_root) / "games" / "chess_promotion_status.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _benchmark_payload(path: Path) -> dict:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        return {}
    if "benchmark" in payload and isinstance(payload.get("benchmark"), dict):
        return payload
    if "standings" in payload and "matches" in payload:
        return {"benchmark": payload}
    return payload


def _ensure_model_file(path: Path, template_factory) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template_factory(), ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return True


def _ensure_runtime_model(runtime_path: Path, bundled_path: Path, template_factory) -> dict:
    copied = ensure_runtime_model_from_bundle(runtime_path, bundled_path)
    if copied["ok"]:
        return copied
    created = _ensure_model_file(runtime_path, template_factory)
    copied["ok"] = True
    copied["created"] = created
    copied["copied"] = False
    copied["source"] = "template_fallback"
    return copied


def ensure_warm_start_chess_environment() -> dict:
    created = []
    exp1_result = ensure_runtime_model_from_bundle(default_chess_engine_db_path(), bundled_chess_engine_db_path())
    if not exp1_result["ok"]:
        exp1_store = ChessExperimentStore(default_chess_engine_db_path())
        conn = exp1_store.connect()
        conn.close()
        exp1_result = {
            "ok": True,
            "created": exp1_store.db_path.exists(),
            "copied": False,
            "runtime_path": str(exp1_store.db_path),
            "bundle_path": str(bundled_chess_engine_db_path()),
            "source": "schema_fallback",
        }
    else:
        exp1_store = ChessExperimentStore(default_chess_engine_db_path())
        conn = exp1_store.connect()
        conn.close()
    created.append({"engine": "experiment", **exp1_result, "path": str(default_chess_engine_db_path())})
    nn_result = _ensure_runtime_model(default_chess_nn_model_path(), bundled_chess_nn_model_path(), experiment_nn_model_template)
    created.append({"engine": "experiment 2:nn", **nn_result, "path": str(default_chess_nn_model_path())})
    dl_result = _ensure_runtime_model(default_chess_dl_model_path(), bundled_chess_dl_model_path(), experiment_dl_model_template)
    created.append({"engine": "experiment 3:dl", **dl_result, "path": str(default_chess_dl_model_path())})
    pv_result = _ensure_runtime_model(default_chess_pv_model_path(), bundled_chess_pv_model_path(), experiment_pv_model_template)
    created.append({"engine": "experiment 4:pv", **pv_result, "path": str(default_chess_pv_model_path())})
    nnue_policy = exp5_model_artifact_policy()
    nnue_result = {
        "ok": True,
        "created": False,
        "copied": False,
        "runtime_path": str(default_chess_nnue_model_path()),
        "bundle_path": "",
        "source": "source_embedded_static_base",
        "source_base_module": nnue_policy["source_base_module"],
        "static_base_model_sha256": nnue_policy["static_base_model_sha256"],
        "experience_delta_path": str(default_chess_nnue_model_path()),
    }
    created.append({"engine": "experiment 5:nnue", **nnue_result, "path": str(default_chess_nnue_model_path())})
    return {"ok": True, "timestamp": _now(), "artifacts": created}


def _current_engine_paths() -> dict[str, Path]:
    return {
        "experiment": default_chess_engine_db_path(),
        "experiment 2:nn": default_chess_nn_model_path(),
        "experiment 3:dl": default_chess_dl_model_path(),
        "experiment 4:pv": default_chess_pv_model_path(),
        "experiment 5:nnue": default_chess_nnue_model_path(),
    }


def production_engine_inventory() -> list[dict]:
    rows = []
    for engine, path in _current_engine_paths().items():
        info = {
            "engine": engine,
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(tzinfo=None).isoformat() + "Z" if path.exists() else "",
        }
        if engine == "experiment 5:nnue":
            base = experiment_nnue_model_template()
            policy = exp5_model_artifact_policy()
            info["source_embedded_base"] = True
            info["source_base_module"] = policy["source_base_module"]
            info["static_base_model_sha256"] = policy["static_base_model_sha256"]
            info["artifact_role"] = policy["generated_artifact_role"]
            info["base_architecture"] = str(base.get("architecture") or "")
            info["base_version"] = int(base.get("version") or 0)
            info["base_sample_count"] = int(base.get("sample_count") or 0)
        if path.exists() and path.suffix == ".json":
            payload = _load_json(path)
            info["architecture"] = str(payload.get("architecture") or "")
            info["version"] = int(payload.get("version") or 0)
            info["sample_count"] = int(payload.get("sample_count") or 0)
        rows.append(info)
    return rows


def promotion_status_summary() -> dict:
    path = default_chess_promotion_status_path()
    payload = _load_json(path)
    return {
        "path": str(path),
        "exists": path.exists(),
        "status": payload or {
            "last_promotion_result": None,
            "current_production": production_engine_inventory(),
            "candidate": None,
            "updated_at": "",
        },
    }


def evaluate_promotion_gate(*, engine: str, benchmark_report_path: Path) -> dict:
    path = Path(benchmark_report_path)
    payload = _benchmark_payload(path)
    benchmark = payload.get("benchmark") if isinstance(payload.get("benchmark"), dict) else {}
    smoke = payload.get("smoke_evaluation") if isinstance(payload.get("smoke_evaluation"), dict) else {}
    standings = benchmark.get("standings") if isinstance(benchmark.get("standings"), list) else []
    engine_row = next((row for row in standings if str(row.get("engine") or "") == engine), None)
    reasons: list[str] = []
    if not path.exists():
        reasons.append("benchmark report not found")
    if not benchmark:
        reasons.append("benchmark summary missing")
    if engine_row is None:
        reasons.append("engine not found in benchmark standings")
    suspicious_matches = len(benchmark.get("suspicious_matches") or [])
    smoke_pass = bool(smoke.get("pass")) if smoke else False
    score_rate = float(engine_row.get("score_rate") or 0.0) if engine_row else 0.0
    win_rate = float(engine_row.get("win_rate") or 0.0) if engine_row else 0.0
    games = int(engine_row.get("games") or 0) if engine_row else 0
    draws = int(engine_row.get("draws") or 0) if engine_row else 0
    draw_rate = (draws / games) if games > 0 else 1.0
    if games < 6:
        reasons.append("benchmark games too few")
    if score_rate < 0.45:
        reasons.append("score_rate below promotion threshold")
    if win_rate < 0.30:
        reasons.append("win_rate below promotion threshold")
    if draw_rate > 0.85:
        reasons.append("draw_rate too high")
    if suspicious_matches > 0:
        reasons.append("benchmark suspicious matches present")
    if smoke and not smoke_pass:
        reasons.append("smoke evaluation failed")
    return {
        "pass": len(reasons) == 0,
        "engine": engine,
        "engine_architecture": {
            "experiment 3:dl": "mlp-49x64x32x1",
            "experiment 4:pv": "board-planes-policy-value-781x96",
            "experiment 5:nnue": "nnue-like-sparse-accumulator-v1",
        }.get(engine, ""),
        "benchmark_report_path": str(path),
        "score_rate": round(score_rate, 4),
        "win_rate": round(win_rate, 4),
        "draw_rate": round(draw_rate, 4),
        "games": games,
        "suspicious_matches": suspicious_matches,
        "smoke_pass": smoke_pass,
        "reasons": reasons,
    }


def promotion_report_consistency(*, engine: str, candidate_path: Path, benchmark_report_path: Path | None = None) -> dict:
    reasons: list[str] = []
    candidate = Path(candidate_path)
    payload = _load_json(candidate) if candidate.exists() and candidate.suffix == ".json" else {}
    expected_architecture = {
        "experiment 3:dl": "mlp-49x64x32x1",
        "experiment 4:pv": "board-planes-policy-value-781x96",
        "experiment 5:nnue": "nnue-like-sparse-accumulator-v1",
    }.get(engine, "")
    if not candidate.exists():
        reasons.append("candidate model file is missing")
    if expected_architecture and candidate.suffix == ".json":
        architecture = str(payload.get("architecture") or "")
        if architecture != expected_architecture:
            reasons.append(f"candidate architecture mismatch: expected {expected_architecture}, got {architecture or '-'}")
        if int(payload.get("version") or 0) <= 0:
            reasons.append("candidate version missing")
        if "sample_count" not in payload:
            reasons.append("candidate sample_count missing")
    if engine == "experiment 5:nnue":
        if str(payload.get("training_objective") or "") != "position_move_evaluator_delta":
            reasons.append("exp5 training_objective mismatch")
        if "feature_weights" not in payload or "piece_square_weights" not in payload:
            reasons.append("exp5 NNUE-like weight sections missing")
    benchmark_path = Path(benchmark_report_path) if benchmark_report_path else None
    return {
        "pass": not reasons,
        "engine": engine,
        "candidate_path": str(candidate),
        "benchmark_report_path": str(benchmark_path) if benchmark_path else "",
        "expected_architecture": expected_architecture,
        "candidate_architecture": str(payload.get("architecture") or "") if payload else "",
        "candidate_version": int(payload.get("version") or 0) if payload else 0,
        "candidate_sample_count": int(payload.get("sample_count") or 0) if payload else 0,
        "reasons": reasons,
    }


def _candidate_dest(engine: str) -> Path:
    safe_name = engine.replace(" ", "_").replace(":", "_")
    return default_chess_candidate_dir() / safe_name


def stage_candidate_model(*, engine: str, source_path: Path, benchmark_report_path: Path | None = None) -> dict:
    current_paths = _current_engine_paths()
    if engine not in current_paths or engine == "experiment":
        raise ValueError("only experiment 2:nn / experiment 3:dl / experiment 4:pv / experiment 5:nnue support file-based candidates")
    source = Path(source_path)
    if not source.exists():
        raise ValueError("candidate model file not found")
    candidate_path = _candidate_dest(engine)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, candidate_path)
    status = promotion_status_summary()["status"]
    promotion_gate = evaluate_promotion_gate(engine=engine, benchmark_report_path=Path(benchmark_report_path)) if benchmark_report_path else None
    consistency = promotion_report_consistency(
        engine=engine,
        candidate_path=candidate_path,
        benchmark_report_path=benchmark_report_path,
    )
    status["candidate"] = {
        "engine": engine,
        "candidate_path": str(candidate_path),
        "benchmark_report_path": str(benchmark_report_path) if benchmark_report_path else "",
        "promotion_gate": promotion_gate,
        "promotion_report_consistency": consistency,
        "staged_at": _now(),
    }
    status["updated_at"] = _now()
    _save_json(default_chess_promotion_status_path(), status)
    return {"ok": True, "engine": engine, "candidate_path": str(candidate_path), "promotion_gate": promotion_gate, "promotion_report_consistency": consistency}


def promote_candidate_model(*, engine: str, benchmark_report_path: Path) -> dict:
    status = promotion_status_summary()["status"]
    candidate = status.get("candidate") or {}
    if str(candidate.get("engine") or "") != engine:
        raise ValueError("no staged candidate for this engine")
    candidate_path = Path(str(candidate.get("candidate_path") or ""))
    if not candidate_path.exists():
        raise ValueError("staged candidate file is missing")
    benchmark_path = Path(benchmark_report_path)
    if not benchmark_path.exists():
        raise ValueError("benchmark report is required before promotion")
    gate = evaluate_promotion_gate(engine=engine, benchmark_report_path=benchmark_path)
    if not gate["pass"]:
        raise ValueError("promotion gate failed: " + "; ".join(gate["reasons"]))
    consistency = promotion_report_consistency(engine=engine, candidate_path=candidate_path, benchmark_report_path=benchmark_path)
    if not consistency["pass"]:
        raise ValueError("promotion report consistency failed: " + "; ".join(consistency["reasons"]))
    destination = _current_engine_paths().get(engine)
    if destination is None:
        raise ValueError("unsupported engine")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(candidate_path, destination)
    status["last_promotion_result"] = {
        "engine": engine,
        "candidate_path": str(candidate_path),
        "production_path": str(destination),
        "benchmark_report_path": str(benchmark_path),
        "promotion_gate": gate,
        "promotion_report_consistency": consistency,
        "promoted_at": _now(),
        "result": "promoted",
    }
    status["candidate"] = None
    status["current_production"] = production_engine_inventory()
    status["updated_at"] = _now()
    _save_json(default_chess_promotion_status_path(), status)
    return {"ok": True, "engine": engine, "production_path": str(destination)}
