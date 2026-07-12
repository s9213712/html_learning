# chess_exp5_strength_gate

- candidate_model_path: `docs/games/2026-05-13_exp5_normal_retrain_smoke_v11/repeatability_gate/run_1_seed_31/candidate/chess_experiment_5_nnue.json`
- baseline_model_path: `services/games/models/chess_experiment_5_nnue.json`
- pass: `False`
- baseline_score: `0.388889`
- candidate_score: `0.388889`
- score_delta: `0.0`
- case_pass_rate: `0.3889`
- benchmark_gate_pass: `False`

## Standard Policy

- Exp5 reuses common safety floors: legal play, suspicious benchmark guard, score-rate floor when benchmark is provided.
- Exp5 does not reuse exp3 semantic replay/promotion evidence.
- Exp5 adds deterministic NNUE/PVS case checks and candidate-vs-baseline rank traces.

## Case Decision Diff Snapshot

- case_count: `18`
- case_pass_rate: `0.3889`
- deterministic_improvement: `0.0`

## Train Row Learning

- enabled: `True`
- train_rows: `31`
- baseline_teacher_agreement_on_train: `0.451613`
- candidate_teacher_agreement_on_train: `0.548387`
- train_agreement_delta: `0.096774`
- retrain_effect_not_visible: `False`
- learned_train_not_generalized: `True`

## Leak Guard

- train_rows: `31`
- held_out_rows: `14`
- overlap_count: `0`
- held_out_in_training: `False`
