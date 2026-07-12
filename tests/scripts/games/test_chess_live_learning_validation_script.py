import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "games" / "chess_live_learning_validation.py"


def _load_validation_module():
    spec = importlib.util.spec_from_file_location("chess_live_learning_validation_test_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_learning_validation_fast_retrain_flags_are_wired():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "--fast-retrain" in source
    assert "--allow-multi-engine" in source
    assert "--skip-autorun-benchmark" in source
    assert "--skip-autorun-promote" in source
    assert "--skip-retrain-benchmark-snapshots" in source
    assert "HTML_LEARNING_CHESS_AUTORUN_SKIP_BENCHMARK" in source
    assert "HTML_LEARNING_CHESS_AUTORUN_SKIP_PROMOTE" in source
    assert "skip_autorun_benchmark=skip_autorun_benchmark" in source
    assert "skip_autorun_promote=skip_autorun_promote" in source
    assert "skip_retrain_benchmark_snapshots=skip_retrain_benchmark_snapshots" in source
    assert "_skipped_benchmark_snapshot" in source
    assert '"autorun_skip_benchmark"' in source
    assert '"autorun_skip_promote"' in source
    assert '"retrain_benchmark_snapshots_skipped"' in source
    assert "--semantic-specialist-probes" in source
    assert "--kingside-development-audit" in source
    assert "_run_semantic_specialist_probes" in source
    assert "_run_kingside_development_audit" in source
    assert "kingside_development_audit" in source
    assert "BALANCED_PROMOTION_SEMANTIC_CLASSES" in source
    assert "STYLE_AUDIT_SEMANTIC_CLASSES" in source
    assert "balanced_clean_heldout_by_semantic" in source
    assert "development_multi_good_credit" in source
    assert "attacking_style_audit" in source
    assert "CENTRAL_FLANK_FOCUS_SEMANTICS" in source
    assert "central_flank_targeted_curriculum" in source
    assert "central_flank_failed_case_analysis" in source
    assert "FLANK_REPAIR_SEMANTIC" in source
    assert "flank_label_audit" in source
    assert "flank_difficulty_performance" in source
    assert "central_vs_flank_boundary" in source
    assert "FLANK_REASON_TAGS" in source
    assert "_flank_context_features" in source
    assert "_contextual_flank_performance_from_rows" in source
    assert "flank_only_contextual" in source
    assert "contextual_flank_pass_rate" in source
    assert "bad_random_flank_push" in source
    assert "_distill_quick_replay_rows" in source
    assert "distilled_replay_preprocessing" in source
    assert "distilled_replay.jsonl" in source
    assert "opening_target_margin_audit" in source
    assert "multi_good_tie_not_failure" in source
    assert "low_margin_override_rejected" in source
    assert "broad_strength_improvement" in source
    assert "mcts_visit_count" in source
    assert 'Path("/tmp") / f"chess_live_learning_validation_{stamp}_{os.getpid()}"' in source
    assert 'if not output_root.is_relative_to(Path("/tmp")):' in source
    assert 'if output_root.exists() or output_root.is_symlink():' in source
    assert 'output_root.mkdir(parents=True, exist_ok=False)' in source


def test_exp4_quick_retrain_sample_cap_scales_with_timeout():
    module = _load_validation_module()

    requested = 256
    assert module._exp4_quick_retrain_sample_cap(requested, 30)[0] == 128
    assert module._exp4_quick_retrain_sample_cap(requested, 75)[0] == 128
    assert module._exp4_quick_retrain_sample_cap(requested, 90)[0] == 192
    assert module._exp4_quick_retrain_sample_cap(requested, 180)[0] == 256

    capped, reason = module._exp4_quick_retrain_sample_cap(requested, 30)
    assert capped == 128
    assert "exp4 quick gate cap" in reason


def test_exp4_opening_target_margin_audit_reports_mcts_and_multigood(tmp_path):
    module = _load_validation_module()
    model_path = tmp_path / "chess_experiment_4_pv.json"
    fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    module.train_experiment_pv_from_replay_samples(
        [{"fen": fen, "side": "black", "move_uci": "e7e5", "target": 1.0, "weight": 1.0}],
        model_path=model_path,
    )

    audit = module._opening_target_margin_audit(
        engine_alias="exp4",
        model_path=model_path,
        deterministic_cases=[
            {
                "case_id": "opening_black_reply",
                "category": "opening",
                "fen": fen,
                "side": "black",
                "expected_best_moves": ["e7e5", "d7d5", "c7c5"],
            }
        ],
        deterministic_report={
            "score_table": [
                {"model_label": "baseline", "overall_deterministic_score": 0.8},
                {"model_label": "final", "overall_deterministic_score": 0.8},
            ]
        },
        checkpoints=[
            {
                "trusted_count": 20,
                "mistake_retention_probe": {
                    "matched_expected": True,
                    "result_kind": "matched_expected",
                },
            }
        ],
    )

    assert audit["supported"] is True
    assert audit["targeted_learning_success"] is True
    assert audit["broad_strength_improvement"] is False
    assert audit["cases"][0]["mcts_visit_count"] is not None
    assert "teacher_distribution" in audit["cases"][0]

    # exp4_07 evidence accounting cleanup
    assert audit["source"] == "exp4_07_opening_evidence_cleanup"
    assert audit["low_margin_override_counted_success_count"] == 0
    assert "override_applied_for_move_selection_count" in audit
    assert "override_counted_as_learning_success_count" in audit
    assert "opening_final_decision_alignment_passed" in audit
    assert "opening_specific_learning_evidence_passed" in audit
    assert "targeted_mistake_retention_success" in audit
    assert "opening_learning_evidence_passed" in audit
    assert "audit_table" in audit
    audit_row = audit["audit_table"][0]
    for field in (
        "teacher_top3",
        "static_best_move",
        "search_best_move",
        "final_top1",
        "raw_policy_rank",
        "margin_vs_second_best",
        "multi_good_tie",
        "override_applied_for_move_selection",
        "override_counted_as_learning_success",
        "low_margin_override_counted_as_learning_success",
        "failure_type",
    ):
        assert field in audit_row
    case_row = audit["cases"][0]
    assert case_row["low_margin_override_counted_as_learning_success"] is False


def test_exp4_07_promotion_gate_classifies_blockers_by_scope():
    module = _load_validation_module()

    classified = module._classify_promotion_blockers(
        [
            "exp4 opening final decision alignment failed or remains unresolved",
            "exp34 hard flank capability gap remains unresolved",
            "contextual flank hard clean pass rate is zero",
            "exp31 semantic scheduler: central_retention_zero_after_semantic_updates",
        ],
        {"opening_target_margin_audit": {"model_scope": "opening_specialist_candidate"}},
    )

    assert "exp31 semantic scheduler: central_retention_zero_after_semantic_updates" in classified["historical_cross_experiment_risk_references"]
    assert "exp4 opening final decision alignment failed or remains unresolved" in classified["current_exp4_measured_blockers"]
    # exp34 hard flank is measured this run (current) but excluded from opening-specialist scope
    assert "exp34 hard flank capability gap remains unresolved" in classified["current_exp4_measured_blockers"]
    assert "exp34 hard flank capability gap remains unresolved" in classified["general_model_promotion_blockers"]
    assert "exp34 hard flank capability gap remains unresolved" not in classified["opening_specialist_gate_blockers"]
    assert "contextual flank hard clean pass rate is zero" not in classified["opening_specialist_gate_blockers"]
    # historical reference must not block opening specialist
    assert "exp31 semantic scheduler: central_retention_zero_after_semantic_updates" not in classified["opening_specialist_gate_blockers"]


def test_exp4_08_e_pawn_equivalence_audit_classifies_multi_good_tie():
    module = _load_validation_module()
    # Build a synthetic summary that exercises the equivalence classifier.
    # central_vs_flank_boundary supplies the per-case data; templates provide
    # the FEN so the teacher annotation can run.
    e_pawn_cases = [
        {  # strict pass
            "case_id": "gate_e_pawn_easy_001",
            "expected_move": "e2e4",
            "expected_semantic": "e_pawn_central_break",
            "raw_policy_top1": "e2e4",
            "final_top1": "e2e4",
            "expected_rank": 1,
            "variant_difficulty": "easy",
        },
        {  # white e2e4 multi-good tie -> central_opening_equivalent_credit
            "case_id": "gate_e_pawn_medium_001",
            "expected_move": "e2e4",
            "expected_semantic": "e_pawn_central_break",
            "raw_policy_top1": "d2d4",
            "final_top1": "d2d4",
            "expected_rank": 2,
            "variant_difficulty": "medium",
        },
        {  # black e7e5 vs c7c5 -> opening_multi_good_tie
            "case_id": "gate_e_pawn_easy_002",
            "expected_move": "e7e5",
            "expected_semantic": "e_pawn_central_break",
            "raw_policy_top1": "c7c5",
            "final_top1": "c7c5",
            "expected_rank": 4,
            "variant_difficulty": "easy",
        },
        {  # exp4_18: black e7e5 vs d7d5 after quiet development is also a multi-good opening response
            "case_id": "gate_e_pawn_easy_003",
            "expected_move": "e7e5",
            "expected_semantic": "e_pawn_central_break",
            "raw_policy_top1": "d7d5",
            "final_top1": "d7d5",
            "expected_rank": 2,
            "variant_difficulty": "easy",
        },
    ]
    summary = {
        "checkpoint_consistency": {
            "checkpoint_consistency_table": [
                {
                    "trusted_count": 20,
                    "central_vs_flank_boundary": {"cases": e_pawn_cases},
                    "clean_heldout_by_semantic": {
                        "e_pawn_central_break": {
                            "passed": 1,
                            "total": 4,
                            "raw_policy_passed": 1,
                        }
                    },
                }
            ]
        }
    }
    diag = module._e_pawn_clean_held_out_diagnosis(summary)
    assert diag["supported"] is True
    cp = diag["per_checkpoint"][0]
    classifications = [case["classification"] for case in cp["cases"]]
    assert classifications[0] == "strict_pass"
    # remaining classifications should reflect multi-good tie credit
    assert "central_opening_equivalent_credit" in classifications
    assert "opening_multi_good_tie" in classifications
    # equivalent-credit pass rate counts strict + equivalent
    assert cp["e_pawn_equivalent_credit_pass_count"] == 4
    assert cp["true_e_pawn_raw_policy_fail_count"] == 0
    assert cp["true_e_pawn_final_decision_blocked_count"] == 0
    assert cp["excluded_multi_good_negative_count"] == 3
    # totals roll up to the aggregate dict
    assert diag["totals"]["e_pawn_equivalent_credit_pass_rate"] == 1.0


def test_exp4_08_e_pawn_diagnosis_runs_with_teacher_coverage_stats():
    """Real call covers the function body so a missing initialization (e.g. NameError) surfaces."""
    module = _load_validation_module()
    summary = {
        "checkpoint_consistency": {
            "checkpoint_consistency_table": [
                {
                    "trusted_count": 20,
                    "central_vs_flank_boundary": {
                        "cases": [
                            {
                                "case_id": "gate_e_pawn_easy_001",
                                "expected_move": "e2e4",
                                "expected_semantic": "e_pawn_central_break",
                                "raw_policy_top1": "e2e4",
                                "final_top1": "e2e4",
                                "expected_rank": 1,
                                "variant_difficulty": "easy",
                            }
                        ]
                    },
                    "clean_heldout_by_semantic": {
                        "e_pawn_central_break": {"passed": 1, "total": 1, "raw_policy_passed": 1}
                    },
                }
            ]
        }
    }
    diag = module._e_pawn_clean_held_out_diagnosis(summary)
    cp = diag["per_checkpoint"][0]
    assert "teacher_annotation_supported_count" in cp
    assert "teacher_annotation_missing_count" in cp
    assert "teacher_top3_empty_count" in cp
    assert "teacher_top5_empty_count" in cp
    assert cp["teacher_annotation_supported_count"] + cp["teacher_annotation_missing_count"] == cp["case_count"]


def test_exp4_08_c7c5_not_equivalent_without_teacher_or_margin_support():
    module = _load_validation_module()
    # Build a synthetic checkpoint row where the case carries no template
    # (so teacher annotation falls back to unsupported). This forces the
    # classifier into the "no positive evidence" branch.
    summary = {
        "checkpoint_consistency": {
            "checkpoint_consistency_table": [
                {
                    "trusted_count": 20,
                    "central_vs_flank_boundary": {
                        "cases": [
                            {
                                "case_id": "synthetic_no_template",
                                "expected_move": "e7e5",
                                "expected_semantic": "e_pawn_central_break",
                                "raw_policy_top1": "c7c5",
                                "final_top1": "c7c5",
                                "expected_rank": 6,
                                "variant_difficulty": "easy",
                            }
                        ]
                    },
                    "clean_heldout_by_semantic": {
                        "e_pawn_central_break": {"passed": 0, "total": 1, "raw_policy_passed": 0}
                    },
                }
            ]
        }
    }
    diag = module._e_pawn_clean_held_out_diagnosis(summary)
    case = diag["per_checkpoint"][0]["cases"][0]
    # No template -> no teacher coverage -> must NOT be classified as multi-good tie
    assert case["classification"] != "opening_multi_good_tie"
    assert case["classification"] in {"true_e_pawn_raw_policy_fail", "label_questionable", "undertrained_opening_pattern"}


def test_exp4_08_d2d4_c2c4_not_equivalent_without_teacher_or_margin_support():
    module = _load_validation_module()
    summary = {
        "checkpoint_consistency": {
            "checkpoint_consistency_table": [
                {
                    "trusted_count": 20,
                    "central_vs_flank_boundary": {
                        "cases": [
                            {
                                "case_id": "synthetic_no_template_white",
                                "expected_move": "e2e4",
                                "expected_semantic": "e_pawn_central_break",
                                "raw_policy_top1": "d2d4",
                                "final_top1": "d2d4",
                                "expected_rank": 2,
                                "variant_difficulty": "medium",
                            }
                        ]
                    },
                    "clean_heldout_by_semantic": {
                        "e_pawn_central_break": {"passed": 0, "total": 1, "raw_policy_passed": 0}
                    },
                }
            ]
        }
    }
    diag = module._e_pawn_clean_held_out_diagnosis(summary)
    case = diag["per_checkpoint"][0]["cases"][0]
    assert case["classification"] != "central_opening_equivalent_credit"
    assert case["classification"] in {"true_e_pawn_final_decision_blocked", "label_questionable", "undertrained_opening_pattern"}


def test_exp4_08_write_audit_artifacts_preserves_totals(tmp_path):
    module = _load_validation_module()
    summary = {
        "opening_target_margin_audit": {
            "supported": True,
            "audit_table": [{"case_id": "c1"}, {"case_id": "c2"}, {"case_id": "c3"}, {"case_id": "c4"}],
            "cases": [{"case_id": "c1"}, {"case_id": "c2"}, {"case_id": "c3"}, {"case_id": "c4"}],
        },
        "e_pawn_clean_held_out_diagnosis": {
            "supported": True,
            "totals": {"e_pawn_strict_pass_rate": 0.0, "e_pawn_equivalent_credit_pass_rate": 0.5},
            "per_checkpoint": [
                {"trusted_count": 10, "cases": [{"case_id": "x1"}, {"case_id": "x2"}]},
                {"trusted_count": 20, "cases": [{"case_id": "y1"}]},
            ],
        },
    }
    artifacts = module._write_audit_artifacts(tmp_path, summary)
    assert (tmp_path / "audits" / "opening_target_margin_audit.jsonl").exists()
    assert (tmp_path / "audits" / "e_pawn_clean_held_out_diagnosis.jsonl").exists()
    # totals + path retained, per-case lists trimmed
    assert summary["opening_target_margin_audit"]["audit_table"] == []
    assert summary["opening_target_margin_audit"]["audit_table_full_row_count"] == 4
    assert summary["opening_target_margin_audit"]["audit_table_artifact_path"].endswith(".jsonl")
    assert summary["opening_target_margin_audit"]["cases"] == []
    for checkpoint in summary["e_pawn_clean_held_out_diagnosis"]["per_checkpoint"]:
        assert checkpoint["cases"] == []
        assert isinstance(checkpoint["cases_full_count"], int)
    assert summary["e_pawn_clean_held_out_diagnosis"]["totals"]["e_pawn_equivalent_credit_pass_rate"] == 0.5
    assert artifacts["e_pawn_clean_held_out_diagnosis"]["row_count"] == 3


def test_exp4_08_classify_promotion_blockers_excuses_e_pawn_when_equivalent():
    module = _load_validation_module()
    classified = module._classify_promotion_blockers(
        [
            "checkpoint instability: trusted=10: semantic class e_pawn_central_break clean held-out pass count is zero",
            "exp4 opening final decision alignment failed or remains unresolved",
        ],
        {
            "opening_target_margin_audit": {"model_scope": "opening_specialist_candidate"},
            "e_pawn_clean_held_out_diagnosis": {
                "supported": True,
                "totals": {
                    "e_pawn_equivalent_credit_pass_rate": 0.5,
                    "central_opening_equivalent_pass_rate": 0.4,
                    "true_e_pawn_raw_policy_fail": 0,
                },
            },
        },
    )
    e_pawn_marker = "semantic class e_pawn_central_break clean held-out pass count is zero"
    assert not any(e_pawn_marker in reason for reason in classified["opening_specialist_gate_blockers"])
    assert any(e_pawn_marker in reason for reason in classified["general_model_promotion_blockers"])
    # alignment failure still blocks opening specialist
    assert any("opening final decision alignment" in reason for reason in classified["opening_specialist_gate_blockers"])


def test_exp4_09_label_audit_classifies_quarantine_and_clean():
    module = _load_validation_module()

    summary = {
        "e_pawn_clean_held_out_diagnosis": {
            "supported": True,
            "totals": {
                "true_e_pawn_raw_policy_fail": 2,
                "true_e_pawn_final_decision_blocked": 0,
            },
            "per_checkpoint": [
                {
                    "trusted_count": 20,
                    "cases": [
                        {
                            "case_id": "gate_e_pawn_hard_001",
                            "fen": "rnbqkbnr/ppp1pppp/8/3p4/2P5/5N2/PP1PPPPP/RNBQKB1R b KQkq - 1 2",
                            "side": "black",
                            "expected_move": "e7e5",
                            "raw_top1": "c7c5",
                            "raw_top3": ["c7c5", "g7g5", "h7h5"],
                            "final_top1": "c7c5",
                            "final_top3": [],
                            "expected_rank": 5,
                            "classification": "true_e_pawn_raw_policy_fail",
                        },
                        {
                            "case_id": "synth_clean_true_fail",
                            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                            "side": "white",
                            "expected_move": "e2e4",
                            "raw_top1": "h2h4",
                            "raw_top3": ["h2h4", "a2a4", "b2b4"],
                            "final_top1": "h2h4",
                            "final_top3": [],
                            "expected_rank": 7,
                            "classification": "true_e_pawn_raw_policy_fail",
                        },
                    ],
                }
            ],
        },
        "opening_target_margin_audit": {"supported": False, "cases": []},
    }
    audit = module._opening_label_audit(summary)
    rows = {row["case_id"]: row for row in audit["cases"]}
    # hard_001: e7e5 drops the d5 pawn -> teacher static_cp_delta is around -100
    # which falls into questionable_label (cp <= -50 and no teacher coverage).
    hard = rows["gate_e_pawn_hard_001"]
    assert hard["label_quality"] in {"questionable_label", "relabel_recommended", "quarantine_recommended"}
    assert hard["clean_true_failure"] is False
    # synth opening case: e2e4 is teacher-supported, should be clean and a true fail
    clean = rows["synth_clean_true_fail"]
    assert clean["label_quality"] == "clean_label"
    assert clean["clean_true_failure"] is True
    # totals
    assert audit["clean_true_failure_count"] >= 1
    # the questionable case must reduce true_raw_fail_count by 1
    assert audit["e_pawn_true_raw_policy_fail_count_clean"] == 1


def test_exp4_09_classify_label_quality_bands():
    module = _load_validation_module()
    # cp > -50 with teacher coverage -> clean
    assert module._classify_label_quality(-10, expected_in_teacher_top3=True, expected_in_teacher_top5=True) == "clean_label"
    # cp between -150 and -50 without teacher coverage -> questionable
    assert module._classify_label_quality(-100, expected_in_teacher_top3=False, expected_in_teacher_top5=False) == "questionable_label"
    # cp between -300 and -150 -> relabel recommended
    assert module._classify_label_quality(-200, expected_in_teacher_top3=False, expected_in_teacher_top5=False) == "relabel_recommended"
    # cp <= -300 -> quarantine
    assert module._classify_label_quality(-500, expected_in_teacher_top3=True, expected_in_teacher_top5=True) == "quarantine_recommended"
    # No cp data -> label_unverified unless teacher covers expected
    assert module._classify_label_quality(None, expected_in_teacher_top3=False, expected_in_teacher_top5=False) == "label_unverified"
    assert module._classify_label_quality(None, expected_in_teacher_top3=True, expected_in_teacher_top5=True) == "clean_label"


def test_exp4_10_replay_blunder_screen_flags_bishop_for_pawn_trade():
    module = _load_validation_module()
    # Synthetic exp3-style replay: 1.e4 e5 2.Bb5 c6 3.Bxc6 — the bishop trades
    # for a pawn and the screen should flag it.
    records = [
        {
            "replay_id": "synth_blunder_replay",
            "match_id": 1,
            "opening_seed": "standard_start",
            "move_history": [
                {"by": "white", "from": "e2", "to": "e4", "piece": "P", "captured": None, "promotion": None},
                {"by": "black", "from": "e7", "to": "e5", "piece": "p", "captured": None, "promotion": None},
                {"by": "white", "from": "f1", "to": "b5", "piece": "B", "captured": None, "promotion": None},
                {"by": "black", "from": "c7", "to": "c6", "piece": "p", "captured": None, "promotion": None},
                {"by": "white", "from": "b5", "to": "c6", "piece": "B", "captured": "p", "promotion": None},
                {"by": "black", "from": "b8", "to": "c6", "piece": "n", "captured": "B", "promotion": None},
            ],
            "confidence_score": 0.42,
        }
    ]
    screen = module._replay_blunder_screen(records)
    assert screen["flagged_replay_count"] == 1
    assert any(row["material_delta_cp"] <= -200 for row in screen["flagged"])


def test_exp4_10_replay_blunder_screen_passes_clean_replay():
    module = _load_validation_module()
    records = [
        {
            "replay_id": "clean_opening",
            "match_id": 2,
            "opening_seed": "standard_start",
            "move_history": [
                {"by": "white", "from": "e2", "to": "e4", "piece": "P", "captured": None, "promotion": None},
                {"by": "black", "from": "e7", "to": "e5", "piece": "p", "captured": None, "promotion": None},
                {"by": "white", "from": "g1", "to": "f3", "piece": "N", "captured": None, "promotion": None},
                {"by": "black", "from": "b8", "to": "c6", "piece": "n", "captured": None, "promotion": None},
            ],
            "confidence_score": 0.92,
        }
    ]
    screen = module._replay_blunder_screen(records)
    assert screen["flagged_replay_count"] == 0


def test_exp4_10_resignation_audit_flags_early_low_confidence_resign():
    module = _load_validation_module()
    records = [
        {
            "replay_id": "early_resign",
            "match_id": 1,
            "confidence_score": 0.42,
            "result": "black",
            "result_reason": "resign",
            "move_count": 8,
            "move_history": [{"by": "white"}] * 8,
            "winner_color": "black",
            "human_side": "black",
        },
        {
            "replay_id": "normal_loss",
            "match_id": 2,
            "confidence_score": 0.85,
            "result": "white",
            "result_reason": "checkmate",
            "move_count": 60,
            "move_history": [{"by": "white"}] * 60,
            "winner_color": "white",
            "human_side": "black",
        },
    ]
    audit = module._resignation_audit(records)
    assert audit["suspicious_count"] == 1
    assert audit["early_resign_count"] == 1
    assert audit["suspicious_replays"][0]["replay_id"] == "early_resign"


def test_exp4_11_low_confidence_trusted_audit_flags_low_conf():
    module = _load_validation_module()
    records = [
        {"replay_id": "r1", "match_id": 1, "collection_tier": "trusted", "confidence_score": 0.42},
        {"replay_id": "r2", "match_id": 2, "collection_tier": "trusted", "confidence_score": 0.92},
        {"replay_id": "r3", "match_id": 3, "collection_tier": "unverified", "confidence_score": 0.10},
    ]
    audit = module._low_confidence_trusted_audit(records)
    assert audit["flagged_replay_count"] == 1
    assert audit["flagged_replay_ids"] == ["r1"]


def test_exp4_11_misclassified_resign_audit_flags_resign_after_capture():
    module = _load_validation_module()
    records = [
        {
            "replay_id": "r_resign_with_capture",
            "match_id": 1,
            "result_reason": "resign",
            "winner_color": "black",
            "move_history": [
                {"by": "white", "captured": None},
                {"by": "black", "captured": None},
                {"by": "white", "captured": "n"},  # white capturing right before "resign"
            ],
        },
        {
            "replay_id": "r_genuine_resign",
            "match_id": 2,
            "result_reason": "resign",
            "winner_color": "white",
            "move_history": [
                {"by": "white", "captured": None},
                {"by": "black", "captured": None},
                {"by": "white", "captured": None},
            ],
        },
    ]
    audit = module._misclassified_resign_audit(records)
    assert audit["flagged_replay_count"] == 1
    assert audit["flagged_replay_ids"] == ["r_resign_with_capture"]


def test_exp4_11_build_replay_quarantine_index_merges_sources():
    module = _load_validation_module()
    summary = {
        "replay_blunder_screen": {
            "flagged": [
                {"replay_id": "r1", "ply": 5, "move_uci": "b5c6", "side": "white", "material_delta_cp": -230, "material_after_best_recapture_cp": -230},
                {"replay_id": "r1", "ply": 7, "move_uci": "g1h3", "side": "white", "material_delta_cp": -250, "material_after_best_recapture_cp": -300},
            ],
        },
        "low_confidence_trusted_audit": {
            "flagged": [{"replay_id": "r2", "match_id": 2, "confidence_score": 0.42, "collection_tier": "trusted"}],
        },
        "misclassified_resign_audit": {
            "flagged": [{"replay_id": "r3", "match_id": 3, "resigning_side": "white", "recent_captures_by_resigning_side": [{"captured_piece": "n"}]}],
        },
    }
    idx = module._build_replay_quarantine_index(summary)
    # r1 had 2 plies flagged -> escalated to replay-level
    assert "r1" in idx["quarantine_replay_ids"]
    assert "r2" in idx["quarantine_replay_ids"]
    assert "r3" in idx["quarantine_replay_ids"]
    # Ply-level still recorded for r1
    assert ["r1", 5] in idx["quarantine_ply_keys"]
    assert idx["by_source_count"]["replay_blunder_screen"] >= 2
    assert idx["by_source_count"]["low_confidence_trusted_audit"] == 1
    assert idx["by_source_count"]["misclassified_resign_audit"] == 1


def test_exp4_11_extract_engine_samples_respects_quarantine():
    module = _load_validation_module()
    records = [
        {
            "replay_id": "good",
            "match_id": 1,
            "human_side": "black",  # engine plays white
            "opening_seed": "standard_start",
            "move_history": [
                {"by": "white", "from": "e2", "to": "e4", "promotion": None},
                {"by": "black", "from": "e7", "to": "e5", "promotion": None},
            ],
        },
        {
            "replay_id": "bad",
            "match_id": 2,
            "human_side": "black",
            "opening_seed": "standard_start",
            "move_history": [
                {"by": "white", "from": "e2", "to": "e4", "promotion": None},
                {"by": "black", "from": "e7", "to": "e5", "promotion": None},
            ],
        },
    ]
    all_samples = module._extract_engine_move_samples_from_records(records)
    assert any(s["game_label"] == "bad" for s in all_samples)
    filtered = module._extract_engine_move_samples_from_records(
        records, quarantine_replay_ids={"bad"}
    )
    assert all(s["game_label"] != "bad" for s in filtered)
    assert any(s["game_label"] == "good" for s in filtered)


def test_exp4_12_should_resign_helper_invariants():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "chess_pv_t", ROOT / "services" / "games" / "chess_pv.py"
    )
    chess_pv = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = chess_pv
    spec.loader.exec_module(chess_pv)
    import chess
    board = chess.Board()
    # Early game: never resign
    r = chess_pv.should_resign(board, search_score=-2000, ply_count=10, mate_distance=None)
    assert r["should_resign"] is False
    assert r["resign_forbidden"] is True
    # Forced mate within distance: allow
    r = chess_pv.should_resign(board, search_score=-9_000_000, ply_count=50, mate_distance=3)
    assert r["should_resign"] is True
    assert r["resign_allowed"] is True
    # Drawable losing: keep playing
    r = chess_pv.should_resign(board, search_score=-700, ply_count=40, mate_distance=None)
    assert r["should_resign"] is False
    # Mate distance beyond threshold should NOT resign
    r = chess_pv.should_resign(board, search_score=-2000, ply_count=40, mate_distance=15)
    assert r["should_resign"] is False


