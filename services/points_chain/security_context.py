from __future__ import annotations

import re
from typing import Any

ATTACK_CLASSIFICATION_THRESHOLD = 70
ATTACK_CLASSIFICATION_MODEL = "pointschain_safe_mode_attack_classifier_v2"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_blob(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, dict):
            parts.extend(_text_blob(key, item) for key, item in value.items())
        elif isinstance(value, (list, tuple, set)):
            parts.extend(_text_blob(item) for item in value)
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts).lower()


def _collect_ip_evidence(value: Any, *, prefix: str = "") -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            key_text = str(key).lower()
            if "ip" in key_text and item not in (None, ""):
                evidence.append({"path": path, "value": str(item)})
            evidence.extend(_collect_ip_evidence(item, prefix=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            evidence.extend(_collect_ip_evidence(item, prefix=f"{prefix}[{index}]"))
    elif isinstance(value, str):
        for match in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", value):
            evidence.append({"path": prefix or "text", "value": match})
    return evidence


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _add_signal(scores: dict[str, int], signals: dict[str, list[dict[str, Any]]], method: str, score: int, evidence: str, detail: Any = None) -> None:
    scores[method] = scores.get(method, 0) + int(score)
    item: dict[str, Any] = {"score": int(score), "evidence": str(evidence)}
    if detail not in (None, ""):
        item["detail"] = detail
    signals.setdefault(method, []).append(item)


def _score_clock_guard(state: dict[str, Any], verification: dict[str, Any], restore_plan: dict[str, Any], scores: dict[str, int], signals: dict[str, list[dict[str, Any]]]) -> None:
    method = "suspected_governance_clock_manipulation"
    reason = str(state.get("reason") or "")
    violation = str(verification.get("violation") or "")
    guard_model = str(verification.get("guard_model") or "")
    if reason == "governance_clock_jump_detected":
        _add_signal(scores, signals, method, 45, "safe_mode.reason exact match", reason)
    if violation in {"wall_clock_fast_forward", "wall_clock_moved_backward"}:
        _add_signal(scores, signals, method, 35, "verification.violation exact match", violation)
    if guard_model == "wall_clock_vs_monotonic_v1":
        _add_signal(scores, signals, method, 20, "verification.guard_model exact match", guard_model)
    wall_elapsed = _as_float(verification.get("wall_elapsed_seconds"))
    monotonic_elapsed = _as_float(verification.get("monotonic_elapsed_seconds"))
    tolerance = _as_float(verification.get("tolerance_seconds"))
    if wall_elapsed is not None and monotonic_elapsed is not None and tolerance is not None:
        forward_jump = wall_elapsed - max(0.0, monotonic_elapsed)
        backward_jump = -wall_elapsed
        exceeded = forward_jump > tolerance or backward_jump > tolerance
        if exceeded:
            _add_signal(
                scores,
                signals,
                method,
                40,
                "measured clock delta exceeded configured tolerance",
                {
                    "wall_elapsed_seconds": wall_elapsed,
                    "monotonic_elapsed_seconds": monotonic_elapsed,
                    "tolerance_seconds": tolerance,
                    "forward_jump_seconds": round(forward_jump, 3),
                    "backward_jump_seconds": round(backward_jump, 3),
                },
            )
    action = str(verification.get("action") or "")
    if action and any(token in action.lower() for token in ("governance", "proposal", "vote", "timelock")):
        _add_signal(scores, signals, method, 10, "clock incident occurred during governance action", action)
    if restore_plan.get("governance_recovery_required") is True:
        _add_signal(scores, signals, method, 10, "restore plan requires governance recovery")


def _score_replay_or_double_spend(state: dict[str, Any], verification: dict[str, Any], restore_plan: dict[str, Any], scores: dict[str, int], signals: dict[str, list[dict[str, Any]]]) -> None:
    method = "suspected_double_spend_or_replay"
    blob = _text_blob(state.get("reason"), verification, restore_plan)
    strong_tokens = (
        "double_spend",
        "double spend",
        "nonce_reuse",
        "signature_replay",
        "signed payload/signature replay rejected",
        "idempotency collision",
    )
    if any(token in blob for token in strong_tokens):
        _add_signal(scores, signals, method, 55, "explicit replay/double-spend marker", [token for token in strong_tokens if token in blob])
    if any(token in blob for token in ("negative wallet", "negative balance", "insufficient locked", "overspend")):
        _add_signal(scores, signals, method, 30, "balance invariant indicates possible overspend")
    if any(key in verification for key in ("nonce", "signature_hash", "signed_payload_hash", "idempotency_key")):
        _add_signal(scores, signals, method, 20, "nonce/signature/idempotency evidence present")
    if any(token in blob for token in ("replay rejected", "cannot replay", "cross-branch replay")):
        _add_signal(scores, signals, method, 35, "replay rejection evidence present")


def _score_branch_or_fork(state: dict[str, Any], verification: dict[str, Any], restore_plan: dict[str, Any], scores: dict[str, int], signals: dict[str, list[dict[str, Any]]]) -> None:
    method = "suspected_branch_or_fork_integrity_incident"
    blob = _text_blob(state.get("reason"), verification, restore_plan)
    if any(token in blob for token in ("fork", "canonical branch mismatch", "branch mismatch", "wrong canonical")):
        _add_signal(scores, signals, method, 55, "explicit branch/fork mismatch marker")
    if any(key in restore_plan for key in ("selected_recovery_strategy", "recovery_strategy", "asset_universe", "excluded_refs")):
        _add_signal(scores, signals, method, 25, "branch recovery plan fields present", sorted(restore_plan.keys()))
    if any(key in verification for key in ("incident_tx_hash", "incident_tx_hashes", "excluded_refs", "parent_branch_uuid")):
        _add_signal(scores, signals, method, 30, "incident/recovery branch evidence present")
    if any(token in blob for token in ("rollback_branch", "recovery branch", "canonical", "parent replay")):
        _add_signal(scores, signals, method, 25, "branch rollback/recovery marker")


def _score_integrity_tampering(state: dict[str, Any], verification: dict[str, Any], restore_plan: dict[str, Any], scores: dict[str, int], signals: dict[str, list[dict[str, Any]]]) -> None:
    method = "suspected_ledger_integrity_tampering"
    reason = str(state.get("reason") or "")
    blob = _text_blob(reason, verification, restore_plan)
    if reason == "chain_verification_failed":
        _add_signal(scores, signals, method, 45, "safe_mode.reason exact match", reason)
    if any(token in blob for token in ("hash mismatch", "previous_hash", "merkle", "tamper", "signature mismatch", "invalid hash")):
        _add_signal(scores, signals, method, 45, "cryptographic integrity mismatch marker")
    errors = verification.get("errors")
    if isinstance(errors, list) and errors:
        typed_errors = [
            item.get("type")
            for item in errors
            if isinstance(item, dict) and item.get("type")
        ]
        if typed_errors:
            _add_signal(scores, signals, method, 25, "structured verification errors present", typed_errors[:10])
    error_count = _as_float(verification.get("error_count"))
    if error_count and error_count > 0:
        _add_signal(scores, signals, method, 20, "verification error_count is non-zero", int(error_count))


def classify_points_safe_mode_attack_detail(safe_mode: dict[str, Any] | None) -> dict[str, Any]:
    state = _as_dict(safe_mode)
    if not state.get("safe_mode"):
        return {
            "method": "none",
            "confidence": "none",
            "score": 0,
            "threshold": ATTACK_CLASSIFICATION_THRESHOLD,
            "model": ATTACK_CLASSIFICATION_MODEL,
            "matched_signals": [],
            "candidate_scores": {},
        }
    verification = _as_dict(state.get("verification"))
    restore_plan = _as_dict(state.get("restore_plan"))
    scores: dict[str, int] = {}
    signals: dict[str, list[dict[str, Any]]] = {}
    _score_clock_guard(state, verification, restore_plan, scores, signals)
    _score_replay_or_double_spend(state, verification, restore_plan, scores, signals)
    _score_branch_or_fork(state, verification, restore_plan, scores, signals)
    _score_integrity_tampering(state, verification, restore_plan, scores, signals)
    if not scores:
        method = "pointschain_safe_mode_write_block"
        score = 0
    else:
        method, score = max(scores.items(), key=lambda item: (item[1], item[0]))
        if score < ATTACK_CLASSIFICATION_THRESHOLD:
            method = "pointschain_safe_mode_write_block"
    confidence = "high" if score >= 100 else "medium" if score >= ATTACK_CLASSIFICATION_THRESHOLD else "low"
    return {
        "method": method,
        "confidence": confidence,
        "score": int(score),
        "threshold": ATTACK_CLASSIFICATION_THRESHOLD,
        "model": ATTACK_CLASSIFICATION_MODEL,
        "matched_signals": signals.get(method, []) if method != "pointschain_safe_mode_write_block" else [],
        "candidate_scores": dict(sorted(scores.items())),
    }


def classify_points_safe_mode_attack(safe_mode: dict[str, Any] | None) -> str:
    return str(classify_points_safe_mode_attack_detail(safe_mode).get("method") or "pointschain_safe_mode_write_block")


def points_safe_mode_security_context(
    safe_mode: dict[str, Any] | None,
    *,
    source: str,
    source_ip: str = "0.0.0.0",
    user_agent: str = "system-startup",
    blocked_action: str = "",
    error: BaseException | str | None = None,
    mode: str | None = None,
    actor: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = _as_dict(safe_mode)
    verification = _as_dict(state.get("verification"))
    restore_plan = _as_dict(state.get("restore_plan"))
    active = bool(state.get("safe_mode"))
    error_message = str(error) if error is not None else ""
    classification = classify_points_safe_mode_attack_detail(state)
    payload: dict[str, Any] = {
        "severity": "critical" if active else "warning",
        "attack_surface": "points_chain",
        "attack_method": classification["method"],
        "attack_classification": classification,
        "source": str(source or ""),
        "source_ip": str(source_ip or "0.0.0.0"),
        "client_ip": str(source_ip or "0.0.0.0"),
        "source_ip_note": "server startup/internal worker event; no direct client request IP" if str(source_ip or "") == "0.0.0.0" else "",
        "user_agent": str(user_agent or ""),
        "actor": _as_dict(actor),
        "mode": mode,
        "blocked_action": str(blocked_action or ""),
        "failure": {
            "type": error.__class__.__name__ if isinstance(error, BaseException) else ("str" if error is not None else ""),
            "message": error_message,
        },
        "safe_mode_active": active,
        "safe_mode_reason": state.get("reason") or "",
        "forensic_bundle_id": state.get("forensic_bundle_id") or "",
        "safe_mode": state,
        "verification": verification,
        "restore_plan": restore_plan,
        "ip_evidence": _collect_ip_evidence({"safe_mode": state, "error": error_message}),
        "operator_action_required": active,
        "operator_action": (
            "Run branch/governance recovery and verify the PointsChain before resuming ledger writes."
            if active
            else "Inspect bootstrap failure and retry after the root cause is fixed."
        ),
    }
    if extra:
        payload.update(dict(extra))
    return payload
