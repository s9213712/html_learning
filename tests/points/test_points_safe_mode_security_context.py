from services.points_chain.security_context import (
    ATTACK_CLASSIFICATION_THRESHOLD,
    classify_points_safe_mode_attack_detail,
    points_safe_mode_security_context,
)


def test_clock_safe_mode_classification_requires_structured_threshold_evidence():
    detail = classify_points_safe_mode_attack_detail({
        "safe_mode": True,
        "reason": "governance_clock_jump_detected",
        "verification": {
            "action": "governance_proposal_execute",
            "violation": "wall_clock_fast_forward",
            "wall_elapsed_seconds": 7200,
            "monotonic_elapsed_seconds": 10,
            "tolerance_seconds": 300,
            "guard_model": "wall_clock_vs_monotonic_v1",
        },
        "restore_plan": {"governance_recovery_required": True},
    })

    assert detail["method"] == "suspected_governance_clock_manipulation"
    assert detail["score"] >= ATTACK_CLASSIFICATION_THRESHOLD
    assert detail["confidence"] == "high"
    assert any(signal["evidence"] == "measured clock delta exceeded configured tolerance" for signal in detail["matched_signals"])


def test_safe_mode_classification_falls_back_when_attack_evidence_is_below_threshold():
    detail = classify_points_safe_mode_attack_detail({
        "safe_mode": True,
        "reason": "manual_operator_pause",
        "verification": {"note": "operator mentioned replay in an unrelated note"},
        "restore_plan": {},
    })

    assert detail["method"] == "pointschain_safe_mode_write_block"
    assert detail["score"] < ATTACK_CLASSIFICATION_THRESHOLD
    assert detail["confidence"] == "low"
    assert detail["matched_signals"] == []


def test_security_context_records_classification_threshold_and_ip_evidence():
    context = points_safe_mode_security_context(
        {
            "safe_mode": True,
            "reason": "governance_clock_jump_detected",
            "forensic_bundle_id": "fb-1",
            "verification": {
                "violation": "wall_clock_fast_forward",
                "wall_elapsed_seconds": 3600,
                "monotonic_elapsed_seconds": 1,
                "tolerance_seconds": 300,
                "guard_model": "wall_clock_vs_monotonic_v1",
                "observed_ip": "203.0.113.42",
            },
        },
        source="pytest",
        source_ip="0.0.0.0",
        blocked_action="ledger transaction",
    )

    assert context["attack_method"] == "suspected_governance_clock_manipulation"
    assert context["attack_classification"]["threshold"] == ATTACK_CLASSIFICATION_THRESHOLD
    assert context["forensic_bundle_id"] == "fb-1"
    assert any(item["value"] == "203.0.113.42" for item in context["ip_evidence"])