def test_exp4_12_chess_label_features_emits_castling_and_promotion():
    module = _load_validation_module()
    # Castling-eligible position
    feats = module._chess_label_features(
        "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 4 5",
        "white",
        "e1g1",
    )
    assert feats["supported"] is True
    assert feats["is_castling_move"] is True
    assert feats["can_castle_kingside"] is True
    # Queen promotion
    feats = module._chess_label_features("8/4P3/8/8/8/8/8/4K2k w - - 0 1", "white", "e7e8q")
    assert feats["is_promotion"] is True
    assert feats["promotion_piece"] == "q"


def test_exp4_07_catastrophic_regression_source_marks_score_not_regressed():
    module = _load_validation_module()
    summary = {
        "deterministic_strength_snapshot": {
            "score_table": [
                {"model_label": "baseline", "overall_deterministic_score": 0.8693},
                {"model_label": "final", "overall_deterministic_score": 0.9231},
            ]
        },
        "checkpoint_consistency": {
            "instability": True,
            "instability_reasons": [
                "trusted=10: seen retention below threshold",
                "trusted=10: clean held-out retention below threshold",
                "trusted=10: hard-negative margin is negative",
                "trusted=10: prior learned case retention regressed",
            ],
        },
        "fusion_mode_comparison": {
            "modes": [{"fusion_mode": "balanced_fusion", "final_decision_generalization_rate": 0.41}]
        },
        "before_after_eval": {"checkpoints": []},
    }
    source = module._catastrophic_regression_source(
        summary=summary,
        illegal_move_delta=None,
        stage_regression={"stage_catastrophic_regression": False},
        retrain_stability={"suspected_catastrophic_regression": False},
        checkpoint_consistency=summary["checkpoint_consistency"],
        before_after=summary["before_after_eval"],
    )
    assert source["deterministic_score_regression"] is False
    assert source["deterministic_score_delta"] == 0.0538
    assert source["checkpoint_retention_instability"] is True
    assert source["clean_heldout_retention_failure"] is True
    assert source["seen_retention_failure"] is True
    assert source["semantic_margin_failure"] is True
    assert source["prior_retention_failure"] is True
    assert source["final_decision_generalization_failure"] is True


