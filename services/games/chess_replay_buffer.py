"""Replay collection helpers for chess engine training data.

Production policy requires that user games feed offline datasets instead of
mutating the live production models in-place. This module collects normalized
match replays with lightweight trust metadata so trainers can filter them later.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.server.runtime import default_runtime_root_path


DEFAULT_CHESS_REPLAY_BUFFER_NAME = "chess_replays.jsonl"
DEFAULT_CHESS_REPLAY_QUARANTINE_NAME = "chess_replays_quarantine.jsonl"
DEFAULT_CHESS_REPLAY_REJECTED_NAME = "chess_replays_rejected.jsonl"
_NATURAL_REASONS = {"checkmate", "stalemate", "king_missing", "resign", "insufficient_material"}


def default_chess_replay_buffer_path() -> Path:
    runtime_dir = os.environ.get("HACKME_RUNTIME_DIR", "").strip() or str(default_runtime_root_path())
    override = os.environ.get("HTML_LEARNING_CHESS_REPLAY_BUFFER_PATH", "").strip()
    return Path(override or os.path.join(runtime_dir, "reports", "games", DEFAULT_CHESS_REPLAY_BUFFER_NAME))


def default_chess_replay_quarantine_path() -> Path:
    runtime_dir = os.environ.get("HACKME_RUNTIME_DIR", "").strip() or str(default_runtime_root_path())
    override = os.environ.get("HTML_LEARNING_CHESS_REPLAY_QUARANTINE_PATH", "").strip()
    return Path(override or os.path.join(runtime_dir, "reports", "games", DEFAULT_CHESS_REPLAY_QUARANTINE_NAME))


def default_chess_replay_rejected_path() -> Path:
    runtime_dir = os.environ.get("HACKME_RUNTIME_DIR", "").strip() or str(default_runtime_root_path())
    override = os.environ.get("HTML_LEARNING_CHESS_REPLAY_REJECTED_PATH", "").strip()
    return Path(override or os.path.join(runtime_dir, "reports", "games", DEFAULT_CHESS_REPLAY_REJECTED_NAME))


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _normalize_history(row) -> list[dict]:
    try:
        history = json.loads(row["move_history_json"] or "[]")
    except Exception:
        history = []
    return history if isinstance(history, list) else []


def _normalize_source(source: str) -> str:
    value = str(source or "user_games").strip().lower()
    if value in {"self_play", "teacher_guidance", "user_games", "external", "benchmark", "imported_dataset"}:
        return value
    return "user_games"


def _natural_or_adjudicated(reason: str) -> str:
    normalized = str(reason or "").strip().lower()
    if normalized in _NATURAL_REASONS:
        return "natural"
    if normalized.startswith("adjudicated_") or normalized.startswith("max_plies"):
        return "adjudicated"
    return "natural" if normalized else "unknown"


def _white_black_engines(row) -> tuple[str, str]:
    mode = str(row["mode"] or "").strip().lower()
    difficulty = str(row["computer_difficulty"] or "normal").strip().lower()
    human_side = str(row["human_side"] or "white").strip().lower()
    if mode != "computer":
        return "user", "user"
    if human_side == "black":
        return difficulty, "user"
    return "user", difficulty


def _confidence_score(*, source: str, suspicious_flag: bool, move_count: int, result_reason: str) -> float:
    base = {
        "benchmark": 0.98,
        "teacher_guidance": 0.95,
        "self_play": 0.9,
        "imported_dataset": 0.88,
        "external": 0.75,
        "user_games": 0.42,
    }.get(source, 0.4)
    if move_count < 6:
        base -= 0.18
    if str(result_reason or "").strip().lower() == "resign" and move_count < 10:
        base -= 0.12
    if suspicious_flag:
        base -= 0.25
    return max(0.0, min(1.0, round(base, 4)))


def _resign_abuse_flag(*, source: str, move_count: int, result_reason: str) -> bool:
    return source == "user_games" and str(result_reason or "").strip().lower() == "resign" and move_count < 8


def _suspicious_flag(*, source: str, move_count: int, result_reason: str, history: list[dict]) -> bool:
    if source != "user_games":
        return False
    if move_count < 4:
        return True
    if str(result_reason or "").strip().lower() == "resign" and move_count < 8:
        return True
    uci_moves = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        uci = str(entry.get("uci") or "").strip().lower()
        if not uci:
            from_square = str(entry.get("from") or "").strip().lower()
            to_square = str(entry.get("to") or "").strip().lower()
            promotion = str(entry.get("promotion") or "").strip().lower()
            if from_square and to_square:
                uci = f"{from_square}{to_square}{promotion}"
        if uci:
            uci_moves.append(uci)
    return bool(uci_moves and len(set(uci_moves)) <= 2 and len(uci_moves) >= 6)


def _row_value(row, key, default=""):
    try:
        return row[key]
    except Exception:
        return default


def _duplicate_signature(*, opening_seed: str, result_reason: str, history: list[dict], actor_username: str | None, engine_name: str) -> str:
    moves = [str((entry or {}).get("uci") or "") for entry in history if isinstance(entry, dict)]
    payload = {
        "opening_seed": opening_seed,
        "result_reason": str(result_reason or "").strip().lower(),
        "actor_username": str(actor_username or "").strip().lower(),
        "engine_name": str(engine_name or "").strip().lower(),
        "moves": moves,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def build_replay_record(row, *, winner_color: str | None, source: str, actor_username: str | None = None) -> dict:
    history = _normalize_history(row)
    result_reason = str(_row_value(row, "result_reason", "") or "").strip()
    source = _normalize_source(source)
    white_engine, black_engine = _white_black_engines(row)
    opening_seed = str(_row_value(row, "initial_fen", "") or "").strip() or "standard_start"
    move_count = len(history)
    suspicious_flag = _suspicious_flag(source=source, move_count=move_count, result_reason=result_reason, history=history)
    resign_abuse_flag = _resign_abuse_flag(source=source, move_count=move_count, result_reason=result_reason)
    match_id = int(_row_value(row, "id", 0) or 0)
    engine_name = str(_row_value(row, "computer_difficulty", "") or "").strip().lower()
    replay_payload = {
        "match_id": match_id,
        "game_key": str(_row_value(row, "game_key", "chess") or "chess"),
        "mode": str(_row_value(row, "mode", "computer") or "computer"),
        "engine_name": engine_name,
        "engine_version": engine_name or "baseline",
        "white_engine": white_engine,
        "black_engine": black_engine,
        "opening_seed": opening_seed,
        "result": winner_color or "draw",
        "winner_color": winner_color,
        "adjudicated_or_natural": _natural_or_adjudicated(result_reason),
        "move_count": move_count,
        "timestamp": _now(),
        "source": source,
        "rating_estimate": None,
        "suspicious_flag": suspicious_flag,
        "duplicate_flag": False,
        "resign_abuse_flag": resign_abuse_flag,
        "confidence_score": _confidence_score(
            source=source,
            suspicious_flag=suspicious_flag,
            move_count=move_count,
            result_reason=result_reason,
        ),
        "collection_tier": "trusted",
        "quarantine_reasons": [],
        "actor_username": str(actor_username or "").strip() or None,
        "human_side": str(_row_value(row, "human_side", "white") or "white"),
        "computer_difficulty": str(_row_value(row, "computer_difficulty", "normal") or "normal"),
        "result_reason": result_reason,
        "updated_at": str(_row_value(row, "updated_at", "") or ""),
        "move_history": history,
    }
    fingerprint_source = json.dumps(
        {
            "match_id": replay_payload["match_id"],
            "updated_at": replay_payload["updated_at"],
            "result": replay_payload["result"],
            "move_history": replay_payload["move_history"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    replay_payload["replay_id"] = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    replay_payload["duplicate_signature"] = _duplicate_signature(
        opening_seed=opening_seed,
        result_reason=result_reason,
        history=history,
        actor_username=replay_payload["actor_username"],
        engine_name=engine_name,
    )
    return replay_payload


def _existing_replay_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                replay_id = str(payload.get("replay_id") or "").strip()
                if replay_id:
                    ids.add(replay_id)
    except Exception:
        return set()
    return ids


def _load_existing_signatures(paths: list[Path]) -> set[str]:
    signatures: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    signature = str(payload.get("duplicate_signature") or "").strip()
                    if signature:
                        signatures.add(signature)
        except Exception:
            continue
    return signatures


def classify_replay_record(record: dict, *, existing_signatures: set[str] | None = None) -> dict:
    existing_signatures = existing_signatures or set()
    tier = "trusted"
    reasons: list[str] = []
    move_count = int(record.get("move_count") or 0)
    source = str(record.get("source") or "user_games")
    duplicate_flag = str(record.get("duplicate_signature") or "") in existing_signatures
    resign_abuse_flag = bool(record.get("resign_abuse_flag"))
    suspicious_flag = bool(record.get("suspicious_flag"))
    if duplicate_flag:
        reasons.append("duplicate")
    if resign_abuse_flag:
        reasons.append("early_resign")
    if suspicious_flag:
        reasons.append("suspicious_pattern")
    if move_count <= 0:
        tier = "rejected"
        reasons.append("empty_history")
    elif source == "user_games" and move_count < 2:
        tier = "rejected"
        reasons.append("too_short")
    elif source == "user_games" and (duplicate_flag or resign_abuse_flag or suspicious_flag or move_count < 6):
        tier = "quarantine"
        if move_count < 6:
            reasons.append("low_move_count")
    elif source in {"external", "imported_dataset"} and suspicious_flag:
        tier = "quarantine"
    record["duplicate_flag"] = duplicate_flag
    record["collection_tier"] = tier
    record["quarantine_reasons"] = sorted(set(reasons))
    return record


def append_replay_record(record: dict, *, path: Path | None = None) -> bool:
    target = Path(path or default_chess_replay_buffer_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    replay_id = str(record.get("replay_id") or "").strip()
    if replay_id and replay_id in _existing_replay_ids(target):
        return False
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return True


def collect_match_replay(
    row,
    *,
    winner_color: str | None,
    source: str,
    actor_username: str | None = None,
    path: Path | None = None,
    quarantine_path: Path | None = None,
    rejected_path: Path | None = None,
) -> dict:
    record = build_replay_record(row, winner_color=winner_color, source=source, actor_username=actor_username)
    trusted_path = Path(path or default_chess_replay_buffer_path())
    quarantine_path = Path(quarantine_path or default_chess_replay_quarantine_path())
    rejected_path = Path(rejected_path or default_chess_replay_rejected_path())
    existing_signatures = _load_existing_signatures([trusted_path, quarantine_path, rejected_path])
    record = classify_replay_record(record, existing_signatures=existing_signatures)
    storage_path = trusted_path
    if record["collection_tier"] == "quarantine":
        storage_path = quarantine_path
    elif record["collection_tier"] == "rejected":
        storage_path = rejected_path
    record["stored"] = append_replay_record(record, path=storage_path)
    record["storage_path"] = str(storage_path)
    return record


def replay_buffer_summary(
    *,
    path: Path | None = None,
    quarantine_path: Path | None = None,
    rejected_path: Path | None = None,
    recent_window_days: int = 7,
) -> dict:
    target = Path(path or default_chess_replay_buffer_path())
    quarantine_target = Path(quarantine_path or default_chess_replay_quarantine_path())
    rejected_target = Path(rejected_path or default_chess_replay_rejected_path())
    summary = {
        "path": str(target),
        "quarantine_path": str(quarantine_target),
        "rejected_path": str(rejected_target),
        "exists": target.exists(),
        "total_replays": 0,
        "usable_replays": 0,
        "trusted_replays": 0,
        "quarantine_replays": 0,
        "rejected_replays": 0,
        "by_source": {},
        "suspicious_count": 0,
        "duplicate_count": 0,
        "resign_abuse_count": 0,
        "recent_user_games": 0,
        "last_timestamp": "",
        "train_split_size": 0,
        "eval_split_size": 0,
        "quarantine_reasons": {},
    }
    if not target.exists() and not quarantine_target.exists() and not rejected_target.exists():
        return summary
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(0, int(recent_window_days or 0)))
    seen = 0
    trusted_seen = 0
    sources = (
        ("trusted", target),
        ("quarantine", quarantine_target),
        ("rejected", rejected_target),
    )
    for bucket, current_path in sources:
        if not current_path.exists():
            continue
        try:
            with current_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    seen += 1
                    source = str(payload.get("source") or "unknown")
                    summary["by_source"][source] = int(summary["by_source"].get(source) or 0) + 1
                    if payload.get("suspicious_flag"):
                        summary["suspicious_count"] += 1
                    if payload.get("duplicate_flag"):
                        summary["duplicate_count"] += 1
                    if payload.get("resign_abuse_flag"):
                        summary["resign_abuse_count"] += 1
                    for reason in payload.get("quarantine_reasons") or []:
                        summary["quarantine_reasons"][reason] = int(summary["quarantine_reasons"].get(reason) or 0) + 1
                    timestamp = str(payload.get("timestamp") or "").strip()
                    if timestamp:
                        summary["last_timestamp"] = max(summary["last_timestamp"], timestamp)
                        if source == "user_games":
                            try:
                                stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).replace(tzinfo=None)
                                if stamp >= cutoff:
                                    summary["recent_user_games"] += 1
                            except Exception:
                                pass
                    if bucket == "trusted":
                        trusted_seen += 1
                        summary["usable_replays"] += 1
                        summary["trusted_replays"] += 1
                        if trusted_seen % 5 == 0:
                            summary["eval_split_size"] += 1
                        else:
                            summary["train_split_size"] += 1
                    elif bucket == "quarantine":
                        summary["quarantine_replays"] += 1
                    else:
                        summary["rejected_replays"] += 1
        except Exception:
            return summary
    summary["total_replays"] = seen
    return summary
