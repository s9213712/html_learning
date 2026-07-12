#!/usr/bin/env python3
"""v9.5 active-failure fine-tune training.

Builds on v9.3 (10K labels + afterstate ranking). Adds:
- 112 failure positions (Stockfish depth-6 re-labeled) from v9.3 vs
  Stockfish play. The original v9.5a used 5x oversampling and failed
  staged-10 catastrophically, so current defaults are conservative.
- Targeted ranking on failure positions: good_move = Stockfish best
  (known per position from depth-6 multipv); bad_move = v9.3's
  actual losing move. Failure ranking weight is configurable and
  defaults to the base ranking weight.

Composite loss per sample:
    1.0 * cp_huber  (target = tanh(clip(cp_white,±2000)/600), side POV)
    + 0.04 * outcome (ply-weighted)
    + ranking (afterstate, alpha configurable)

Pipeline matches v9.3 structure; main delta is the failure-aware
extra rows + per-sample ranking weight.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.chess_neural import (  # noqa: E402
    EVAL_SCALE, INPUT_DIM, NeuralWeights, active_features,
    load_weights, make_initial_weights, save_weights,
    static_baseline_cp_white,
)
from scripts.games.common_paths import exp6_artifacts_root, exp6_private_dir  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402


# v9.5 hyperparameters (same as v9.3 except ranking weights)
TRAIN_GAME_ID_RANGE = (0, 8999)
DEV_GAME_ID_RANGE = (9000, 9999)
CP_CLAMP = 2000.0
VALUE_SCALE = 600.0
HUBER_DELTA = 0.1
OUTCOME_PLY_FULL_WEIGHT_AT = 40.0
BATCH_SIZE = 64
LR = 0.0005
MOMENTUM = 0.9
GRAD_CLIP = 1.0
EPOCHS = 8
EARLY_STOP_PATIENCE = 8
SEED = 20260519
K_NEG = 3
RANK_TEMPERATURE = 0.5

# Failure handling
FAILURE_OVERSAMPLE = 1            # v9.5a used 5 and failed staged-10
FAILURE_RANK_WEIGHT = 0.15        # v9.5a used 0.30 and failed staged-10
BASE_RANK_WEIGHT = 0.15           # ranking loss weight on base rows
OUTCOME_WEIGHT = 0.04

EXP6_PRIVATE_DIR = exp6_private_dir()
LABELS_10K_PATH = EXP6_PRIVATE_DIR / "curriculum_labels_10k.jsonl"
PLAYED_10K_PATH = EXP6_PRIVATE_DIR / "played_moves_10k.jsonl"
FAILURE_PATH = EXP6_PRIVATE_DIR / "v9_5_failure_positions.jsonl"

EXP6_ARTIFACTS = exp6_artifacts_root() / "v9_5"
SNAPSHOTS_DIR = EXP6_ARTIFACTS / "snapshots"
REPORT_JSON = EXP6_ARTIFACTS / "report.json"


def _value_target_from_cp_white(cp_white: float, stm_is_white: bool) -> float:
    cp_side = cp_white if stm_is_white else -cp_white
    cp_clipped = max(-CP_CLAMP, min(CP_CLAMP, cp_side))
    return math.tanh(cp_clipped / VALUE_SCALE)


def load_played_moves(path: Path) -> dict[tuple[int, str], str]:
    out: dict[tuple[int, str], str] = {}
    with path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
                out[(int(rec["game_idx"]), rec["fen"])] = rec["played_move"]
            except Exception:
                pass
    return out


def load_base_split(played_map: dict, labels_path: Path) -> tuple[list, list]:
    train, dev = [], []
    with labels_path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            fen = rec["fen"]
            game_idx = int(rec["game_idx"])
            played = played_map.get((game_idx, fen))
            if played is None:
                continue
            cp_w = float(rec["cp_white"])
            out_w = float(rec.get("outcome_white", 0.0))
            board = chess.Board(fen)
            stm_is_white = (board.turn == chess.WHITE)
            row = {
                "fen": fen, "cp_white": cp_w, "outcome_white": out_w,
                "game_idx": game_idx, "stm_is_white": stm_is_white,
                "board": board, "played_uci": played,
                "kind": "base",
            }
            if TRAIN_GAME_ID_RANGE[0] <= game_idx <= TRAIN_GAME_ID_RANGE[1]:
                train.append(row)
            elif DEV_GAME_ID_RANGE[0] <= game_idx <= DEV_GAME_ID_RANGE[1]:
                dev.append(row)
    return train, dev


def load_failure_rows() -> list[dict]:
    if not FAILURE_PATH.exists():
        print(f"  failure file missing: {FAILURE_PATH}")
        return []
    rows = []
    with FAILURE_PATH.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            board = chess.Board(rec["fen"])
            rows.append({
                "fen": rec["fen"],
                "cp_white": float(rec["cp_white"]),
                "outcome_white": float(rec.get("outcome_white", 0.0)),
                "stm_is_white": (board.turn == chess.WHITE),
                "board": board,
                # Use Stockfish best as the "good" played_move; v9.3's actual
                # play is recorded for ranking as the explicit BAD move.
                "played_uci": rec.get("stockfish_best_move"),
                "exp6_bad_uci": rec.get("exp6_played_move"),
                "kind": "failure",
            })
    return rows


def board_features_idx(board: chess.Board) -> np.ndarray:
    return np.asarray(active_features(board), dtype=np.int64)


def empty_precomputed() -> dict:
    return {
        "features_idx": [],
        "baselines": np.zeros(0, dtype=np.float32),
        "stm": np.zeros(0, dtype=bool),
        "cp_target": np.zeros(0, dtype=np.float32),
        "outcome_target": np.zeros(0, dtype=np.float32),
        "ply_weight": np.zeros(0, dtype=np.float32),
        "ranking_weight": np.zeros(0, dtype=np.float32),
        "played_uci": [],
        "explicit_bad": [],
        "kinds": [],
        "boards": [],
    }


def precompute(rows: list[dict], *, base_rank_weight: float = BASE_RANK_WEIGHT,
               failure_rank_weight: float = FAILURE_RANK_WEIGHT) -> dict:
    n = len(rows)
    features_idx = []
    baselines = np.zeros(n, dtype=np.float32)
    stm = np.zeros(n, dtype=bool)
    cp_targets = np.zeros(n, dtype=np.float32)
    outcome_targets = np.zeros(n, dtype=np.float32)
    ply_weights = np.zeros(n, dtype=np.float32)
    ranking_weights = np.zeros(n, dtype=np.float32)
    played_ucis: list[str | None] = []
    explicit_bad: list[str | None] = []
    kinds: list[str] = []
    for i, r in enumerate(rows):
        board = r["board"]
        features_idx.append(board_features_idx(board))
        baselines[i] = static_baseline_cp_white(board)
        stm[i] = r["stm_is_white"]
        cp_targets[i] = _value_target_from_cp_white(r["cp_white"], r["stm_is_white"])
        out_side = r["outcome_white"] if r["stm_is_white"] else -r["outcome_white"]
        outcome_targets[i] = out_side
        ply = 2 * (board.fullmove_number - 1) + (0 if board.turn == chess.WHITE else 1)
        ply_weights[i] = min(1.0, ply / OUTCOME_PLY_FULL_WEIGHT_AT)
        ranking_weights[i] = failure_rank_weight if r["kind"] == "failure" else base_rank_weight
        played_ucis.append(r.get("played_uci"))
        explicit_bad.append(r.get("exp6_bad_uci"))
        kinds.append(r["kind"])
    return {
        "features_idx": features_idx, "baselines": baselines, "stm": stm,
        "cp_target": cp_targets, "outcome_target": outcome_targets,
        "ply_weight": ply_weights, "ranking_weight": ranking_weights,
        "played_uci": played_ucis, "explicit_bad": explicit_bad,
        "kinds": kinds, "boards": [r["board"] for r in rows],
    }


def huber(err: np.ndarray, delta: float = HUBER_DELTA) -> np.ndarray:
    abs_err = np.abs(err)
    quad_mask = abs_err <= delta
    out = np.zeros_like(err)
    out[quad_mask] = 0.5 * err[quad_mask] ** 2
    out[~quad_mask] = delta * (abs_err[~quad_mask] - 0.5 * delta)
    return out


def huber_grad(err: np.ndarray, delta: float = HUBER_DELTA) -> np.ndarray:
    return np.clip(err, -delta, delta)


def _forward_full(X: np.ndarray, baselines: np.ndarray, stm: np.ndarray,
                   weights: NeuralWeights):
    pre1, h1, pre2, h2, out = cc._forward(X, weights)
    nn_residual = out[:, 0] * EVAL_SCALE
    cp_white = nn_residual + baselines
    sign = np.where(stm, 1.0, -1.0).astype(np.float32)
    cp_side = cp_white * sign
    v_pred = np.tanh(cp_side / VALUE_SCALE)
    return pre1, h1, pre2, h2, out, v_pred, cp_white


def densify(features_list, indices: np.ndarray) -> np.ndarray:
    X = np.zeros((indices.size, INPUT_DIM), dtype=np.float32)
    for r, src in enumerate(indices):
        X[r, features_list[src]] = 1.0
    return X


def make_after_state(board: chess.Board, move: chess.Move) -> tuple[np.ndarray, float, bool]:
    board.push(move)
    feats = board_features_idx(board)
    bl = static_baseline_cp_white(board)
    stm_w = (board.turn == chess.WHITE)
    board.pop()
    return feats, float(bl), bool(stm_w)


def evaluate_dev(weights, dev_data) -> dict:
    n = len(dev_data["features_idx"])
    if n == 0:
        return {}
    all_v = np.zeros(n, dtype=np.float32)
    all_cp = np.zeros(n, dtype=np.float32)
    for start in range(0, n, BATCH_SIZE):
        end = min(start + BATCH_SIZE, n)
        idx = np.arange(start, end)
        X = densify(dev_data["features_idx"], idx)
        _, _, _, _, _, v, cp_w = _forward_full(X, dev_data["baselines"][idx], dev_data["stm"][idx], weights)
        sign = np.where(dev_data["stm"][idx], 1.0, -1.0)
        all_v[idx] = v
        all_cp[idx] = cp_w * sign
    err = all_v - dev_data["cp_target"]
    huber_loss = float(huber(err).mean())
    sign_acc = float(((all_v > 0) == (dev_data["cp_target"] > 0)).mean())
    target_cp_side = np.arctanh(np.clip(dev_data["cp_target"], -0.999, 0.999)) * VALUE_SCALE
    if n > 1 and target_cp_side.std() > 1e-6:
        cp_corr = float(np.corrcoef(all_cp, target_cp_side)[0, 1])
    else:
        cp_corr = 0.0
    avg_cp_err = float(np.abs(all_cp - target_cp_side).mean())
    return {"n": n, "huber_value_loss": huber_loss, "sign_accuracy": sign_acc,
            "cp_correlation": cp_corr, "avg_abs_cp_err": avg_cp_err}


def train_loop(weights, train_data, dev_data, failure_rows, *, epochs, lr, momentum,
               batch_size, patience, k_neg, outcome_weight, oversample):
    """Mix base train + oversampled failure rows. Each batch may
    contain both kinds; ranking_weight differs per sample.
    """
    n_base = len(train_data["features_idx"])
    n_failure = len(failure_rows["features_idx"]) if failure_rows else 0
    # Replicate failure indices oversample times
    failure_indices_expanded = []
    if n_failure:
        for _ in range(oversample):
            failure_indices_expanded.extend(range(n_failure))
    # Merge into single index space: 0..n_base-1 are base rows, then
    # we tag failure rows by negative offsets — simpler: process base
    # and failure in alternating batches but with per-row ranking_weight.
    # Use combined dict approach.
    combined_features = train_data["features_idx"] + failure_rows["features_idx"]
    combined_baselines = np.concatenate([train_data["baselines"], failure_rows["baselines"]])
    combined_stm = np.concatenate([train_data["stm"], failure_rows["stm"]])
    combined_cp = np.concatenate([train_data["cp_target"], failure_rows["cp_target"]])
    combined_out = np.concatenate([train_data["outcome_target"], failure_rows["outcome_target"]])
    combined_plw = np.concatenate([train_data["ply_weight"], failure_rows["ply_weight"]])
    combined_rkw = np.concatenate([train_data["ranking_weight"], failure_rows["ranking_weight"]])
    combined_played = train_data["played_uci"] + failure_rows["played_uci"]
    combined_explicit_bad = train_data["explicit_bad"] + failure_rows["explicit_bad"]
    combined_boards = train_data["boards"] + failure_rows["boards"]
    # Sampling index list: base indices each once, failure indices replicated
    base_indices = list(range(n_base))
    failure_indices_offset = [n_base + i for i in failure_indices_expanded]
    all_indices = base_indices + failure_indices_offset
    if not all_indices:
        raise ValueError("no training rows: enable base training or provide failure rows")
    print(f"  combined: {len(base_indices)} base + {len(failure_indices_offset)} failure (oversampled) = {len(all_indices)} samples/epoch", flush=True)

    rng_np = np.random.default_rng(SEED)
    sample_rng = random.Random(SEED + 1)
    vel = {name: np.zeros_like(getattr(weights, name)) for name in ("W1", "b1", "W2", "b2", "W3", "b3")}
    history = []
    best_loss = float("inf")
    best_weights = None
    stale = 0
    for epoch in range(epochs):
        order = list(all_indices)
        sample_rng.shuffle(order)
        order = np.array(order, dtype=np.int64)
        ep_cp_sum = 0.0; ep_out_sum = 0.0; ep_rank_sum = 0.0
        ep_rank_acc_sum = 0.0; rank_pairs = 0; samples = 0
        for start in range(0, len(order), batch_size):
            idx_arr = order[start:start + batch_size]
            B = idx_arr.size
            X_cur = densify(combined_features, idx_arr)
            bl_cur = combined_baselines[idx_arr]
            stm_cur = combined_stm[idx_arr]
            cp_tgt = combined_cp[idx_arr]
            out_tgt = combined_out[idx_arr]
            plw = combined_plw[idx_arr]
            rkw = combined_rkw[idx_arr]
            pre1, h1, pre2, h2, out_cur, v_cur, _ = _forward_full(X_cur, bl_cur, stm_cur, weights)
            # cp loss
            err_cp = v_cur - cp_tgt
            ep_cp_sum += float(huber(err_cp).sum())
            huber_d = huber_grad(err_cp)
            # outcome loss
            err_out = v_cur - out_tgt
            plw_err = plw * err_out
            ep_out_sum += float((plw_err * err_out).sum()) * 0.5
            d_out_grad = plw_err
            samples += B

            # Build afterstate ranking batch
            after_features = []
            after_bls = np.zeros(B * (1 + k_neg), dtype=np.float32)
            after_stms = np.zeros(B * (1 + k_neg), dtype=bool)
            slot_valid = np.zeros((B, k_neg), dtype=bool)
            for b, src in enumerate(idx_arr):
                board = combined_boards[src]
                played_uci = combined_played[src]
                explicit_bad = combined_explicit_bad[src]
                try:
                    played_mv = chess.Move.from_uci(played_uci) if played_uci else None
                except Exception:
                    played_mv = None
                legal = list(board.legal_moves)
                if played_mv is None or played_mv not in legal:
                    # fallback to current board features (will produce zero margin)
                    feats = board_features_idx(board); bl = static_baseline_cp_white(board); stm = (board.turn == chess.WHITE)
                else:
                    feats, bl, stm = make_after_state(board, played_mv)
                after_features.append(feats)
                slot_played = b * (1 + k_neg)
                after_bls[slot_played] = bl
                after_stms[slot_played] = stm
                # Bad moves: prefer explicit_bad for failure rows
                bad_candidates: list[chess.Move] = []
                if explicit_bad:
                    try:
                        bm = chess.Move.from_uci(explicit_bad)
                        if bm in legal and bm != played_mv:
                            bad_candidates.append(bm)
                    except Exception:
                        pass
                # Pad with random legal alts
                remaining_alts = [m for m in legal if m != played_mv and m not in bad_candidates]
                if remaining_alts:
                    take = min(k_neg - len(bad_candidates), len(remaining_alts))
                    bad_candidates.extend(sample_rng.sample(remaining_alts, take))
                # Fill slot_valid + features
                for k in range(k_neg):
                    slot = b * (1 + k_neg) + 1 + k
                    if k < len(bad_candidates):
                        alt = bad_candidates[k]
                        af, ab, asw = make_after_state(board, alt)
                        after_features.append(af)
                        after_bls[slot] = ab
                        after_stms[slot] = asw
                        slot_valid[b, k] = True
                    else:
                        # pad with played's after-state
                        after_features.append(feats)
                        after_bls[slot] = bl
                        after_stms[slot] = stm
            X_after = densify(after_features, np.arange(len(after_features)))
            _ap1, _ah1, _ap2, _ah2, out_after, v_after, _ = _forward_full(X_after, after_bls, after_stms, weights)
            v_after_grp = v_after.reshape(B, 1 + k_neg)
            scores = -v_after_grp
            played_scores = scores[:, 0:1]
            alt_scores = scores[:, 1:]
            diffs = (played_scores - alt_scores) / RANK_TEMPERATURE
            valid_mask = slot_valid.astype(np.float32)
            rank_loss_per = np.logaddexp(0.0, -diffs)
            masked = rank_loss_per * valid_mask * rkw[:, None]  # per-sample ranking weight
            n_valid = float(valid_mask.sum())
            ep_rank_sum += float(masked.sum())
            ep_rank_acc_sum += float(((diffs > 0).astype(np.float32) * valid_mask).sum())
            rank_pairs += n_valid

            # Gradients
            tanh_d_cur = 1.0 - v_cur ** 2
            sign_cur = np.where(stm_cur, 1.0, -1.0).astype(np.float32)
            d_cp_d_out = huber_d * tanh_d_cur * sign_cur * (EVAL_SCALE / VALUE_SCALE)
            d_out_d_out = d_out_grad * tanh_d_cur * sign_cur * (EVAL_SCALE / VALUE_SCALE)
            if n_valid > 0:
                sig = 1.0 / (1.0 + np.exp(diffs))
                sig_masked = -sig * valid_mask
                grad_scale_per_sample = rkw[:, None] / n_valid  # per-sample (B,1) broadcast (B, k_neg)
                d_score_played = (sig_masked * grad_scale_per_sample).sum(axis=1, keepdims=True) * (1.0 / RANK_TEMPERATURE)
                d_score_alt = -sig_masked * grad_scale_per_sample * (1.0 / RANK_TEMPERATURE)
                d_v_after_played = -d_score_played
                d_v_after_alt = -d_score_alt
                d_v_after = np.concatenate([d_v_after_played, d_v_after_alt], axis=1).reshape(-1)
                tanh_d_after = 1.0 - v_after ** 2
                sign_after = np.where(after_stms, 1.0, -1.0).astype(np.float32)
                d_rank_d_out_after = d_v_after * tanh_d_after * sign_after * (EVAL_SCALE / VALUE_SCALE)
            else:
                d_rank_d_out_after = None

            d_loss_d_out_cur = d_cp_d_out + outcome_weight * d_out_d_out
            y_eff_cur = out_cur[:, 0] - d_loss_d_out_cur * (B / 2.0)
            grads_cur = cc._backward(X_cur, y_eff_cur, weights, pre1, h1, pre2, h2, out_cur)
            if d_rank_d_out_after is not None:
                N_after = X_after.shape[0]
                y_eff_after = out_after[:, 0] - d_rank_d_out_after * (N_after / 2.0)
                grads_after = cc._backward(X_after, y_eff_after, weights, _ap1, _ah1, _ap2, _ah2, out_after)
                total_grads = tuple(g_cur + g_aft for g_cur, g_aft in zip(grads_cur, grads_after))
            else:
                total_grads = grads_cur
            for name, grad in zip(("W1", "b1", "W2", "b2", "W3", "b3"), total_grads):
                norm = float(np.linalg.norm(grad))
                if norm > GRAD_CLIP:
                    grad = grad * (GRAD_CLIP / norm)
                vel[name] = momentum * vel[name] - lr * grad
                getattr(weights, name)[...] += vel[name]
        mean_cp = ep_cp_sum / samples if samples else 0.0
        mean_out = ep_out_sum / samples if samples else 0.0
        mean_rank = ep_rank_sum / rank_pairs if rank_pairs else 0.0
        rank_pair_acc = ep_rank_acc_sum / rank_pairs if rank_pairs else 0.0
        dev_metrics = evaluate_dev(weights, dev_data)
        combined_dev = dev_metrics.get("huber_value_loss", float("inf"))
        improved = combined_dev < best_loss - 1e-5
        if improved:
            best_loss = combined_dev
            best_weights = NeuralWeights(
                W1=weights.W1.copy(), b1=weights.b1.copy(),
                W2=weights.W2.copy(), b2=weights.b2.copy(),
                W3=weights.W3.copy(), b3=weights.b3.copy(),
                side_to_move_bias_cp=weights.side_to_move_bias_cp,
            )
            stale = 0
        else:
            stale += 1
        tag = "✓best" if improved else f"stale{stale}"
        history.append({
            "epoch": epoch + 1, "cp": mean_cp, "out": mean_out, "rank": mean_rank,
            "train_rank_acc": rank_pair_acc, "dev": dev_metrics,
        })
        print(f"  epoch {epoch+1:2d}/{epochs}: cp={mean_cp:.4f} out={mean_out:.4f} rank={mean_rank:.4f}  "
              f"dev_huber={dev_metrics.get('huber_value_loss',0):.4f} dev_sign={dev_metrics.get('sign_accuracy',0):.3f}  {tag}", flush=True)
        if stale >= patience:
            print(f"  early stop at epoch {epoch+1}", flush=True)
            break
    return history, best_weights if best_weights is not None else weights


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-weights", type=Path, required=True,
                    help="Starting weights: v6.2 S2 for v9.5a, v9.3 for v9.5b")
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    ap.add_argument("--lr", type=float, default=LR)
    ap.add_argument("--patience", type=int, default=EARLY_STOP_PATIENCE)
    ap.add_argument("--oversample", type=int, default=FAILURE_OVERSAMPLE)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--k-neg", type=int, default=K_NEG)
    ap.add_argument("--outcome-weight", type=float, default=OUTCOME_WEIGHT)
    ap.add_argument("--base-rank-weight", type=float, default=BASE_RANK_WEIGHT)
    ap.add_argument("--failure-rank-weight", type=float, default=FAILURE_RANK_WEIGHT)
    ap.add_argument("--snapshot-dir", type=Path, default=SNAPSHOTS_DIR)
    ap.add_argument("--report-json", type=Path, default=REPORT_JSON)
    ap.add_argument("--failure-only", action="store_true",
                    help="Train only on failure rows; useful for tiny final fine-tunes from v9.3")
    ap.add_argument("--skip-dev", action="store_true",
                    help="Skip 10K dev loading/evaluation for quick failure-only probes")
    ap.add_argument("--out-name", required=True, help="snapshot filename (e.g., v9_5a_best.npz)")
    args = ap.parse_args()
    args.oversample = max(0, args.oversample)
    args.k_neg = max(1, args.k_neg)

    args.snapshot_dir.mkdir(parents=True, exist_ok=True)

    print(f"loading failure positions from {FAILURE_PATH}...", flush=True)
    failure_rows_raw = load_failure_rows()
    print(f"  {len(failure_rows_raw)} failure rows", flush=True)

    train_rows: list[dict] = []
    dev_rows: list[dict] = []
    if not args.failure_only or not args.skip_dev:
        print(f"loading 10K played moves...", flush=True)
        t0 = time.perf_counter()
        played_map = load_played_moves(PLAYED_10K_PATH)
        print(f"  {len(played_map)} entries ({time.perf_counter()-t0:.1f}s)", flush=True)

        print(f"loading 10K labels + split...", flush=True)
        t0 = time.perf_counter()
        train_rows, dev_rows = load_base_split(played_map, LABELS_10K_PATH)
        if args.failure_only:
            train_rows = []
        print(f"  base train={len(train_rows)}, dev={len(dev_rows)} ({time.perf_counter()-t0:.1f}s)", flush=True)

    print(f"precomputing features...", flush=True)
    t0 = time.perf_counter()
    train_data = precompute(
        train_rows,
        base_rank_weight=args.base_rank_weight,
        failure_rank_weight=args.failure_rank_weight,
    ) if train_rows else empty_precomputed()
    dev_data = precompute(
        dev_rows,
        base_rank_weight=args.base_rank_weight,
        failure_rank_weight=args.failure_rank_weight,
    ) if dev_rows else empty_precomputed()
    failure_data = precompute(
        failure_rows_raw,
        base_rank_weight=args.base_rank_weight,
        failure_rank_weight=args.failure_rank_weight,
    ) if failure_rows_raw else empty_precomputed()
    print(f"  precomputed ({time.perf_counter()-t0:.1f}s)", flush=True)

    if args.seed_weights.exists():
        weights = load_weights(args.seed_weights)
        print(f"  warm-start from {args.seed_weights}", flush=True)
    else:
        weights = make_initial_weights(seed=SEED)
        print(f"  fresh random init", flush=True)

    initial = evaluate_dev(weights, dev_data)
    if initial:
        print(f"\ninitial dev:  huber={initial['huber_value_loss']:.4f} sign={initial['sign_accuracy']:.3f} "
              f"cp_corr={initial['cp_correlation']:.3f} avg_cp_err={initial['avg_abs_cp_err']:.1f}", flush=True)
    else:
        print("\ninitial dev:  skipped", flush=True)

    history, best_weights = train_loop(
        weights, train_data, dev_data, failure_data,
        epochs=args.epochs, lr=args.lr, momentum=MOMENTUM, batch_size=args.batch_size,
        patience=args.patience, k_neg=args.k_neg, outcome_weight=args.outcome_weight,
        oversample=args.oversample,
    )
    final = evaluate_dev(best_weights, dev_data)
    if final:
        print(f"\nfinal best dev: huber={final['huber_value_loss']:.4f} sign={final['sign_accuracy']:.3f} "
              f"cp_corr={final['cp_correlation']:.3f} avg_cp_err={final['avg_abs_cp_err']:.1f}", flush=True)
    else:
        print("\nfinal best dev: skipped", flush=True)

    snap = args.snapshot_dir / args.out_name
    save_weights(snap, best_weights)
    print(f"saved -> {snap}", flush=True)

    print(f"\nstaged-10 sanity check:", flush=True)
    results = cc.play_staged_test(snap)
    summ = cc.score_summary(results); sr = cc.score_rate(results)
    print(f"  {summ['W']}W/{summ['D']}D/{summ['L']}L score={summ['total_score']:+d}/{summ['max_possible_score']} (norm={sr:.2%})", flush=True)

    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps({
        "config": {"lr": args.lr, "epochs": args.epochs, "oversample": args.oversample,
                   "seed_weights": str(args.seed_weights), "out_name": args.out_name,
                   "batch_size": args.batch_size, "k_neg": args.k_neg,
                   "outcome_weight": args.outcome_weight,
                   "base_rank_weight": args.base_rank_weight,
                   "failure_rank_weight": args.failure_rank_weight,
                   "failure_only": args.failure_only, "skip_dev": args.skip_dev,
                   "snapshot_dir": str(args.snapshot_dir)},
        "initial_dev": initial,
        "final_dev": final,
        "history": history,
        "staged_10": {"W": summ["W"], "D": summ["D"], "L": summ["L"],
                       "score_total": summ["total_score"], "norm": sr},
    }, indent=2))
    print(f"\nreport -> {args.report_json}", flush=True)
    print(f"NOTE: v9.5 — run chess_exp6_match.py vs v6.2 S2 (Gate 1) AND check staged ≥ -25 (Gate 2).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