def test_semantic_specialist_training_rows_are_weighted():
    module = _load_validation_module()
    variant = {
        "case_id": "kingside_probe",
        "variant_id": "kingside_probe:train",
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "side": "white",
        "expected_move": "h2h4",
        "expected_semantic": "kingside_aggression",
        "semantic_class": "kingside_aggression",
        "variant_difficulty": "easy",
    }

    row = module._specialist_training_row(variant, weight=2.2)

    assert row["variant_split"] == "specialist_train"
    assert row["source"] == "semantic_specialist_replay"
    assert row["move_uci"] == "h2h4"
    assert row["expected_semantic"] == "kingside_aggression"
    assert row["hard_negatives"]
    sampling = module._apply_semantic_class_balanced_weights([row])
    assert sampling["enabled"] is True
    assert sampling["sample_weight_by_semantic"]["kingside_aggression"] > 0


def test_balanced_gate_excludes_style_and_credits_development_multigood():
    module = _load_validation_module()
    variants = [
        {
            "case_id": "dev_case",
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "side": "black",
            "expected_move": "g8f6",
            "expected_semantic": "development_move",
            "semantic_class": "development_move",
            "variant_split": "held_out",
            "variant_difficulty": "medium",
        },
        {
            "case_id": "style_case",
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "side": "white",
            "expected_move": "h2h4",
            "expected_semantic": "kingside_aggression",
            "semantic_class": "kingside_aggression",
            "variant_split": "held_out",
            "variant_difficulty": "easy",
        },
    ]
    final_rows = [
        {"case_id": "dev_case", "top1": "e7e5", "top3": ["e7e5", "d7d5", "g8f6"], "expected_is_top1": False},
        {"case_id": "style_case", "top1": "e2e4", "top3": ["e2e4"], "expected_is_top1": False},
    ]
    raw_rows = [
        {"case_id": "dev_case", "raw_policy_top1": "e7e5", "raw_policy_top3": ["e7e5", "d7d5", "g8f6"], "expected_is_raw_top1": False},
        {"case_id": "style_case", "raw_policy_top1": "e2e4", "raw_policy_top3": ["e2e4"], "expected_is_raw_top1": False},
    ]

    result = module._balanced_gate_performance_for_case_ids(
        variants,
        final_rows,
        raw_rows,
        {"dev_case", "style_case"},
        label_quality={
            "cases": [
                {"case_id": "dev_case", "static_cp_delta": -20},
                {"case_id": "style_case", "static_cp_delta": -20},
            ]
        },
    )

    assert result["count"] == 1
    assert result["balanced_hits"] == 1
    assert result["balanced_pass_rate"] == 1.0
    assert result["cases"][0]["case_id"] == "dev_case"
    assert result["cases"][0]["multi_good_credit_applied"] is True
    assert result["cases"][0]["multi_good_credit_reason"] == "expected_in_final_top3"


def test_compact_sanity_probe_preserves_balanced_gate_evidence():
    module = _load_validation_module()

    compact = module._compact_sanity_probe_for_summary(
        {
            "balanced_gate_semantic_set": ["development_move"],
            "excluded_style_semantics": ["kingside_aggression"],
            "balanced_clean_held_out_count": 9,
            "balanced_clean_held_out_pass_rate": 0.6667,
            "balanced_clean_heldout_by_semantic": {"development_move": {"passed": 6, "total": 9}},
            "development_multi_good_credit": {"multi_good_credit_applied_count": 4},
            "attacking_style_audit": {"case_count": 9, "strict_final_pass_rate": 0.0},
        }
    )

    assert compact["balanced_gate_semantic_set"] == ["development_move"]
    assert compact["excluded_style_semantics"] == ["kingside_aggression"]
    assert compact["balanced_clean_held_out_count"] == 9
    assert compact["balanced_clean_held_out_pass_rate"] == 0.6667
    assert compact["balanced_clean_heldout_by_semantic"]["development_move"]["passed"] == 6
    assert compact["development_multi_good_credit"]["multi_good_credit_applied_count"] == 4
    assert compact["attacking_style_audit"]["case_count"] == 9


def test_exp26_central_flank_curriculum_and_semantic_negatives_are_wired():
    module = _load_validation_module()

    variants = module._central_flank_targeted_supervised_variants(
        split="train",
        offsets=(2,),
    )
    semantics = {row["semantic_class"] for row in variants}

    assert {"e_pawn_central_break", "d_pawn_central_break", "flank_pawn_push"} <= semantics
    assert "development_move" not in semantics
    assert "kingside_aggression" not in semantics
    assert all(row["curriculum_focus"] == "central_flank_semantic_improvement" for row in variants)

    negatives = module._semantic_negative_moves(module.chess.STARTING_FEN, "white", "e2e4", limit=3)
    negative_semantics = [
        module._move_semantic_class(module.chess.STARTING_FEN, "white", move)
        for move in negatives
    ]

    assert negative_semantics[0] == "d_pawn_central_break"
    assert "flank_pawn_push" in negative_semantics
    assert "kingside_aggression" not in negative_semantics[:2]


def test_compact_checkpoint_preserves_central_flank_curriculum_summary():
    module = _load_validation_module()

    compact = module._compact_checkpoint_for_summary(
        {
            "sanity_variant_training": {
                "rows": [{"variant_id": "row"}],
                "central_flank_targeted_curriculum": {
                    "enabled": True,
                    "train_rows_added": 27,
                    "focus_semantics": ["e_pawn_central_break", "d_pawn_central_break", "flank_pawn_push"],
                },
            }
        }
    )

    training = compact["sanity_variant_training"]
    assert training["rows"] == []
    assert training["central_flank_targeted_curriculum"]["enabled"] is True
    assert training["central_flank_targeted_curriculum"]["train_rows_added"] == 27


def test_exp27_flank_hard_coverage_and_label_audit_are_clean():
    module = _load_validation_module()
    curriculum = module._sanity_curriculum_variants(
        {
            "case_id": "probe",
            "fen": module.chess.STARTING_FEN,
            "side": "white",
            "expected_move": "e2e4",
            "expected_semantic": "e_pawn_central_break",
        },
        trusted_replays=20,
    )
    pool = curriculum["held_out_pool"]
    audit = module._flank_label_audit_for_cases(pool["all_cases"], pool["label_quality"])
    hard_flank = [
        row for row in pool["clean_gate_cases"]
        if row["semantic_class"] == "flank_pawn_push" and row["difficulty"] == "hard"
    ]

    assert "flank_pawn_push:hard" not in pool["semantic_coverage_missing"]
    assert len(hard_flank) >= module.SEMANTIC_BALANCED_GATE_MIN_PER_DIFFICULTY
    assert audit["questionable_count"] == 0
    assert audit["invalid_count"] == 0
    assert audit["by_difficulty"]["hard"]["clean"] >= module.SEMANTIC_BALANCED_GATE_MIN_PER_DIFFICULTY


def test_exp27_flank_performance_and_boundary_report_shapes():
    module = _load_validation_module()
    variants = [
        {
            "case_id": "flank_easy",
            "fen": module.chess.STARTING_FEN,
            "side": "white",
            "expected_move": "c2c4",
            "expected_semantic": "flank_pawn_push",
            "semantic_class": "flank_pawn_push",
            "variant_split": "held_out",
            "variant_difficulty": "easy",
        },
        {
            "case_id": "e_case",
            "fen": module.chess.STARTING_FEN,
            "side": "white",
            "expected_move": "e2e4",
            "expected_semantic": "e_pawn_central_break",
            "semantic_class": "e_pawn_central_break",
            "variant_split": "held_out",
            "variant_difficulty": "easy",
        },
    ]
    final_rows = [
        {"case_id": "flank_easy", "top1": "e2e4", "top3": ["e2e4"], "expected_is_top1": False},
        {"case_id": "e_case", "top1": "c2c4", "top3": ["c2c4"], "expected_is_top1": False},
    ]
    raw_rows = [
        {"case_id": "flank_easy", "raw_policy_top1": "e2e4", "raw_policy_top3": ["e2e4"], "expected_is_raw_top1": False},
        {"case_id": "e_case", "raw_policy_top1": "c2c4", "raw_policy_top3": ["c2c4"], "expected_is_raw_top1": False},
    ]
    ids = {"flank_easy", "e_case"}

    flank_perf = module._flank_difficulty_performance(variants, final_rows, raw_rows, ids)
    boundary = module._central_vs_flank_boundary_report(
        module._semantic_confusion_for_case_ids(variants, final_rows, raw_rows, ids)
    )

    assert flank_perf["by_difficulty"]["easy"]["total"] == 1
    assert flank_perf["hard_coverage_complete"] is False
    assert boundary["flank_to_central_confusion"] == 1
    assert boundary["central_to_flank_confusion"] == 1


def test_exp28_flank_context_features_and_reason_tags_are_wired():
    module = _load_validation_module()
    fen = module.chess.STARTING_FEN

    features = module._flank_context_features(fen, "white")
    reason = module._flank_reason_tag_for_move(fen, "white", "c2c4")
    bad_reason = module._flank_reason_tag_for_move(fen, "white", "e2e4")

    assert features["supported"] is True
    assert "open_closed_center" in features
    assert "wing_space_advantage" in features
    assert "central_tension" in features
    assert "attack_lane_availability" in features
    assert reason in module.FLANK_REASON_TAGS
    assert bad_reason == "bad_random_flank_push"


def test_exp28_contextual_flank_performance_reports_bad_random_confusion():
    module = _load_validation_module()
    variants = [
        {
            "case_id": "flank_context",
            "fen": module.chess.STARTING_FEN,
            "side": "white",
            "expected_move": "c2c4",
            "expected_semantic": "flank_pawn_push",
            "semantic_class": "flank_pawn_push",
            "variant_difficulty": "hard",
            "flank_reason_tag": module._flank_reason_tag_for_move(module.chess.STARTING_FEN, "white", "c2c4"),
            "flank_context_features": module._flank_context_features(module.chess.STARTING_FEN, "white"),
        }
    ]
    rows = [
        {
            "case_id": "flank_context",
            "expected_move": "c2c4",
            "final_top1": "e2e4",
            "raw_policy_top1": "e2e4",
            "final_pass": False,
            "raw_policy_pass": False,
        }
    ]

    result = module._contextual_flank_performance_from_rows(variants, rows, label="unit")

    assert result["count"] == 1
    assert result["hard_clean_count"] == 1
    assert result["contextual_flank_pass_rate"] == 0.0
    assert result["non_flank_move_confusion"]["count"] == 1
    assert result["bad_random_flank_push_confusion"]["promoted_count"] == 0
    assert result["flank_reason_distribution"][variants[0]["flank_reason_tag"]] == 1
    assert result["context_feature_importance"]


def test_exp28_flank_label_audit_v2_excludes_bad_random_from_balanced_gate():
    module = _load_validation_module()
    cases = [
        {
            "case_id": "bad_flank",
            "fen": module.chess.STARTING_FEN,
            "side": "white",
            "expected_move": "e2e4",
            "expected_semantic": "flank_pawn_push",
            "semantic_class": "flank_pawn_push",
            "variant_difficulty": "easy",
            "difficulty": "easy",
            "label_quality": "clean",
        }
    ]

    audit = module._flank_label_audit_for_cases(
        cases,
        {"cases": [{"case_id": "bad_flank", "label_quality": "clean", "static_cp_delta": -10}]},
    )

    assert audit["source"] == "exp28_flank_label_audit_v2"
    assert audit["bad_random_flank_push_count"] == 1
    assert audit["flank_reason_distribution"]["bad_random_flank_push"] == 1
    assert audit["cases"][0]["balanced_gate_eligible"] is False


def test_exp28_5_distilled_replay_preprocessing_dedupes_and_annotates(tmp_path):
    module = _load_validation_module()
    rows = [
        {
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "side": "black",
            "move_uci": "e7e5",
            "source_game_id": 1,
            "source_move_index": 1,
            "replay_id": "dup_a",
            "weight": 1.0,
        },
        {
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "side": "black",
            "move_uci": "e7e5",
            "source_game_id": 2,
            "source_move_index": 1,
            "replay_id": "dup_b",
            "weight": 1.0,
        },
        {
            "fen": module.chess.STARTING_FEN,
            "side": "white",
            "move_uci": "a2a3",
            "source_game_id": 3,
            "source_move_index": 1,
            "replay_id": "flank",
            "weight": 1.0,
        },
        {
            "fen": module.chess.STARTING_FEN,
            "side": "white",
            "move_uci": "h2h4",
            "source_game_id": 4,
            "source_move_index": 1,
            "replay_id": "style",
            "weight": 1.0,
        },
    ]

    distilled, report = module._distill_quick_replay_rows(rows, checkpoint_dir=tmp_path, held_out_cases=[])

    assert report["source"] == "exp28_5_distilled_replay_preprocessing"
    assert report["original_rows"] == 4
    assert report["distilled_rows"] == 2
    assert report["compression_ratio"] == 0.5
    assert report["duplicate_ratio_before"] > report["duplicate_ratio_after"]
    assert report["style_audit_rows_excluded"] == 1
    assert report["held_out_in_training"] is False
    assert (tmp_path / "distilled_replay.jsonl").exists()
    assert {row["semantic_class"] for row in distilled} == {"e_pawn_central_break", "flank_pawn_push"}
    flank = next(row for row in distilled if row["semantic_class"] == "flank_pawn_push")
    assert flank["teacher_top3"]
    assert flank["teacher_top5"]
    assert flank["hard_negatives"]
    assert flank["reason_tag"] in module.FLANK_REASON_TAGS
    assert flank["flank_context_features"]["supported"] is True
    assert len(flank["flank_context_feature_vector"]) == 8
    assert flank["flank_context_feature_injection"] is True
    assert sorted(next(row for row in distilled if row["move_uci"] == "e7e5")["source_game_ids"]) == ["1", "2"]


def test_exp28_5_distilled_replay_reports_heldout_leakage(tmp_path):
    module = _load_validation_module()
    row = {
        "fen": module.chess.STARTING_FEN,
        "side": "white",
        "move_uci": "c2c4",
        "source_game_id": 1,
        "source_move_index": 1,
        "replay_id": "leak",
    }
    held_out = [
        {
            "fen": module.chess.STARTING_FEN,
            "expected_move": "c2c4",
            "normalized_fen_hash": module._normalized_fen_hash(module.chess.STARTING_FEN),
        }
    ]

    distilled, report = module._distill_quick_replay_rows([row], checkpoint_dir=tmp_path, held_out_cases=held_out)

    assert distilled == []
    assert report["pre_filter_overlap_count"] == 1
    assert report["blocked_leakage_candidate_count"] == 1
    assert report["leakage_detected"] is False
    assert report["leakage_count"] == 0
    assert report["held_out_in_training"] is False


def test_exp30a_evaluation_cache_key_contains_required_fields(tmp_path):
    module = _load_validation_module()
    model_path = tmp_path / "model.json"
    model_path.write_text('{"policy": {"e2e4": 1.0}}', encoding="utf-8")
    cases = [
        {
            "case_id": "cache_case",
            "fen": module.chess.STARTING_FEN,
            "side": "white",
            "expected_move": "e2e4",
            "semantic_class": "e_pawn_central_break",
            "difficulty": "easy",
        }
    ]

    result, cache = module._cached_position_set_performance(
        engine_alias="exp3",
        model_path=model_path,
        cases=cases,
        label="cache test",
        cache_dir=tmp_path / "cache",
    )
    cached_result, cached = module._cached_position_set_performance(
        engine_alias="exp3",
        model_path=model_path,
        cases=cases,
        label="cache test",
        cache_dir=tmp_path / "cache",
    )

    assert result["cache_key"]["model_hash"]
    assert result["cache_key"]["case_set_hash"]
    assert result["cache_key"]["evaluator_config_hash"]
    assert result["cache_key"]["decision_mode"] == "balanced_fusion"
    assert result["cache_key"]["style_profile"] == "balanced"
    assert result["cache_key"]["semantic_gate_version"]
    assert cache["cache_hit"] is False
    assert cached["cache_hit"] is True
    assert cached_result["cache_key"] == result["cache_key"]


def test_exp30a_smoke_gate_failed_probe_skips_full_gate():
    module = _load_validation_module()
    smoke = {
        "passed": False,
        "reasons": ["distilled_replay_heldout_leakage"],
        "case_count": 8,
        "final_pass_rate": 0.25,
        "by_semantic": {"e_pawn_central_break": {"total": 2, "passed": 1, "pass_rate": 0.5}},
    }

    probe = module._skipped_sanity_learning_probe_from_smoke(smoke)

    assert probe["result_kind"] == "smoke_gate_failed_full_gate_skipped"
    assert probe["full_gate_skipped"] is True
    assert "distilled_replay_heldout_leakage" in probe["full_gate_skip_reason"]
    assert probe["final_decision_learning"]["learning_signal"] is False


def test_exp30b_semantic_routing_weights_are_scoped():
    module = _load_validation_module()
    central = {
        "case_id": "central",
        "fen": module.chess.STARTING_FEN,
        "side": "white",
        "expected_move": "e2e4",
        "semantic_class": "e_pawn_central_break",
    }
    flank = {
        "case_id": "flank",
        "fen": module.chess.STARTING_FEN,
        "side": "white",
        "expected_move": "c2c4",
        "semantic_class": "flank_pawn_push",
        "flank_context_features": module._flank_context_features(module.chess.STARTING_FEN, "white"),
    }

    central_weights = module._semantic_routing_weights_for_case(central)
    flank_weights = module._semantic_routing_weights_for_case(flank)

    assert central_weights["central_head"] > central_weights["flank_head"]
    assert flank_weights["flank_head"] > flank_weights["central_head"]
    assert round(sum(central_weights.values()), 4) == 1.0
    assert round(sum(flank_weights.values()), 4) == 1.0


def test_exp29_flank_context_feature_vector_and_trainer_auxiliary(tmp_path):
    module = _load_validation_module()
    from services.games import chess_dl

    context = module._flank_context_features(module.chess.STARTING_FEN, "white")
    vector = module._flank_context_feature_vector(context)
    sample = {
        "fen": module.chess.STARTING_FEN,
        "side": "white",
        "move_uci": "c2c4",
        "target": 1.0,
        "weight": 2.0,
        "expected_semantic": "flank_pawn_push",
        "semantic_class": "flank_pawn_push",
        "flank_context_features": context,
        "flank_context_feature_vector": vector,
        "flank_reason_tag": module._flank_reason_tag_for_move(module.chess.STARTING_FEN, "white", "c2c4"),
        "hard_negatives": ["e2e4", "d2d4", "g1f3", "a2a4"],
    }

    result = chess_dl.train_experiment_dl_from_replay_samples(
        [sample],
        model_path=tmp_path / "exp3_model.json",
        replay_path=tmp_path / "exp3_replay.jsonl",
        replace_replay=True,
    )

    assert len(vector) == 8
    assert result["ok"] is True
    assert result["training_objective"] == "contrastive_policy_ranking_with_flank_context_auxiliary_semantic_adapters_budget_scheduler"
    assert result["auxiliary_objectives"]["flank_context_classification_loss"] is True
    assert result["auxiliary_objectives"]["semantic_specific_adapter_loss"] is True
    assert result["auxiliary_objectives"]["retention_aware_update_scheduler"] is True
    assert result["semantic_loss_budget_scheduler"] is True
    assert result["flank_context_feature_vector_used"] is True
    assert result["flank_context_classification_updates"] > 0
    assert result["flank_reason_tag_updates"] > 0
    assert result["flank_vs_nonflank_margin_updates"] > 0
    assert result["semantic_specific_adapters"] is True
    assert result["semantic_head_update_count"]["flank_head"] > 0
    assert result["loss_budget_by_semantic"]["flank_pawn_push"] > 0
    assert result["consumed_budget_by_semantic"]["flank_pawn_push"] > 0
    assert result["update_schedule_trace"]
    assert result["anchor_check_after_each_semantic"]
    assert result["gradient_conflict_matrix"]


def test_exp31_semantic_loss_budget_scheduler_report_blocks_interference():
    module = _load_validation_module()
    semantic_interference = {
        "pass_rate_delta_by_semantic": {
            "e_pawn_central_break": -0.5,
            "d_pawn_central_break": -0.5,
            "flank_pawn_push": 0.5,
            "development_move": 0.0,
        },
        "after_pass_rate_by_semantic": {
            "e_pawn_central_break": {"pass_rate": 0.0},
            "d_pawn_central_break": {"pass_rate": 0.0},
            "flank_pawn_push": {"pass_rate": 0.5},
            "development_move": {"pass_rate": 1.0},
        },
        "central_retention": {"e_pawn": 0.0, "d_pawn": 0.0, "min_delta": -0.5},
        "flank_retention": {"pass_rate": 0.5, "delta": 0.5},
        "development_retention": {"pass_rate": 1.0, "delta": 0.0},
    }
    train_result = {
        "loss_budget_by_semantic": {"e_pawn_central_break": 10, "d_pawn_central_break": 10, "flank_pawn_push": 10, "development_move": 10},
        "consumed_budget_by_semantic": {"e_pawn_central_break": 4, "d_pawn_central_break": 4, "flank_pawn_push": 20, "development_move": 4},
        "effective_gradient_norm_by_semantic": {"flank_pawn_push": 12.0},
        "update_count_by_semantic": {"flank_pawn_push": 20},
        "margin_delta_by_semantic": {"flank_pawn_push": 0.8, "d_pawn_central_break": -0.2},
        "semantic_loss_budget_skew": True,
        "update_schedule_trace": [{"semantic": "flank_pawn_push"}],
        "anchor_check_after_each_semantic": [{"after_semantic_update": "flank_pawn_push"}],
        "retention_delta_after_update": [{"semantic": "d_pawn_central_break", "margin_delta_proxy": -0.2}],
        "rollback_applied": True,
        "rollback_reason": "semantic_loss_budget_skew_detected_weight_dampening_applied",
        "dampened_semantic": "flank_pawn_push",
        "adjusted_loss_weight": {"flank_pawn_push": 0.55},
        "gradient_conflict_matrix": {"d_pawn_central_break": {"flank_pawn_push": -0.2}},
        "negative_cosine_like_conflict_count": 1,
        "conflict_pair_examples": [{"semantic_pair": ["d_pawn_central_break", "flank_pawn_push"]}],
    }

    report = module._semantic_loss_budget_scheduler_report(
        semantic_interference=semantic_interference,
        train_result=train_result,
        mistake_retention_probe={"learning_signal": True, "matched_expected": True, "result_kind": "matched_expected"},
    )

    assert report["semantic_interference"] is True
    assert report["catastrophic_semantic_interference"] is True
    assert report["rollback_applied"] is True
    assert "semantic_loss_budget_skew" in report["interference_reasons"]
    assert report["central_retention_after_flank_updates"]["e_pawn"] == 0.0


def test_exp32_smoke_gate_cases_are_stratified():
    module = _load_validation_module()

    cases = module._exp30a_smoke_gate_cases()
    by_semantic = {}
    for case in cases:
        by_semantic.setdefault(case["semantic_class"], []).append(case)

    for semantic in ["e_pawn_central_break", "d_pawn_central_break", "flank_pawn_push", "development_move"]:
        rows = by_semantic[semantic]
        difficulties = {row["difficulty"] for row in rows}
        assert len(rows) == 2
        assert difficulties & {"easy", "medium"}
        assert "hard" in difficulties


def test_exp32_development_multi_good_credit_can_lift_smoke_pass(monkeypatch, tmp_path):
    module = _load_validation_module()
    case = {
        "case_id": "dev_smoke",
        "fen": module.chess.STARTING_FEN,
        "side": "white",
        "expected_move": "g1f3",
        "semantic_class": "development_move",
        "difficulty": "easy",
        "label_quality_detail": {"static_cp_delta": -20},
    }

    monkeypatch.setattr(
        module,
        "_evaluate_sanity_learning_position_batch",
        lambda *args, **kwargs: [{"case_id": "dev_smoke", "top1": "b1c3", "top3": ["b1c3", "g1f3"], "expected_is_top1": False}],
    )
    monkeypatch.setattr(
        module,
        "_evaluate_raw_policy_position_batch",
        lambda *args, **kwargs: [{"case_id": "dev_smoke", "raw_policy_top1": "b1c3", "raw_policy_top3": ["b1c3", "g1f3"], "expected_rank": 2, "expected_is_raw_top1": False}],
    )

    without_credit = module._position_set_performance(
        engine_alias="exp3",
        model_path=tmp_path / "model.json",
        cases=[case],
        label="unit",
        use_development_multi_good_credit=False,
    )
    with_credit = module._position_set_performance(
        engine_alias="exp3",
        model_path=tmp_path / "model.json",
        cases=[case],
        label="unit",
        use_development_multi_good_credit=True,
    )

    assert without_credit["final_pass_rate"] == 0.0
    assert with_credit["final_pass_rate"] == 1.0
    assert with_credit["development_smoke_after_credit"]["passed"] == 1


def test_exp33_safe_checkpoint_selection_falls_back_to_cp10(tmp_path):
    module = _load_validation_module()
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}")
    checkpoints = [
        {
            "trusted_count": 10,
            "candidate_model_path": str(tmp_path / "cp10.json"),
            "new_model_hash": "cp10hash",
            "mistake_retention_probe": {
                "learning_signal": True,
                "matched_expected": True,
                "result_kind": "matched_expected",
            },
        },
        {
            "trusted_count": 20,
            "candidate_model_path": str(tmp_path / "cp20.json"),
            "new_model_hash": "cp20hash",
            "mistake_retention_probe": {
                "learning_signal": False,
                "matched_expected": False,
                "result_kind": "repeated_old_mistake",
                "before_move": "f8a3",
                "after_move": "f8a3",
                "expected_move": "a7a5",
            },
        },
    ]

    selected = module._safe_checkpoint_selection(checkpoints, baseline_model_path=baseline)

    assert selected["selected_safe_checkpoint"] == "cp10_fallback"
    assert selected["cp20_retention_pass"] is False
    assert selected["cp10_retention_pass"] is True
    assert selected["selected_model_hash"] == "cp10hash"


def test_exp33_e_pawn_dampening_audit_marks_overapplied():
    module = _load_validation_module()
    audit = module._exp33_e_pawn_dampening_audit(
        {
            "update_count_by_semantic": {
                "e_pawn_central_break": 10,
                "d_pawn_central_break": 100,
                "flank_pawn_push": 100,
            },
            "consumed_budget_by_semantic": {"e_pawn_central_break": 1.0},
            "effective_gradient_norm_by_semantic": {"e_pawn_central_break": 0.1},
            "margin_delta_by_semantic": {"e_pawn_central_break": -0.2},
            "dampened_semantic": "e_pawn_central_break",
            "rollback_reason": "central retention guard",
        }
    )

    assert audit["possibly_overapplied"] is True
    assert audit["dampening_applied_steps"] == 10


def test_exp34_retention_case_version_audit_detects_expected_conflict():
    module = _load_validation_module()
    checkpoints = [
        {
            "trusted_count": 10,
            "mistake_retention_probe": {
                "probe_case_id": "game:7:ply:3:e2e4",
                "fen": module.chess.STARTING_FEN,
                "before_move": "f8a3",
                "expected_move": "a5a4",
                "result_kind": "matched_expected",
            },
        },
        {
            "trusted_count": 20,
            "mistake_retention_probe": {
                "probe_case_id": "game:7:ply:3:e2e4",
                "fen": module.chess.STARTING_FEN,
                "before_move": "f8a3",
                "expected_move": "a7a5",
                "result_kind": "repeated_old_mistake",
            },
        },
    ]

    audit = module._exp34_retention_case_version_audit(checkpoints)

    assert audit["retention_label_version_conflict"] is True
    assert audit["conflicts"][0]["expected_moves"] == ["a5a4", "a7a5"]


def test_exp34_smoke_level_report_separates_foundation_and_hard_failures():
    module = _load_validation_module()
    checkpoints = [
        {
            "trusted_count": 10,
            "mistake_retention_probe": {"learning_signal": True, "matched_expected": True},
            "incremental_gate": {
                "smoke_gate": {
                    "leakage_check": {"held_out_in_training": False, "leakage_detected": False},
                    "smoke_anchor_audit": {
                        "cases": [
                            {"semantic_class": "e_pawn_central_break", "difficulty": "easy", "final_pass": True},
                            {"semantic_class": "flank_pawn_push", "difficulty": "easy", "final_pass": True},
                            {"semantic_class": "development_move", "difficulty": "easy", "final_pass": True},
                            {"semantic_class": "e_pawn_central_break", "difficulty": "hard", "final_pass": False},
                            {"semantic_class": "flank_pawn_push", "difficulty": "hard", "final_pass": False},
                        ]
                    },
                }
            },
        },
        {
            "trusted_count": 20,
            "mistake_retention_probe": {"learning_signal": False, "matched_expected": False},
            "incremental_gate": {
                "smoke_gate": {
                    "leakage_check": {"held_out_in_training": False, "leakage_detected": False},
                    "smoke_anchor_audit": {
                        "cases": [
                            {"semantic_class": "e_pawn_central_break", "difficulty": "easy", "final_pass": False},
                            {"semantic_class": "flank_pawn_push", "difficulty": "easy", "final_pass": False},
                            {"semantic_class": "development_move", "difficulty": "easy", "final_pass": True},
                        ]
                    },
                }
            },
        },
    ]

    report = module._exp34_smoke_level_report(
        checkpoints,
        retention_audit={"retention_label_version_conflict": False},
        safe_checkpoint_selection={"selected_safe_checkpoint": "cp10_fallback"},
    )

    assert report[0]["smoke_level_1_passed"] is True
    assert report[0]["smoke_level_2_passed"] is False
    assert report[0]["failure_classification"] == "hard_generalization_fail"
    assert report[1]["smoke_level_1_passed"] is False
    assert report[1]["failure_classification"] == "foundation_fail"


def test_exp34_hard_flank_audit_quarantines_questionable_label():
    module = _load_validation_module()
    checkpoints = [
        {
            "trusted_count": 20,
            "exp33_failed_anchor_isolated_probes": {
                "cases": [
                    {
                        "case_id": "gate_flank_hard_001",
                        "isolated_exact_pass": False,
                        "isolated_variant_pass_rate": 0.25,
                    }
                ]
            },
            "incremental_gate": {
                "smoke_gate": {
                    "smoke_anchor_audit": {
                        "cases": [
                            {
                                "case_id": "gate_flank_hard_001",
                                "semantic_class": "flank_pawn_push",
                                "difficulty": "hard",
                                "expected_move": "h7h5",
                                "expected_rank": 9,
                                "final_top3": ["e7e5", "g8f6", "d7d5"],
                                "static_cp_delta": -220,
                                "decision_breakdown": {"reason": "static_eval_rejected_expected"},
                            }
                        ]
                    }
                }
            },
        }
    ]

    hard_e, hard_flank = module._exp34_hard_case_decision_audits(checkpoints)

    assert hard_e == []
    assert hard_flank[0]["questionable_hard_flank_label"] is True
    assert hard_flank[0]["hard_flank_capability_gap"] is False


def test_exp29_flank_context_feature_injection_report_shape(tmp_path):
    module = _load_validation_module()
    model_path = tmp_path / "model.json"
    from services.games import chess_dl

    chess_dl.train_experiment_dl_from_replay_samples(
        [
            {
                "fen": module.chess.STARTING_FEN,
                "side": "white",
                "move_uci": "c2c4",
                "target": 1.0,
                "weight": 2.0,
                "expected_semantic": "flank_pawn_push",
                "semantic_class": "flank_pawn_push",
                "flank_context_features": module._flank_context_features(module.chess.STARTING_FEN, "white"),
                "flank_context_feature_vector": module._flank_context_feature_vector(module._flank_context_features(module.chess.STARTING_FEN, "white")),
                "flank_reason_tag": module._flank_reason_tag_for_move(module.chess.STARTING_FEN, "white", "c2c4"),
                "hard_negatives": ["e2e4", "d2d4", "g1f3", "a2a4"],
            }
        ],
        model_path=model_path,
        replay_path=tmp_path / "replay.jsonl",
        replace_replay=True,
    )
    report = module._flank_context_feature_injection_report(
        engine_alias="exp3",
        model_path=model_path,
        cases=[
            {
                "case_id": "flank_case",
                "fen": module.chess.STARTING_FEN,
                "side": "white",
                "expected_move": "c2c4",
                "expected_semantic": "flank_pawn_push",
                "semantic_class": "flank_pawn_push",
                "variant_difficulty": "hard",
                "flank_context_features": module._flank_context_features(module.chess.STARTING_FEN, "white"),
                "flank_context_feature_vector": module._flank_context_feature_vector(module._flank_context_features(module.chess.STARTING_FEN, "white")),
                "flank_reason_tag": module._flank_reason_tag_for_move(module.chess.STARTING_FEN, "white", "c2c4"),
            }
        ],
        trainer_result={
            "flank_context_feature_vector_used": True,
            "flank_context_classification_updates": 1,
            "flank_reason_tag_updates": 1,
            "flank_vs_nonflank_margin_updates": 1,
            "auxiliary_objectives": {"flank_context_classification_loss": True},
        },
    )

    assert report["source"] == "exp29_flank_context_feature_injection"
    assert report["trainer_feature_injection"] is True
    assert report["flank_context_classification_loss"]["updates"] == 1
    assert report["flank_vs_central_margin"]["count"] > 0
    assert report["case_count"] == 1


def test_live_learning_validation_requires_individual_engine_selection():
    module = _load_validation_module()

    assert module._select_requested_engines("exp3") == [("exp3", module.EXPERIMENT_DL_DIFFICULTY)]
    try:
        module._select_requested_engines("exp2")
    except ValueError as exc:
        assert "unknown engine alias" in str(exc)
    else:
        raise AssertionError("exp2 should be removed from validation engine selection")
    try:
        module._select_requested_engines("")
    except ValueError as exc:
        assert "pass exactly one --engines alias" in str(exc)
    else:
        raise AssertionError("empty engine selection should fail")
    try:
        module._select_requested_engines("exp3,exp4")
    except ValueError as exc:
        assert "multi-engine validation is disabled" in str(exc)
    else:
        raise AssertionError("multi-engine selection should fail without override")
    assert module._select_requested_engines("exp3,exp4", allow_multi_engine=True) == [
        ("exp3", module.EXPERIMENT_DL_DIFFICULTY),
        ("exp4", module.EXPERIMENT_PV_DIFFICULTY),
    ]


def test_live_learning_validation_retrains_every_ten_valid_games_and_records_old_probe():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "BASE_VALID_GAMES = 20" in source
    assert "EXTRA_VALID_GAMES = 5" in source
    assert "AUTORUN_THRESHOLD = 10" in source
    assert "return combined + extra_valid_games" in source
    assert "_evaluate_retention_probe" in source
    assert "_evaluate_mistake_retention_probe" in source
    assert '"counted_as_game": False' in source
    assert '"source": "old_trusted_engine_move"' in source
    assert '"source": "old_trusted_engine_move_mistake"' in source
    assert '"retention_probe": retention_probe' in source
    assert '"mistake_retention_probe": mistake_retention_probe' in source


def test_mistake_retention_probe_requires_before_failure_and_after_fix(monkeypatch):
    module = _load_validation_module()
    samples = [
        {
            "category": "mistake_retention",
            "fen": "fixture",
            "side": "white",
            "move_uci": "e2e4",
            "game_index": 1,
            "game_id": 7,
            "game_label": "old_error",
            "ply": 3,
        }
    ]
    calls = []

    def fake_choose(engine_alias, board_state, side, model_path, **_kwargs):
        calls.append(str(model_path))
        if str(model_path) == "before":
            return {"from": "g1", "to": "f3"}
        return {"from": "e2", "to": "e4"}

    monkeypatch.setattr(module, "_choose_engine_move_for_eval", fake_choose)

    result = module._evaluate_mistake_retention_probe("exp3", Path("before"), Path("after"), samples)

    assert result["counted_as_game"] is False
    assert result["source"] == "old_trusted_engine_move_mistake"
    assert result["probe_case_id"] == "game:7:ply:3:e2e4"
    assert result["before_failed"] is True
    assert result["after_fixed"] is True
    assert result["avoided_same_error"] is True
    assert result["avoided_old_mistake"] is True
    assert result["matched_expected"] is True
    assert result["result_kind"] == "matched_expected"
    assert result["learning_signal"] is True
    assert "before model failed" in result["learning_signal_reason"]


def test_mistake_retention_probe_treats_already_fixed_case_as_retained(monkeypatch):
    module = _load_validation_module()
    samples = [{"category": "mistake_retention", "fen": "fixture", "side": "white", "move_uci": "e2e4"}]

    monkeypatch.setattr(module, "_choose_engine_move_for_eval", lambda *args, **kwargs: {"from": "e2", "to": "e4"})

    result = module._evaluate_mistake_retention_probe("exp3", Path("before"), Path("after"), samples)

    assert result["supported"] is True
    assert result["reason"] == "retained_expected_move"
    assert result["counted_as_game"] is False
    assert result["probe_case_id"] == "game:0:ply:0:e2e4"
    assert result["before_move"] == "e2e4"
    assert result["after_move"] == "e2e4"
    assert result["avoided_same_error"] is False
    assert result["matched_expected"] is True
    assert result["result_kind"] == "retained_expected"
    assert result["learning_signal"] is True
    assert "仍保留正解" in result["human_explanation"]


def test_mistake_retention_probe_rejects_fixture_continuation(monkeypatch):
    module = _load_validation_module()
    samples = [
        {
            "category": "fixture_continuation",
            "fen": "fixture",
            "side": "black",
            "move_uci": "a7a5",
            "game_index": 1,
            "game_id": 8,
            "game_label": "continuation",
            "ply": 5,
        }
    ]
    monkeypatch.setattr(module, "_choose_engine_move_for_eval", lambda *args, **kwargs: {"from": "a7", "to": "a5"})

    result = module._evaluate_mistake_retention_probe("exp3", Path("before"), Path("after"), samples)

    assert result["supported"] is False
    assert result["reason"] == "no_mistake_retention_samples"
    assert result["learning_signal"] is False


def test_live_learning_validation_reports_retrain_and_step_timing():
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"retrain_duration_seconds"' in source
    assert '"checkpoint_duration_seconds"' in source
    assert '"avg_think_ms_per_step"' in source
    assert '"think_steps_measured"' in source
    assert "_flow_timing_summary" in source
    assert "_checkpoint_timing_summary" in source


def test_live_learning_validation_reports_audit_gate_and_failure_context():
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"dataset_integrity"' in source
    assert '"stability"' in source
    assert '"promotion_gate"' in source
    assert '"poison_detection"' in source
    assert '"replay_sources"' in source
    assert '"runtime_metrics"' in source
    assert '"reproducibility"' in source
    assert "## Why This Run Failed" in source
    assert "_failure_explanations" in source


def test_live_learning_validation_gate_is_decision_evidence_not_dashboard_only():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "deterministic strength gate skipped:" in source
    assert "Stochastic Auxiliary Game Benchmark" in source
    assert "catastrophic_regression" in source
    assert 'overall_verdict = "HIGH_RISK"' in source
    assert "forced repetition poison signal exceeded threshold" in source
    assert "illegal moves detected in dataset" in source
    assert '"gate_decision": checkpoint_gate' in source
    assert '"dataset_hash"' in source
    assert '"benchmark_skip_reason"' in source
    assert "## Can This Model Be Promoted?" in source
    assert "_promotion_explanation" in source
    assert "_report_consistency_issues" in source
    assert "_evaluate_deterministic_strength_snapshot" in source
    assert "hash_changed only proves model bytes changed" in source
    assert "retrain_stability_report" in source


def test_live_learning_validation_expands_traps_and_probe_range():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "white_fried_liver_probe" in source
    assert "black_caro_kann_probe" in source
    assert "short_low_signal_trap" in source
    assert "premature_resign_trap" in source
    assert "promotion_white" in source
    assert "free_queen_white" in source


def test_prepare_formal_dataset_blocks_validation_invalid_rows(tmp_path, monkeypatch):
    module = _load_validation_module()
    engine_dir = tmp_path / "exp3"
    runtime_dir = tmp_path / "runtime"
    reports_dir = runtime_dir / "reports" / "games"
    reports_dir.mkdir(parents=True)
    module._jsonl_dump(reports_dir / "chess_replays_quarantine.jsonl", [])
    module._jsonl_dump(reports_dir / "chess_replays_rejected.jsonl", [])

    def fake_run_json_subprocess(cmd, cwd):
        output_dir = Path(cmd[cmd.index("--output-dir") + 1])
        module._jsonl_dump(
            output_dir / "train.jsonl",
            [
                {"source_game_id": 1001, "engine_name": "experiment 3:dl", "move_count": 20},
                {"source_game_id": 2001, "engine_name": "experiment 3:dl", "move_count": 20},
            ],
        )
        return {"ok": True}

    monkeypatch.setattr(module, "_run_json_subprocess", fake_run_json_subprocess)

    result = module._prepare_formal_dataset(engine_dir, runtime_dir, invalid_game_ids={2001})
    train_rows = module._read_jsonl(engine_dir / "train_dataset.jsonl")
    rejected_rows = module._read_jsonl(engine_dir / "rejected_dataset.jsonl")

    assert result["contaminated_rows"] == 0
    assert result["blocked_contaminated_rows"] == 1
    assert [row["source_game_id"] for row in train_rows] == [1001]
    assert any(row["source_game_id"] == 2001 and row["reject_reason"] == "validation_invalid_game" for row in rejected_rows)


def test_checkpoint_trigger_consumes_target_and_ignores_invalid_stored_rows():
    module = _load_validation_module()
    pending = {10, 20}
    valid_game = SimpleNamespace(category="valid")
    invalid_game = SimpleNamespace(category="invalid_duplicate")

    assert module._should_launch_checkpoint(
        engine_alias="exp4",
        game=valid_game,
        stored_replay={"stored": True},
        trusted_replays=10,
        pending_checkpoint_targets=pending,
    )
    pending.discard(10)
    assert not module._should_launch_checkpoint(
        engine_alias="exp4",
        game=invalid_game,
        stored_replay={"stored": True},
        trusted_replays=10,
        pending_checkpoint_targets=pending,
    )
    assert not module._should_launch_checkpoint(
        engine_alias="exp4",
        game=valid_game,
        stored_replay={"stored": True},
        trusted_replays=10,
        pending_checkpoint_targets=pending,
    )
    assert module._should_launch_checkpoint(
        engine_alias="exp4",
        game=valid_game,
        stored_replay={"stored": True},
        trusted_replays=20,
        pending_checkpoint_targets=pending,
    )


def test_stage_game_win_rates_include_post_twenty_extra_normal_games():
    module = _load_validation_module()
    games = []
    outcomes = ["black"] * 10 + ["white"] * 5 + [None] * 5 + ["black"] * 3 + ["white"] * 2
    for index, winner_color in enumerate(outcomes, start=1):
        games.append(
            {
                "index": index,
                "category": "valid",
                "human_side": "white",
                "winner_color": winner_color,
                "stored_replay": {"collection_tier": "trusted"},
            }
        )

    rows = module._stage_game_win_rates(games, autorun_threshold=10)

    assert module.TOTAL_GAMES == 30
    assert module.VALID_GAMES == 25
    assert [row["stage"] for row in rows] == ["0-10", "10-20", "20-25"]
    assert all(row["basis"] == "trusted_valid_games_only" for row in rows)
    assert all(row["invalid_games_excluded"] is True for row in rows)
    assert rows[0]["normal_games"] == 10
    assert rows[0]["wins"] == 10
    assert rows[1]["normal_games"] == 10
    assert rows[1]["losses"] == 5
    assert rows[1]["draws"] == 5
    assert rows[1]["win_rate"] == 0.0
    assert rows[1]["win_rate_delta_from_previous_stage"] == -1.0
    assert rows[2]["normal_games"] == 5
    assert rows[2]["wins"] == 3
    assert rows[2]["win_rate"] == 0.6
    assert rows[2]["win_rate_delta_from_previous_stage"] == 0.6


def test_stage_win_rate_drop_marks_catastrophic_regression():
    module = _load_validation_module()
    summary = {
        "stage_game_win_rates": [
            {"stage": "0-10", "win_rate": 0.5},
            {"stage": "10-20", "win_rate": 0.2},
            {"stage": "20-25", "win_rate": 0.6},
        ],
        "before_after_eval": {"benchmark_before": {"skipped": True}, "benchmark_after": {"skipped": True}, "checkpoints": []},
    }

    stability = module._stability_summary(summary)

    assert stability["stage_win_rate_drop_10_20_vs_0_10"] == 0.3
    assert stability["stage_catastrophic_regression"] is True
    assert stability["catastrophic_regression"] is True


def test_late_stage_collapse_with_deterministic_and_mistake_retention_regression_blocks_gate():
    module = _load_validation_module()
    summary = {
        "engine_alias": "exp3",
        "replay_summary": {"trusted_replays": module.VALID_GAMES, "quarantine_replays": module.INVALID_GAMES},
        "dataset_integrity": {"contaminated_rows": 0, "duplicate_ratio": 0.0, "invalid_fen": 0, "illegal_moves": 0, "side_mismatch": 0, "short_resign_games": 0, "unique_positions": 20, "duplicate_positions": 0},
        "dataset_result": {"accepted_rows": 25},
        "poison_detection": {"forced_repetition_patterns": 0, "intentional_blunders": 0, "engine_copy_suspected": 0, "suspicious_resign_rate": 0.0},
        "position_quality": {},
        "stage_game_win_rates": [
            {"stage": "0-10", "win_rate": 0.2, "normal_games": 10},
            {"stage": "10-20", "win_rate": 0.3, "normal_games": 10},
            {"stage": "20-25", "win_rate": 0.0, "normal_games": 5},
        ],
        "before_after_eval": {
            "benchmark_before": {"skipped": True},
            "benchmark_after": {"skipped": True},
            "checkpoints": [
                {
                    "trusted_count": 20,
                    "mistake_retention_probe": {
                        "learning_signal": False,
                        "probe_case_id": "fixture",
                        "before_move": "h4e4",
                        "after_move": "h4e4",
                        "expected_move": "e2e4",
                        "avoided_same_error": False,
                        "avoided_old_mistake": False,
                        "matched_expected": False,
                        "result_kind": "repeated_old_mistake",
                        "learning_signal_reason": "after model repeated the same wrong move",
                    },
                }
            ],
        },
        "deterministic_strength_snapshot": {
            "supported": True,
            "skipped": False,
            "passed": False,
            "reasons": ["final deterministic score regressed below baseline"],
            "score_table": [
                {"model_label": "baseline", "overall_deterministic_score": 1.0, "category_score": {"mistake_retention": {"score": 1.0}}},
                {"model_label": "checkpoint@10", "overall_deterministic_score": 0.9231, "category_score": {"mistake_retention": {"score": 0.6667}}},
                {"model_label": "checkpoint@20", "overall_deterministic_score": 0.9231, "category_score": {"mistake_retention": {"score": 0.6667}}},
                {"model_label": "final", "overall_deterministic_score": 0.9231, "category_score": {"mistake_retention": {"score": 0.6667}}},
            ],
            "final": {"illegal_rate": 0.0, "blunder_avoid_rate": 1.0},
        },
        "engine_verdict": "PASS",
    }

    summary["retrain_stability_report"] = module._retrain_stability_report(summary)
    summary["stability"] = module._stability_summary(summary)
    gate = module._promotion_gate_summary(summary)

    assert summary["retrain_stability_report"]["suspected_catastrophic_regression"] is True
    assert summary["stability"]["late_stage_win_rate_collapse"] is True
    assert summary["stability"]["catastrophic_regression"] is True
    assert gate["passed"] is False
    assert "catastrophic regression detected" in gate["reasons"]
    assert "retrain stability risk: late trusted-valid stage 20-25 collapsed to win_rate=0.0" in gate["reasons"]
    assert "retrain stability risk: mistake_retention deterministic category regressed" in gate["reasons"]


def test_quick_retrain_gate_fixture_is_fixed_legal_and_extracts_engine_samples():
    module = _load_validation_module()

    records = module._quick_retrain_fixture_records(
        engine_alias="exp3",
        difficulty="experiment 3:dl",
        actor_username="fixture_user",
        seed=20260509,
    )
    samples = module._extract_engine_move_samples_from_records(records)

    assert len(records) == module.QUICK_RETRAIN_GATE_TRUSTED_REPLAYS
    assert all(record["collection_tier"] == "trusted" for record in records)
    assert all(record["source"] == "quick_retrain_gate_fixture" for record in records)
    assert all(int(record["move_count"]) >= 8 for record in records)
    assert samples
    assert all(sample["side"] in {"white", "black"} for sample in samples)
    categories = {str(record.get("quick_category")) for record in records}
    assert {"opening", "tactic", "endgame", "blunder_avoid", "mistake_retention"} <= categories
    for category in categories:
        assert sum(1 for record in records if record.get("quick_category") == category) >= module.QUICK_RETRAIN_MIN_CASES_PER_CATEGORY
    health = module._quick_replay_fixture_health(records, samples, [])
    assert health["passed"] is True
    assert health["duplicate_ratio"] <= module.DATASET_DUPLICATE_RATIO_LIMIT
    assert health["unique_target_move_count"] >= len(categories)


def test_deterministic_strength_gate_blocks_regression_without_using_game_benchmark():
    module = _load_validation_module()
    snapshots = [
        {"model_label": "baseline", "model_hash": "base", "aggregate": {"overall_deterministic_score": 0.8, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0, "category_score": {"mistake_retention": {"score": 1.0}}}},
        {"model_label": "checkpoint@10", "model_hash": "c10", "aggregate": {"overall_deterministic_score": 0.8, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0, "category_score": {"mistake_retention": {"score": 1.0}}}},
        {"model_label": "checkpoint@20", "model_hash": "c20", "aggregate": {"overall_deterministic_score": 0.75, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0, "category_score": {"mistake_retention": {"score": 1.0}}}},
        {"model_label": "final", "model_hash": "final", "aggregate": {"overall_deterministic_score": 0.6, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0, "category_score": {"mistake_retention": {"score": 1.0}}}},
    ]

    report = module._deterministic_strength_report(snapshots)

    assert report["passed"] is False
    assert "final deterministic score regressed below baseline" in report["reasons"]
    assert "final deterministic score regressed beyond checkpoint@20 threshold" in report["reasons"]


def test_exp4_guarded_overlay_keeps_baseline_default_and_only_adopts_safe_positive_cases():
    module = _load_validation_module()

    def case(case_id, category, expected, top1, top3=None, illegal=False):
        top3 = top3 or [top1]
        return {
            "case_id": case_id,
            "category": category,
            "fen": "8/8/8/8/8/8/8/8 w - - 0 1",
            "side": "white",
            "expected_best_moves": [expected],
            "engine_top1": top1,
            "engine_top3": top3,
            "top1_correct": top1 == expected,
            "top3_contains": expected in top3,
            "illegal_move": illegal,
            "blunder": False,
        }

    deterministic_report = {
        "score_table": [
            {"model_label": "baseline", "overall_deterministic_score": 0.5},
            {"model_label": "final", "overall_deterministic_score": 0.5},
        ],
        "snapshots": [
            {
                "model_label": "baseline",
                "cases": [
                    case("same", "opening", "e2e4", "e2e4"),
                    case("final_improves", "mistake_retention", "d7d5", "e7e5", ["e7e5", "g8f6"]),
                    case("final_regresses", "blunder_avoid", "g1f3", "g1f3"),
                    case("both_wrong", "tactic", "c2c4", "b1c3", ["b1c3", "d2d4"]),
                ],
            },
            {
                "model_label": "final",
                "cases": [
                    case("same", "opening", "e2e4", "e2e4"),
                    case("final_improves", "mistake_retention", "d7d5", "d7d5"),
                    case("final_regresses", "blunder_avoid", "g1f3", "h2h4", ["h2h4", "a2a4"]),
                    case("both_wrong", "tactic", "c2c4", "g1f3", ["g1f3", "d2d4"]),
                ],
            },
        ],
    }

    report = module._exp4_guarded_overlay_attribution(deterministic_report)

    assert report["supported"] is True
    assert report["production_ready"] is False
    assert report["full_model_replacement"] is False
    assert report["decision_counts"]["positive_override"] == 1
    assert report["decision_counts"]["prevented_regression"] == 1
    assert report["decision_counts"]["unresolved_no_gain"] == 1
    assert report["unsafe_override_count"] == 0
    assert report["guarded_overlay_score"] > report["baseline_score"]
    assert report["candidate_worth_runtime_overlay"] is True
    selected = {row["case_id"]: row for row in report["cases"]}
    assert selected["final_improves"]["selected_source"] == "final"
    assert selected["final_regresses"]["selected_source"] == "baseline"
    assert selected["both_wrong"]["selected_source"] == "baseline"


def test_exp4_runtime_guarded_overlay_uses_no_labels_and_blocks_promotion_piece_regression():
    module = _load_validation_module()
    from services.games.chess_pv_guarded_overlay import exp4_runtime_overlay_allows_final

    def case(case_id, fen, expected, top1, top3=None, score_cp=0, category="opening", illegal=False):
        top3 = top3 or [top1]
        return {
            "case_id": case_id,
            "category": category,
            "fen": fen,
            "side": "white" if " w " in fen else "black",
            "expected_best_moves": [expected],
            "engine_top1": top1,
            "engine_top3": top3,
            "top1_correct": top1 == expected,
            "top3_contains": expected in top3,
            "illegal_move": illegal,
            "blunder": False,
            "score_cp": score_cp,
            "policy_score": score_cp,
        }

    opening_fen = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1"
    promotion_fen = "k7/4P3/2K5/8/8/8/8/8 w - - 0 1"
    deterministic_report = {
        "score_table": [
            {"model_label": "baseline", "overall_deterministic_score": 0.5},
            {"model_label": "final", "overall_deterministic_score": 0.5},
        ],
        "snapshots": [
            {
                "model_label": "baseline",
                "cases": [
                    case("safe_opening", opening_fen, "d7d5", "e7e5", ["e7e5", "c7c5"], category="mistake_retention"),
                    case("promotion_regression", promotion_fen, "e7e8q", "e7e8q", ["e7e8q", "e7e8n"], score_cp=-9, category="endgame"),
                ],
            },
            {
                "model_label": "final",
                "cases": [
                    case("safe_opening", opening_fen, "d7d5", "d7d5", ["d7d5", "e7e5"], category="mistake_retention"),
                    case("promotion_regression", promotion_fen, "e7e8q", "e7e8n", ["e7e8n", "e7e8q"], score_cp=-3, category="endgame"),
                ],
            },
        ],
    }

    report = module._exp4_runtime_guarded_overlay_report(deterministic_report)

    assert report["supported"] is True
    assert report["diagnostic_uses_expected_labels_for_decision"] is False
    assert report["production_ready"] is False
    assert report["decision_counts"]["positive_override_after_scoring"] == 0
    assert report["decision_counts"]["prevented_regression_after_scoring"] == 1
    assert report["unsafe_override_count"] == 0
    assert report["runtime_guarded_score"] == report["baseline_score"]
    assert report["candidate_worth_runtime_overlay"] is False
    selected = {row["case_id"]: row for row in report["cases"]}
    assert selected["safe_opening"]["selected_source"] == "baseline"
    assert selected["safe_opening"]["guard_reason"] == "ordinary_runtime_margin_insufficient"
    assert selected["promotion_regression"]["selected_source"] == "baseline"
    assert selected["promotion_regression"]["guard_reason"] == "nonqueen_promotion_downgrade_without_runtime_tactical_reason"

    allowed, reason, detail = exp4_runtime_overlay_allows_final(
        fen=opening_fen,
        side="black",
        baseline_move_uci="e7e5",
        final_move_uci="d7d5",
        baseline_score_cp=None,
        final_score_cp=None,
    )
    assert allowed is False
    assert reason == "ordinary_runtime_margin_insufficient"
    assert detail["baseline_score_cp"] is not None
    assert detail["final_score_cp"] is not None

    allowed, reason, detail = exp4_runtime_overlay_allows_final(
        fen=opening_fen,
        side="black",
        baseline_move_uci="e7e5",
        final_move_uci="d7d5",
        baseline_score_cp=0,
        final_score_cp=200,
    )
    assert allowed is True
    assert reason == "runtime_static_and_rule_guard_passed"
    assert detail["ordinary_override_min_delta_cp"] == 125


def test_exp4_guarded_overlay_actual_runtime_helper_uses_real_board_state(monkeypatch):
    from services.games import chess_pv
    from services.games.chess_pv_guarded_overlay import explain_experiment_pv_guarded_overlay_decision

    def fake_choose(_board_state, _side, *, model_path=None, **_kwargs):
        if "candidate" in str(model_path):
            return {"from": "d7", "to": "d5"}
        return {"from": "e7", "to": "e5"}

    monkeypatch.setattr(chess_pv, "choose_experiment_pv_move", fake_choose)
    decision = explain_experiment_pv_guarded_overlay_decision(
        {"__fen__": "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1"},
        "black",
        baseline_model_path=Path("baseline_model.json"),
        candidate_model_path=Path("candidate_model.json"),
    )

    assert decision["selected_source"] == "baseline"
    assert decision["selected_move_uci"] == "e7e5"
    assert decision["guard_reason"] == "ordinary_runtime_margin_insufficient"
    assert decision["guard_detail"]["baseline_score_cp"] is not None
    assert decision["guard_detail"]["final_score_cp"] is not None


def test_exp4_guarded_overlay_broad_sanity_gate_uses_no_label_runtime_overlay_shape():
    module = _load_validation_module()
    summary = {
        "engine_alias": "exp4",
        "exp4_actual_runtime_guarded_overlay": {
            "supported": True,
            "candidate_worth_runtime_overlay": True,
            "unsafe_override_count": 0,
            "simulator_selected_mismatch_count": 0,
            "baseline_score": 0.8693,
            "actual_runtime_guarded_score": 0.9231,
            "delta_vs_baseline": 0.0538,
        },
        "special_rule_deterministic_gate": {"supported": True, "pass_rate": 1.0},
        "before_after_eval": {
            "checkpoints": [
                {
                    "trusted_count": 10,
                    "sanity_learning_probe": {
                        "guarded_overlay_sanity": {
                            "seen_variants": {
                                "supported": True,
                                "pass_rate": 0.5,
                                "baseline_pass_rate": 0.4,
                                "unsafe_override_count": 0,
                            },
                            "unseen_variants": {
                                "supported": True,
                                "pass_rate": 0.45,
                                "baseline_pass_rate": 0.45,
                                "unsafe_override_count": 0,
                            },
                        }
                    },
                }
            ]
        },
    }

    gate = module._guarded_overlay_broad_sanity_gate(summary)

    assert gate["supported"] is True
    assert gate["passed"] is True
    assert gate["production_enablement_required"] is True
    assert gate["actual_runtime_guarded_overlay"]["delta_vs_baseline"] == 0.0538
    assert gate["per_trusted"][0]["seen_non_regression"] is True
    assert gate["per_trusted"][0]["unseen_non_regression"] is True


def test_exp4_guarded_overlay_broad_sanity_gate_blocks_runtime_regression_and_mismatch():
    module = _load_validation_module()
    summary = {
        "engine_alias": "exp4",
        "exp4_actual_runtime_guarded_overlay": {
            "supported": True,
            "candidate_worth_runtime_overlay": False,
            "unsafe_override_count": 1,
            "simulator_selected_mismatch_count": 1,
            "baseline_score": 0.8693,
            "actual_runtime_guarded_score": 0.86,
            "delta_vs_baseline": -0.0093,
        },
        "special_rule_deterministic_gate": {"supported": True, "pass_rate": 0.8571},
        "before_after_eval": {
            "checkpoints": [
                {
                    "trusted_count": 20,
                    "sanity_learning_probe": {
                        "guarded_overlay_sanity": {
                            "seen_variants": {
                                "supported": True,
                                "pass_rate": 0.3,
                                "baseline_pass_rate": 0.5,
                                "unsafe_override_count": 0,
                            },
                            "unseen_variants": {
                                "supported": True,
                                "pass_rate": 0.2,
                                "baseline_pass_rate": 0.3,
                                "unsafe_override_count": 1,
                            },
                        }
                    },
                }
            ]
        },
    }

    gate = module._guarded_overlay_broad_sanity_gate(summary)

    assert gate["passed"] is False
    assert "actual runtime guarded overlay did not improve over baseline safely" in gate["reasons"]
    assert "actual runtime guarded overlay has unsafe overrides" in gate["reasons"]
    assert "actual runtime guarded overlay differs from simulator" in gate["reasons"]
    assert "actual runtime guarded overlay score did not improve over baseline" in gate["reasons"]
    assert "guarded overlay seen variant pass rate regressed at trusted=20" in gate["reasons"]
    assert "guarded overlay unseen variant pass rate regressed at trusted=20" in gate["reasons"]
    assert "guarded overlay sanity unsafe override at trusted=20" in gate["reasons"]
    assert "special-rule gate below 1.0" in gate["reasons"]


def test_compact_sanity_probe_preserves_guarded_overlay_baseline_rates():
    module = _load_validation_module()
    compact = module._compact_sanity_probe_for_summary(
        {
            "guarded_overlay_generalization_rate": 0.4,
            "guarded_overlay_seen_variant_pass_rate": 0.5,
            "guarded_overlay_unseen_variant_pass_rate": 0.3,
            "guarded_overlay_seen_baseline_pass_rate": 0.45,
            "guarded_overlay_unseen_baseline_pass_rate": 0.35,
            "guarded_overlay_seen_final_pass_rate": 0.48,
            "guarded_overlay_unseen_final_pass_rate": 0.32,
            "guarded_overlay_unsafe_override_count": 2,
        }
    )

    assert compact["guarded_overlay_seen_baseline_pass_rate"] == 0.45
    assert compact["guarded_overlay_unseen_baseline_pass_rate"] == 0.35
    assert compact["guarded_overlay_seen_final_pass_rate"] == 0.48
    assert compact["guarded_overlay_unseen_final_pass_rate"] == 0.32


def test_quick_gate_does_not_promote_on_loss_or_hash_without_score_and_matched_probe():
    module = _load_validation_module()
    summary = {
        "engine_alias": "exp4",
        "expected_trusted_replays": 20,
        "expected_quarantine_replays": 0,
        "quick_retrain_gate": {"enabled": True},
        "replay_summary": {"trusted_replays": 20, "quarantine_replays": 0},
        "dataset_integrity": {"contaminated_rows": 0, "duplicate_ratio": 0.0, "invalid_fen": 0, "illegal_moves": 0, "side_mismatch": 0, "short_resign_games": 0},
        "dataset_result": {"accepted_rows": 20},
        "poison_detection": {"forced_repetition_patterns": 0, "intentional_blunders": 0, "engine_copy_suspected": 0, "suspicious_resign_rate": 0.0},
        "replay_fixture_health": {"passed": True, "reasons": []},
        "stability": {"catastrophic_regression": False},
        "before_after_eval": {
            "checkpoints": [
                {
                    "trusted_count": 10,
                    "hash_changed": True,
                    "replay_loss_before": {"loss": 0.9},
                    "replay_loss_after": {"loss": 0.1},
                    "mistake_retention_probe": {
                        "learning_signal": False,
                        "matched_expected": False,
                        "learning_signal_reason": "after model repeated the same wrong move",
                    },
                }
            ]
        },
        "deterministic_strength_snapshot": {
            "supported": True,
            "skipped": False,
            "passed": True,
            "reasons": [],
            "score_table": [
                {"model_label": "baseline", "overall_deterministic_score": 0.5},
                {"model_label": "final", "overall_deterministic_score": 0.5},
            ],
            "final": {"overall_deterministic_score": 0.5, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0},
        },
        "engine_verdict": "PASS",
    }

    gate = module._promotion_gate_summary(summary)

    assert gate["passed"] is False
    assert "deterministic score did not improve over baseline; learning success not proven" in gate["reasons"]
    assert any("mistake retention probe failed" in reason for reason in gate["reasons"])
    assert "mistake retention probe did not match expected move at trusted=10" in gate["reasons"]


def test_sanity_learning_probe_requires_after_top1_and_variant_generalization(monkeypatch):
    module = _load_validation_module()
    samples = [
        {
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "side": "black",
            "move_uci": "e7e5",
            "game_id": 900001,
            "game_label": "mr_e4_e5",
            "ply": 1,
            "category": "mistake_retention",
        }
    ]

    def fake_choose(_engine_alias, board_state, _side, model_path, **_kwargs):
        if str(model_path) == "before":
            return {"from": "a7", "to": "a5"}
        return {"from": "e7", "to": "e5"} if " e6 " not in str(board_state.get("__fen__")) else {"from": "a7", "to": "a5"}

    monkeypatch.setattr(module, "_choose_engine_move_for_eval", fake_choose)
    monkeypatch.setattr(
        module,
        "_engine_decision_breakdown",
        lambda _engine_alias, _model_path, case, **_kwargs: {
            "chosen_move": case.get("expected_move"),
            "chosen_reason": "fixture_fast_decision",
            "candidate_scores": [],
        },
    )

    def fake_raw_policy(_engine_alias, model_path, case, **_kwargs):
        expected = str(case.get("expected_move") or "e7e5")
        before = str(model_path) == "before"
        return {
            "supported": True,
            "raw_policy_top1": "a7a5" if before else expected,
            "raw_policy_top3": ["a7a5", expected, "b8c6"] if before else [expected, "a7a5", "b8c6"],
            "expected_rank": 2 if before else 1,
            "old_move_rank": 1 if before else 2,
            "expected_is_raw_top1": not before,
            "margin_vs_old_move": -1.0 if before else 1.0,
            "expected_score": 0.0 if before else 1.0,
            "old_move_score": 1.0 if before else 0.0,
        }

    monkeypatch.setattr(module, "_evaluate_engine_raw_policy_position", fake_raw_policy)

    result = module._evaluate_sanity_learning_probe("exp4", Path("before"), Path("after"), samples)

    assert result["supported"] is True
    assert result["before_exact"]["top1"] == "a7a5"
    assert result["after_exact"]["top1"] == "e7e5"
    assert result["after_exact"]["expected_is_top1"] is True
    assert result["result_kind"] in {"memorized_exact_fen", "partial_seen_variants_only", "generalized_to_variants"}
    assert result["learning_signal"] is (result["result_kind"] == "generalized_to_variants")
    assert result["variant_count"] >= 5
    assert result["exact_fen_pass"] is True
    assert "seen_variant_pass_rate" in result
    assert "unseen_variant_pass_rate" in result
    assert "easy_unseen_pass_rate" in result
    assert "medium_unseen_pass_rate" in result
    assert "hard_unseen_pass_rate" in result
    assert result["variant_difficulty_scores"]["hard"]["held_out_from_training"] is True
    assert result["feature_generalization_debug"]["board_embedding_similarity"]["cases"]
    assert "expected_move_rank_across_variants" in result["feature_generalization_debug"]
    assert "final_decision_blockers" in result["feature_generalization_debug"]
    assert "raw_policy_generalization_rate" in result
    assert "final_decision_generalization_rate" in result
    assert "raw_policy_unseen_generalization_rate" in result
    assert "final_decision_unseen_generalization_rate" in result
    assert result["feature_generalization_debug"]["split"]["held_out_never_trained"] is True
    assert "failed_unseen_cases" in result["feature_generalization_debug"]
    assert "failed_feature_groups" in result["feature_generalization_debug"]
    assert "embedding_similarity" in result["feature_generalization_debug"]
    assert "expected_vs_hard_negative_margin" in result["feature_generalization_debug"]
    assert "replay_loss" in result["not_learning_success_sources"]
    assert "hash_changed" in result["not_learning_success_sources"]


def test_sanity_variant_training_rows_record_seen_variant_evidence(monkeypatch):
    module = _load_validation_module()
    samples = [
        {
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "side": "black",
            "move_uci": "e7e5",
            "game_id": 900001,
            "game_label": "mr_e4_e5",
            "ply": 1,
            "category": "mistake_retention",
        }
    ]

    monkeypatch.setattr(module, "_choose_engine_move_for_eval", lambda *_args, **_kwargs: {"from": "a7", "to": "a5"})

    result = module._sanity_seen_variant_training_rows("exp3", Path("before"), samples)

    assert result["case"]["expected_move"] == "e7e5"
    assert result["rows"]
    assert any(row["variant_split"] == "exact" for row in result["rows"])
    assert any(row["variant_split"] == "train" for row in result["rows"])
    assert any(row["move_uci"] == "e7e5" for row in result["rows"])
    assert len({row["move_uci"] for row in result["rows"]}) > 1
    assert all(row["variant_id"] and row["normalized_fen_hash"] for row in result["rows"])
    assert any(row["hard_negatives"] for row in result["rows"])
    assert any(row["invariance_group_id"] == "black|e7e5" for row in result["rows"])
    assert any(row["pairwise_role"] == "positive_variant" for row in result["rows"])
    assert result["curriculum"]["train"]
    assert result["curriculum"]["validation"]
    assert result["curriculum"]["held_out"]
    assert result["curriculum"]["by_difficulty"]["easy"]["seen"]
    assert result["curriculum"]["by_difficulty"]["medium"]["seen"]
    assert result["curriculum"]["by_difficulty"]["hard"]["held_out"]
    assert result["semantic_coverage_by_split"]["train"]["complete"] is True
    assert result["semantic_coverage_by_split"]["validation"]["complete"] is True
    assert all(count > 0 for count in result["semantic_distribution_by_split"]["train"].values())
    assert all(count > 0 for count in result["semantic_distribution_by_split"]["validation"].values())
    assert result["semantic_sampling"]["method"] == "inverse_frequency_row_weight"
    assert result["semantic_sampling"]["passed"] is True
    assert result["semantic_sampling"]["skew_ratio"] <= module.SEMANTIC_SAMPLING_MAX_SKEW_RATIO
    assert all(weight > 0 for weight in result["effective_sample_weight_by_semantic"].values())
    assert result["train_effective_distribution"]
    trained_hashes = {row["normalized_fen_hash"] for row in result["rows"]}
    held_out_hashes = {row["normalized_fen_hash"] for row in result["curriculum"]["held_out"]}
    assert trained_hashes.isdisjoint(held_out_hashes)


def test_sanity_learning_probe_failed_when_after_top1_is_not_expected(monkeypatch):
    module = _load_validation_module()
    samples = [
        {
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "side": "black",
            "move_uci": "e7e5",
            "game_id": 900001,
            "game_label": "mr_e4_e5",
            "ply": 1,
            "category": "mistake_retention",
        }
    ]

    monkeypatch.setattr(module, "_choose_engine_move_for_eval", lambda *_args, **_kwargs: {"from": "a7", "to": "a5"})
    monkeypatch.setattr(
        module,
        "_engine_decision_breakdown",
        lambda *_args, **_kwargs: {"chosen_move": "a7a5", "chosen_reason": "fixture_fast_decision"},
    )
    monkeypatch.setattr(
        module,
        "_evaluate_engine_raw_policy_position",
        lambda *_args, **_kwargs: {
            "supported": True,
            "raw_policy_top1": "a7a5",
            "raw_policy_top3": ["a7a5", "b8c6", "g8f6"],
            "expected_rank": 4,
            "old_move_rank": 1,
            "expected_is_raw_top1": False,
            "margin_vs_old_move": -1.0,
        },
    )

    result = module._evaluate_sanity_learning_probe("exp4", Path("before"), Path("after"), samples)

    assert result["result_kind"] == "failed_to_learn"
    assert result["learning_signal"] is False
    assert result["after_exact"]["top1"] == "a7a5"
    assert result["after_exact"]["expected_is_top1"] is False


def test_sanity_learning_probe_marks_partial_when_raw_policy_learns_but_final_decision_does_not(monkeypatch):
    module = _load_validation_module()
    samples = [
        {
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "side": "black",
            "move_uci": "e7e5",
            "game_id": 900001,
            "game_label": "mr_e4_e5",
            "ply": 1,
            "category": "mistake_retention",
        }
    ]

    monkeypatch.setattr(module, "_choose_engine_move_for_eval", lambda *_args, **_kwargs: {"from": "a7", "to": "a5"})
    monkeypatch.setattr(
        module,
        "_evaluate_engine_raw_policy_position",
        lambda engine_alias, model_path, case, old_move="": {
            "supported": True,
            "expected_move": "e7e5",
            "raw_policy_top1": "a7a5" if str(model_path) == "before" else "e7e5",
            "expected_rank": 4 if str(model_path) == "before" else 1,
            "old_move": "a7a5",
            "old_move_rank": 1 if str(model_path) == "before" else 3,
            "margin_vs_old_move": -0.5 if str(model_path) == "before" else 0.4,
            "expected_is_raw_top1": str(model_path) == "after",
        },
    )
    monkeypatch.setattr(
        module,
        "_engine_decision_breakdown",
        lambda *_args, **_kwargs: {
            "supported": True,
            "chosen_move": "a7a5",
            "chosen_reason": "search_best_move",
            "watched_moves": [
                {"move": "e7e5", "raw_policy_score": 1.0, "static_eval_score": 0, "search_score": 0, "legal_move_bonus_penalty": 28, "final_combined_score": 0},
                {"move": "a7a5", "raw_policy_score": 0.2, "static_eval_score": 50, "search_score": 50, "legal_move_bonus_penalty": 0, "final_combined_score": 50},
            ],
        },
    )

    result = module._evaluate_sanity_learning_probe("exp4", Path("before"), Path("after"), samples)

    assert result["result_kind"] == "partial_policy_learned_but_decision_unchanged"
    assert result["raw_policy_learning"]["learning_signal"] is True
    assert result["final_decision_learning"]["learning_signal"] is False
    assert result["blocked_by_search_or_static_eval"] is True
    assert "final decision stayed" in result["final_decision_learning"]["blocked_reason"]


def test_promotion_gate_blocks_partial_policy_learning_without_final_decision_learning():
    module = _load_validation_module()
    summary = {
        "engine_alias": "exp4",
        "replay_summary": {"trusted_replays": module.VALID_GAMES, "quarantine_replays": module.INVALID_GAMES},
        "dataset_integrity": {"contaminated_rows": 0, "duplicate_ratio": 0.0, "invalid_fen": 0, "illegal_moves": 0, "side_mismatch": 0, "short_resign_games": 0},
        "poison_detection": {"forced_repetition_patterns": 0, "intentional_blunders": 0, "engine_copy_suspected": 0, "suspicious_resign_rate": 0.0},
        "stability": {"catastrophic_regression": False},
        "quick_retrain_gate": {"enabled": True},
        "before_after_eval": {
            "benchmark_before": {"skipped": True, "reason": "stochastic_auxiliary_disabled"},
            "benchmark_after": {"skipped": True, "reason": "stochastic_auxiliary_disabled"},
            "checkpoints": [
                {
                    "trusted_count": 10,
                    "mistake_retention_probe": {"learning_signal": True, "matched_expected": True},
                    "sanity_learning_probe": {
                        "result_kind": "partial_policy_learned_but_decision_unchanged",
                        "learning_signal_reason": "raw policy learned expected_move but final decision did not change",
                        "final_decision_learning": {
                            "learning_signal": False,
                            "blocked_reason": "raw policy learned expected_move but final decision stayed a7a5 via search_best_move",
                        },
                    },
                }
            ],
        },
        "deterministic_strength_snapshot": {
            "supported": True,
            "skipped": False,
            "passed": True,
            "reasons": [],
            "score_table": [
                {"model_label": "baseline", "overall_deterministic_score": 0.5},
                {"model_label": "final", "overall_deterministic_score": 0.8},
            ],
            "final": {"overall_deterministic_score": 0.8, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0},
        },
        "engine_verdict": "PASS",
    }

    gate = module._promotion_gate_summary(summary)

    assert gate["passed"] is False
    assert any("sanity final decision learning failed" in reason for reason in gate["reasons"])
    assert "sanity raw policy learned but final decision unchanged at trusted=10" in gate["reasons"]


def test_stochastic_benchmark_hash_perft_and_runtime_do_not_release_promotion():
    module = _load_validation_module()
    summary = {
        "engine_alias": "exp3",
        "replay_summary": {"trusted_replays": module.VALID_GAMES, "quarantine_replays": module.INVALID_GAMES},
        "dataset_integrity": {"contaminated_rows": 0, "duplicate_ratio": 0.0, "invalid_fen": 0, "illegal_moves": 0, "side_mismatch": 0, "short_resign_games": 0},
        "poison_detection": {"forced_repetition_patterns": 0, "intentional_blunders": 0, "engine_copy_suspected": 0, "suspicious_resign_rate": 0.0},
        "stability": {"catastrophic_regression": False},
        "before_after_eval": {
            "benchmark_before": {"skipped": True, "reason": "stochastic_auxiliary_disabled"},
            "benchmark_after": {"skipped": True, "reason": "stochastic_auxiliary_disabled"},
            "checkpoints": [
                {
                    "trusted_count": 10,
                    "trusted_replays": 10,
                    "hash_changed": True,
                    "model_hash_changed": True,
                    "previous_model_hash": "before",
                    "new_model_hash": "after",
                    "pre_checkpoint_model_sha256": "before",
                    "post_checkpoint_model_sha256": "after",
                    "ineffective_training": False,
                    "mistake_retention_probe": {"learning_signal": True},
                }
            ],
        },
        "deterministic_strength_snapshot": {"supported": True, "skipped": False, "passed": False, "reasons": ["deterministic fixture failed"], "final": {"illegal_rate": 0.0, "blunder_avoid_rate": 1.0}},
        "stochastic_auxiliary_benchmark": {"skipped": True, "strength_evidence": False},
        "perft": {"strength_evidence": False},
        "runtime_metrics": {"train_seconds": 1.0},
        "engine_verdict": "PASS",
    }

    gate = module._promotion_gate_summary(summary)

    assert gate["passed"] is False
    assert "deterministic strength gate failed: deterministic fixture failed" in gate["reasons"]
    assert not any("stochastic_auxiliary_disabled" in reason for reason in gate["reasons"])


def test_live_learning_validation_minimal_gate_failure_fixture_writes_consistent_reports(tmp_path):
    module = _load_validation_module()
    module._environment_summary = lambda: {"python_version": "test", "platform": "test", "cpu": "test", "gpu": "", "torch_version": ""}
    skip_reason = "disabled_by_fast_retrain"
    summary = {
        "engine_alias": "exp3",
        "difficulty": "experiment 3:dl",
        "seed": 7,
        "started_at": "2026-05-09T00:00:00+00:00",
        "finished_at": "2026-05-09T00:01:00+00:00",
        "commit": "fixture",
        "total_games": module.TOTAL_GAMES,
        "games": [],
        "invalid_case_audit": [],
        "replay_summary": {"trusted_replays": module.VALID_GAMES, "quarantine_replays": module.INVALID_GAMES, "rejected_replays": 0},
        "retrain_result": {"retrain_supported": True, "trainer_probe": {"validation": {"accepted_samples_gt_zero": True, "rejected_samples_match": True}}},
        "autorun": {"launched": True},
        "autorun_status": {"status": "completed"},
        "dataset_result": {"accepted_rows": 3, "rejected_rows": 2, "dataset_sha256": "datasetfixture"},
        "dataset_integrity": {
            "total_rows": 3,
            "unique_positions": 1,
            "duplicate_positions": 2,
            "duplicate_ratio": 0.667,
            "invalid_fen": 1,
            "illegal_moves": 1,
            "side_mismatch": 0,
            "mate_positions": 0,
            "terminal_positions": 0,
            "avg_game_length": 3.0,
            "short_resign_games": 0,
            "contaminated_rows": 0,
        },
        "poison_detection": {
            "forced_repetition_patterns": 1,
            "intentional_blunders": 1,
            "engine_copy_suspected": 1,
            "suspicious_resign_rate": 0.2,
            "suspicious_resigns": 1,
        },
        "evaluation_before": {"agreement": 0.5, "avg_think_ms": 1.0, "total_think_ms": 2.0},
        "evaluation_after": {"agreement": 0.5, "avg_think_ms": 1.1, "total_think_ms": 2.2},
        "game_timing": {"avg_think_ms_per_step": 3.0, "steps_measured": 2, "total_think_ms": 6.0},
        "retrain_timing": {"checkpoint_count": 1, "total_retrain_seconds": 4.0, "avg_retrain_seconds": 4.0, "total_checkpoint_seconds": 5.0},
        "model_before": {"sha256": "before"},
        "model_after": {"sha256": "after"},
        "before_after_eval": {
            "benchmark_before": {"skipped": True, "reason": skip_reason},
            "benchmark_after": {"skipped": True, "reason": skip_reason},
            "checkpoints": [
                {
                    "trusted_count": 10,
                    "trusted_replays": 10,
                    "started_at": "2026-05-09T00:00:10+00:00",
                    "finished_at": "2026-05-09T00:00:14+00:00",
                    "duration_seconds": 4.0,
                    "dataset_hash": "sha256:datasetfixture",
                    "previous_model_hash": "before",
                    "new_model_hash": "after",
                    "hash_changed": True,
                    "pre_checkpoint_model_sha256": "before",
                    "post_checkpoint_model_sha256": "after",
                    "benchmark_skipped": True,
                    "benchmark_skip_reason": skip_reason,
                    "gate_decision": {"passed": False, "reasons": [f"benchmark skipped: {skip_reason}"]},
                    "mistake_retention_probe": {
                        "probe_case_id": "game:7:ply:3:e2e4",
                        "before_move": "g1f3",
                        "after_move": "g1f3",
                        "expected_move": "e2e4",
                        "avoided_same_error": False,
                        "avoided_old_mistake": False,
                        "matched_expected": False,
                        "result_kind": "repeated_old_mistake",
                        "learning_signal": False,
                        "learning_signal_reason": "after model repeated the same wrong move",
                        "human_explanation": "目前沒有足夠證據證明 retrain 改善了該錯誤，因為 after model 仍重複同一錯誤。",
                    },
                }
            ],
        },
        "deterministic_strength_snapshot": {
            "supported": True,
            "skipped": False,
            "passed": False,
            "reasons": ["final deterministic score regressed below baseline"],
            "regression_vs_baseline": -0.2,
            "regression_vs_checkpoint10": -0.1,
            "regression_vs_checkpoint20": -0.1,
            "score_table": [
                {"model_label": "baseline", "model_hash": "before", "overall_deterministic_score": 0.8, "top1_correct_rate": 0.8, "top3_contains_rate": 0.8, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0},
                {"model_label": "checkpoint@10", "model_hash": "after", "overall_deterministic_score": 0.6, "top1_correct_rate": 0.6, "top3_contains_rate": 0.6, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0},
                {"model_label": "final", "model_hash": "after", "overall_deterministic_score": 0.6, "top1_correct_rate": 0.6, "top3_contains_rate": 0.6, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0},
            ],
            "final": {"overall_deterministic_score": 0.6, "top1_correct_rate": 0.6, "top3_contains_rate": 0.6, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0, "category_score": {"mistake_retention": {"score": 0.5, "count": 1, "top1_correct_rate": 0.5, "top3_contains_rate": 0.5, "illegal_rate": 0.0, "blunder_rate": 0.0}}},
        },
        "stochastic_auxiliary_benchmark": {
            "purpose": "sanity signal only; not primary promotion evidence",
            "strength_evidence": False,
            "skipped": True,
            "skip_reason": skip_reason,
        },
        "perft": {"purpose": "move generation correctness only; not strength evidence", "strength_evidence": False, "skipped": True, "reason": "fixture"},
        "stability": {
            "catastrophic_regression": True,
            "opening_regression": 0.0,
            "tactical_regression": 0.0,
            "endgame_regression": 0.0,
            "illegal_move_delta": None,
            "blunder_rate_before": None,
            "blunder_rate_after": None,
        },
        "exp1_live_learning": {},
        "runtime_metrics": {},
        "reproducibility": {},
    }
    summary["engine_verdict"] = module._engine_verdict(summary)
    summary["promotion_gate"] = module._promotion_gate_summary(summary)
    summary["suitable_for_production_self_learning"] = summary["engine_verdict"] == "PASS" and summary["promotion_gate"]["passed"]

    engine_dir = tmp_path / "exp3"
    engine_dir.mkdir()
    module._json_dump(engine_dir / "summary.json", summary)
    module._write_engine_report(engine_dir, summary)
    root_summary = module._build_root_summary(
        output_root=tmp_path,
        summaries=[summary],
        skip_autorun_benchmark=True,
        skip_autorun_promote=True,
        skip_retrain_benchmark_snapshots=True,
    )
    module._write_root_report(tmp_path, root_summary, [summary])

    root_json = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    engine_json = json.loads((engine_dir / "summary.json").read_text(encoding="utf-8"))
    root_md = (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")
    engine_md = (engine_dir / "SUMMARY.md").read_text(encoding="utf-8")

    assert root_json["overall_verdict"] == "HIGH_RISK"
    assert root_json["engines"][0]["promotion_gate_passed"] is False
    assert engine_json["promotion_gate"]["passed"] is False
    assert "deterministic strength gate failed: final deterministic score regressed below baseline" in engine_json["promotion_gate"]["reasons"]
    assert any("mistake retention probe failed" in reason for reason in engine_json["promotion_gate"]["reasons"])
    assert "illegal moves detected in dataset" in engine_json["promotion_gate"]["reasons"]
    assert "forced repetition poison signal exceeded threshold" in engine_json["promotion_gate"]["reasons"]
    assert "## Can This Model Be Promoted?" in root_md
    assert "## Can This Model Be Promoted?" in engine_md
    assert "Stochastic Auxiliary Game Benchmark" in root_md
    assert "Stochastic Auxiliary Game Benchmark" in engine_md
    assert "strength_evidence: `False`" in engine_md
    assert "目前沒有足夠證據證明 retrain 改善了該錯誤" in engine_md
    assert module._report_consistency_issues(root_json, [engine_json], root_md, {"exp3": engine_md}) == []


def test_quick_retrain_skip_heavy_sanity_flag_is_wired():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "--quick-retrain-skip-heavy-sanity" in source
    assert "skip_heavy_sanity=bool(args.quick_retrain_skip_heavy_sanity)" in source
    assert "skip_heavy_sanity=bool(skip_heavy_sanity)" in source
    assert "def _skipped_sanity_learning_probe_heavy_skip" in source
    assert "def _skipped_semantic_interference_isolation_report" in source
    assert "def _skipped_prior_sanity_case_retention" in source
    assert "def _skipped_flank_context_feature_injection_report" in source
    assert "skipped_heavy_diagnostics" in source
    assert "heavy_sanity_skipped" in source
    assert "generalization_blocker" in source
    assert "targeted_mistake_fixed" in source
    assert "trainer_timeout" in source


def test_skipped_sanity_learning_probe_heavy_skip_shape():
    module = _load_validation_module()
    stub = module._skipped_sanity_learning_probe_heavy_skip(
        {
            "passed": True,
            "case_count": 12,
            "final_pass_rate": 0.875,
            "by_semantic": {"central_head": 0.9},
            "smoke_anchor_audit": {"checked": 3},
        }
    )

    assert stub["result_kind"] == "skipped_heavy_diagnostics"
    assert stub["heavy_sanity_skipped"] is True
    assert stub["full_gate_skipped"] is True
    assert stub["full_gate_skip_reason"] == "quick_retrain_skip_heavy_sanity"
    assert stub["seen_variant_pass_rate"] is None
    assert stub["balanced_clean_held_out_pass_rate"] is None
    assert stub["final_decision_learning"]["blocked_reason"] == "heavy_sanity_diagnostics_skipped"
    assert stub["clean_heldout_by_semantic"] == {"central_head": 0.9}


def test_skipped_semantic_interference_and_prior_retention_stubs_are_inert():
    module = _load_validation_module()
    semantic = module._skipped_semantic_interference_isolation_report()
    prior = module._skipped_prior_sanity_case_retention()
    flank = module._skipped_flank_context_feature_injection_report()

    assert semantic["skipped"] is True
    assert semantic["interference"] is False
    assert semantic["interference_reasons"] == []
    assert prior["skipped"] is True
    assert prior["failed_count"] == 0
    assert prior["learning_signal"] is None
    assert flank["skipped"] is True
    assert flank["cases"] == []


def test_promotion_gate_short_circuits_on_heavy_sanity_skip():
    module = _load_validation_module()
    summary = {
        "engine_alias": "exp4",
        "replay_summary": {"trusted_replays": module.VALID_GAMES, "quarantine_replays": module.INVALID_GAMES},
        "dataset_integrity": {"contaminated_rows": 0, "duplicate_ratio": 0.0, "invalid_fen": 0, "illegal_moves": 0, "side_mismatch": 0, "short_resign_games": 0},
        "poison_detection": {"forced_repetition_patterns": 0, "intentional_blunders": 0, "engine_copy_suspected": 0, "suspicious_resign_rate": 0.0},
        "stability": {"catastrophic_regression": False},
        "quick_retrain_gate": {"enabled": True},
        "before_after_eval": {
            "benchmark_before": {"skipped": True, "reason": "stochastic_auxiliary_disabled"},
            "benchmark_after": {"skipped": True, "reason": "stochastic_auxiliary_disabled"},
            "checkpoints": [
                {
                    "trusted_count": 10,
                    "heavy_sanity_skipped": True,
                    "trainer_timeout": False,
                    "hash_changed": True,
                    "targeted_mistake_fixed": True,
                    "broad_strength_improvement": False,
                    "generalization_blocker": "heavy_sanity_skipped",
                    "mistake_retention_probe": {"learning_signal": True, "matched_expected": True},
                    "sanity_learning_probe": {
                        "result_kind": "skipped_heavy_diagnostics",
                        "heavy_sanity_skipped": True,
                        "full_gate_skipped": True,
                        "learning_signal": False,
                        "learning_signal_reason": "full deterministic sanity probe skipped by --quick-retrain-skip-heavy-sanity",
                        "seen_variant_pass_rate": None,
                        "unseen_variant_count": 0,
                        "balanced_clean_held_out_count": 0,
                        "hard_clean_held_out_count": 0,
                        "final_decision_learning": {"learning_signal": None, "blocked_reason": "heavy_sanity_diagnostics_skipped"},
                    },
                }
            ],
        },
        "deterministic_strength_snapshot": {
            "supported": True,
            "skipped": False,
            "passed": True,
            "reasons": [],
            "score_table": [
                {"model_label": "baseline", "overall_deterministic_score": 0.8},
                {"model_label": "final", "overall_deterministic_score": 0.8},
            ],
            "final": {"overall_deterministic_score": 0.8, "illegal_rate": 0.0, "blunder_avoid_rate": 1.0},
        },
        "engine_verdict": "PARTIAL",
    }

    gate = module._promotion_gate_summary(summary)

    skip_reasons = [reason for reason in gate["reasons"] if "heavy sanity diagnostics skipped" in reason]
    assert len(skip_reasons) == 1
    assert "broad strength improvement not evaluated" in skip_reasons[0]
    cascade_reasons = [
        reason
        for reason in gate["reasons"]
        if any(
            tag in reason
            for tag in (
                "sanity seen variant pass rate",
                "sanity unseen variants missing",
                "sanity clean held-out labels",
                "sanity hard clean held-out",
                "sanity final decision learning",
            )
        )
    ]
    assert cascade_reasons == []
    assert gate["passed"] is False
