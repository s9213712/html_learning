#!/usr/bin/env python3
"""Exp6 v7.3 Phase 3: afterstate move-ranking loss.

Per user spec (revised again post v7.1/v7.2 reject):

  Goal: train so that for each position s, the PLAYED move m_good
  produces a better afterstate (per V) than sampled alternative
  legal moves. This directly aligns with what search uses:

    score(move a from s) = -V(after(s, a))
    best_move = argmax_a score(a)

  Loss (logistic ranking, smoother than hinge):

    L_rank = log(1 + exp(-(score_good - score_bad) / temp))

  Composite training loss (per user):

    L = 1.0  * cp_huber       (anchor — value still tracks Stockfish)
      + 0.04 * outcome        (small aux — final result signal)
      + 0.15 * ranking        (NEW — direct move-quality alignment)

  Seed: v6.2 S2 (the current champion). v7.1/v7.2 were both REJECTED
  in match-gate, so do NOT warm-start from them.

  Match-gating remains: candidate vs v6.2 S2 ≥ 52% to promote.

Negative-move sampling (initial cut): random legal moves excluding
the played move. The user noted Stockfish-multipv-filtered negatives
would be cleaner; that's a follow-up if random negatives already help.
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


TRAIN_GAME_ID_RANGE = (0, 899)
DEV_GAME_ID_RANGE = (900, 999)
CP_CLAMP = 2000.0
VALUE_SCALE = 600.0
HUBER_DELTA = 0.1
OUTCOME_PLY_FULL_WEIGHT_AT = 40.0
BATCH_SIZE = 64                  # smaller batch — each step does 1+1+K forward passes
LR = 0.0005                       # smaller LR because composite gradient larger
MOMENTUM = 0.9
GRAD_CLIP = 1.0
EPOCHS = 40
EARLY_STOP_PATIENCE = 8
SEED = 20260518

K_NEGATIVES = 3                  # alternative legal moves sampled per position
RANK_TEMPERATURE = 0.5           # logistic ranking softness in value space

PLAYED_MOVES_PATH = exp6_private_dir() / "played_moves.jsonl"
# v9.3 override targets — pass via --labels / --played-moves at runtime.
PERSISTENT_DIR = exp6_artifacts_root() / "v7_3"
SNAPSHOTS_DIR = PERSISTENT_DIR / "snapshots"
REPORT_JSON = PERSISTENT_DIR / "report.json"


def _value_target_from_cp_white(cp_white: float, stm_is_white: bool) -> float:
    cp_side = cp_white if stm_is_white else -cp_white
    cp_clipped = max(-CP_CLAMP, min(CP_CLAMP, cp_side))
    return math.tanh(cp_clipped / VALUE_SCALE)


def load_played_moves(path: Path = PLAYED_MOVES_PATH) -> dict[tuple[int, str], str]:
    """Returns {(game_idx, fen_before): played_move_uci}."""
    if not path.exists():
        raise FileNotFoundError(f"missing {path}; run chess_exp6_extract_played_moves.py first")
    out: dict[tuple[int, str], str] = {}
    with path.open() as f:
        for line in f:
            rec = json.loads(line)
            out[(int(rec["game_idx"]), rec["fen"])] = rec["played_move"]
    return out


def load_split(played_map: dict, labels_path: Path | None = None) -> tuple[list, list]:
    labels_path = labels_path or cc.LABELS_PATH
    if not labels_path.exists():
        raise FileNotFoundError(f"Labels cache missing: {labels_path}")
    train, dev = [], []
    n_no_move = 0
    with labels_path.open() as f:
        for line in f:
            rec = json.loads(line)
            fen = rec["fen"]
            game_idx = int(rec["game_idx"])
            played = played_map.get((game_idx, fen))
            if played is None:
                # No matching played move for this label (e.g., last move's fen_after
                # wasn't followed by another move). Skip.
                n_no_move += 1
                continue
            cp_w = float(rec["cp_white"])
            out_w = float(rec.get("outcome_white", 0.0))
            board = chess.Board(fen)
            stm_is_white = (board.turn == chess.WHITE)
            row = (fen, cp_w, out_w, game_idx, stm_is_white, board, played)
            if TRAIN_GAME_ID_RANGE[0] <= game_idx <= TRAIN_GAME_ID_RANGE[1]:
                train.append(row)
            elif DEV_GAME_ID_RANGE[0] <= game_idx <= DEV_GAME_ID_RANGE[1]:
                dev.append(row)
    if n_no_move:
        print(f"  skipped {n_no_move} labels with no matching played_move", flush=True)
    return train, dev


def board_features_array(board: chess.Board) -> np.ndarray:
    """Sparse-to-dense one-hot for one board."""
    x = np.zeros(INPUT_DIM, dtype=np.float32)
    for idx in active_features(board):
        x[idx] = 1.0
    return x


def precompute_features(rows: list) -> dict:
    """Returns dict with:
      "current_features": list[np.ndarray]  (sparse indices)
      "current_baseline_white": np.ndarray  (cp baseline of current pos)
      "current_stm_white": np.ndarray (bool)
      "cp_value_target": np.ndarray  (tanh-scaled side-to-move value)
      "outcome_target": np.ndarray  (side-to-move outcome ∈ {-1,0,+1})
      "ply_weight": np.ndarray
      "played_move_uci": list[str]   (for sampling alternatives on the fly)
      "boards": list[chess.Board]   (for live sampling)
    """
    n = len(rows)
    current_features = []
    baselines = np.zeros(n, dtype=np.float32)
    stm = np.zeros(n, dtype=bool)
    cp_targets = np.zeros(n, dtype=np.float32)
    outcome_targets = np.zeros(n, dtype=np.float32)
    ply_weights = np.zeros(n, dtype=np.float32)
    played_moves: list[str] = []
    boards: list[chess.Board] = []
    for i, (fen, cp_w, out_w, gid, stm_is_white, board, played) in enumerate(rows):
        current_features.append(np.asarray(active_features(board), dtype=np.int64))
        baselines[i] = static_baseline_cp_white(board)
        stm[i] = stm_is_white
        cp_targets[i] = _value_target_from_cp_white(cp_w, stm_is_white)
        out_side = out_w if stm_is_white else -out_w
        outcome_targets[i] = out_side
        ply = 2 * (board.fullmove_number - 1) + (0 if board.turn == chess.WHITE else 1)
        ply_weights[i] = min(1.0, ply / OUTCOME_PLY_FULL_WEIGHT_AT)
        played_moves.append(played)
        boards.append(board)
    return {
        "current_features": current_features,
        "baselines": baselines,
        "stm": stm,
        "cp_target": cp_targets,
        "outcome_target": outcome_targets,
        "ply_weight": ply_weights,
        "played_move_uci": played_moves,
        "boards": boards,
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


def _forward_full(X: np.ndarray, baseline_cp_white: np.ndarray, stm_is_white: np.ndarray,
                   weights: NeuralWeights):
    """Returns (pre1, h1, pre2, h2, out, value_stm_pred, cp_white_pred)."""
    pre1, h1, pre2, h2, out = cc._forward(X, weights)
    nn_residual_cp_white = out[:, 0] * EVAL_SCALE
    cp_pred_white = nn_residual_cp_white + baseline_cp_white
    sign = np.where(stm_is_white, 1.0, -1.0).astype(np.float32)
    cp_pred_side = cp_pred_white * sign
    value_pred = np.tanh(cp_pred_side / VALUE_SCALE)
    return pre1, h1, pre2, h2, out, value_pred, cp_pred_white


def make_after_state(board: chess.Board, move: chess.Move) -> tuple[np.ndarray, float, bool]:
    """Returns (features_idx, baseline_cp_white, stm_is_white) for the
    state AFTER pushing ``move`` on ``board``. Caller is responsible
    for not mutating ``board`` (we push+pop)."""
    board.push(move)
    feats = np.asarray(active_features(board), dtype=np.int64)
    bl = static_baseline_cp_white(board)
    stm_w = (board.turn == chess.WHITE)
    board.pop()
    return feats, float(bl), bool(stm_w)


def densify(features_list: list[np.ndarray], indices: np.ndarray) -> np.ndarray:
    X = np.zeros((indices.size, INPUT_DIM), dtype=np.float32)
    for r, src in enumerate(indices):
        X[r, features_list[src]] = 1.0
    return X


def evaluate_dev(weights: NeuralWeights, dev_data: dict) -> dict:
    n = len(dev_data["current_features"])
    if n == 0:
        return {}
    all_v = np.zeros(n, dtype=np.float32)
    all_cp = np.zeros(n, dtype=np.float32)
    for start in range(0, n, BATCH_SIZE):
        end = min(start + BATCH_SIZE, n)
        idx = np.arange(start, end)
        X = densify(dev_data["current_features"], idx)
        _, _, _, _, _, v_pred, cp_pred_white = _forward_full(
            X, dev_data["baselines"][idx], dev_data["stm"][idx], weights
        )
        sign = np.where(dev_data["stm"][idx], 1.0, -1.0)
        all_v[idx] = v_pred
        all_cp[idx] = cp_pred_white * sign  # cp from side-to-move POV
    err_v = all_v - dev_data["cp_target"]
    huber_loss = float(huber(err_v).mean())
    sign_acc = float(((all_v > 0) == (dev_data["cp_target"] > 0)).mean())
    dev_cp_side = np.where(dev_data["stm"], 1.0, -1.0) * 0  # placeholder
    # cp_target is tanh-scaled; for cp_corr we need raw cp from side-to-move POV.
    # Reconstruct from cp_target via atanh (within clipping bounds).
    # Approx: target_cp_side_raw = atanh(cp_target) * VALUE_SCALE
    safe_cp_target = np.clip(dev_data["cp_target"], -0.999, 0.999)
    dev_cp_side_raw = np.arctanh(safe_cp_target) * VALUE_SCALE
    if n > 1 and dev_cp_side_raw.std() > 1e-6:
        cp_corr = float(np.corrcoef(all_cp, dev_cp_side_raw)[0, 1])
    else:
        cp_corr = 0.0
    avg_delta_cp = float(np.abs(all_cp - dev_cp_side_raw).mean())
    return {
        "n": n,
        "huber_value_loss": huber_loss,
        "sign_accuracy": sign_acc,
        "cp_correlation": cp_corr,
        "avg_abs_cp_err": avg_delta_cp,
    }


def evaluate_ranking_on_dev(weights: NeuralWeights, dev_data: dict,
                            *, n_samples: int = 2000, k_neg: int = K_NEGATIVES,
                            rng_seed: int = 99) -> dict:
    """Sample n_samples positions from dev; for each, compute V(after_played)
    vs V(after_alt) for k_neg random alternatives. Report how often
    played afterstate gives a better score than alternative.
    """
    n = len(dev_data["boards"])
    if n == 0:
        return {}
    rng = random.Random(rng_seed)
    chosen = rng.sample(range(n), min(n_samples, n))
    n_compared = 0
    n_correct = 0
    total_margin = 0.0
    for i in chosen:
        board = dev_data["boards"][i]
        played_uci = dev_data["played_move_uci"][i]
        try:
            played_mv = chess.Move.from_uci(played_uci)
        except Exception:
            continue
        legal = list(board.legal_moves)
        if played_mv not in legal:
            continue
        alts = [m for m in legal if m != played_mv]
        if not alts:
            continue
        sampled_alts = rng.sample(alts, min(k_neg, len(alts)))
        # Forward pass on after_played + after_alts
        # after_played:
        ap_feats, ap_bl, ap_stm = make_after_state(board, played_mv)
        # alts
        alt_feats_list = []
        alt_bls = []
        alt_stms = []
        for alt in sampled_alts:
            af_feats, af_bl, af_stm = make_after_state(board, alt)
            alt_feats_list.append(af_feats)
            alt_bls.append(af_bl)
            alt_stms.append(af_stm)
        all_feats = [ap_feats] + alt_feats_list
        all_bls = np.array([ap_bl] + alt_bls, dtype=np.float32)
        all_stms = np.array([ap_stm] + alt_stms, dtype=bool)
        X = densify(all_feats, np.arange(len(all_feats)))
        _, _, _, _, _, v_after, _ = _forward_full(X, all_bls, all_stms, weights)
        # Score from current side POV: score = -V(after) because after-state side flipped
        scores = -v_after
        played_score = scores[0]
        for alt_score in scores[1:]:
            n_compared += 1
            margin = float(played_score - alt_score)
            total_margin += margin
            if margin > 0:
                n_correct += 1
    return {
        "ranking_pair_acc": n_correct / n_compared if n_compared else 0.0,
        "ranking_avg_margin": total_margin / n_compared if n_compared else 0.0,
        "ranking_pairs": n_compared,
    }


def train_loop(weights: NeuralWeights, train_data: dict, dev_data: dict, *,
               epochs: int, lr: float, momentum: float, batch_size: int,
               patience: int, outcome_weight: float, ranking_weight: float,
               k_neg: int) -> tuple[list[dict], NeuralWeights]:
    n = len(train_data["current_features"])
    rng = np.random.default_rng(SEED)
    sample_rng = random.Random(SEED + 1)
    vel = {name: np.zeros_like(getattr(weights, name)) for name in ("W1", "b1", "W2", "b2", "W3", "b3")}
    history = []
    best_dev_loss = float("inf")
    best_weights = None
    stale = 0
    boards = train_data["boards"]
    played_ucis = train_data["played_move_uci"]
    for epoch in range(epochs):
        order = rng.permutation(n)
        ep_cp = 0.0
        ep_out = 0.0
        ep_rank = 0.0
        ep_rank_acc = 0.0
        seen = 0
        rank_seen = 0
        for start in range(0, n, batch_size):
            idx_arr = order[start:start + batch_size]
            B = idx_arr.size
            X_cur = densify(train_data["current_features"], idx_arr)
            bl_cur = train_data["baselines"][idx_arr]
            stm_cur = train_data["stm"][idx_arr]
            cp_tgt = train_data["cp_target"][idx_arr]
            out_tgt = train_data["outcome_target"][idx_arr]
            plw = train_data["ply_weight"][idx_arr]

            # Forward current
            pre1, h1, pre2, h2, out_cur, v_cur, _cp_pred = _forward_full(
                X_cur, bl_cur, stm_cur, weights
            )

            # --- cp huber loss ---
            err_cp = v_cur - cp_tgt
            ep_cp += float(huber(err_cp).sum())
            huber_d = huber_grad(err_cp)

            # --- outcome loss (ply-weighted MSE) ---
            err_out = v_cur - out_tgt
            ply_weighted_err = plw * err_out
            ep_out += float((ply_weighted_err * err_out).sum()) * 0.5
            d_out_grad = ply_weighted_err

            # --- ranking loss (afterstate logistic) ---
            # Build batch of (after_played, after_alt_1, ..., after_alt_K) for each sample
            # Total forward inputs: B * (1 + K)
            after_features_list = []
            after_bls = np.zeros(B * (1 + k_neg), dtype=np.float32)
            after_stms = np.zeros(B * (1 + k_neg), dtype=bool)
            slot_valid = np.zeros((B, k_neg), dtype=bool)
            # For each batch sample, fill (after_played, after_alt_1, ..., after_alt_K)
            for b, src in enumerate(idx_arr):
                board = boards[src]
                try:
                    played_mv = chess.Move.from_uci(played_ucis[src])
                except Exception:
                    played_mv = None
                legal = list(board.legal_moves) if board.is_valid() else []
                # Use played move (validate)
                if played_mv is not None and played_mv in legal:
                    feats, bl, stm = make_after_state(board, played_mv)
                else:
                    # Fallback: encode current board itself (will produce zero margin)
                    feats = np.asarray(active_features(board), dtype=np.int64)
                    bl = static_baseline_cp_white(board)
                    stm = (board.turn == chess.WHITE)
                slot_played = b * (1 + k_neg)
                after_features_list.append(feats)
                after_bls[slot_played] = bl
                after_stms[slot_played] = stm
                # Sample k_neg alternatives
                alts = [m for m in legal if m != played_mv] if played_mv else []
                if alts:
                    chosen_alts = sample_rng.sample(alts, min(k_neg, len(alts)))
                    for k, alt in enumerate(chosen_alts):
                        feats_a, bl_a, stm_a = make_after_state(board, alt)
                        after_features_list.append(feats_a)
                        slot = b * (1 + k_neg) + 1 + k
                        after_bls[slot] = bl_a
                        after_stms[slot] = stm_a
                        slot_valid[b, k] = True
                    # If fewer than k_neg alts, pad with played (zero-margin)
                    for k in range(len(chosen_alts), k_neg):
                        slot = b * (1 + k_neg) + 1 + k
                        after_features_list.append(feats)
                        after_bls[slot] = bl
                        after_stms[slot] = stm
                else:
                    # No alternatives — pad with played
                    for k in range(k_neg):
                        slot = b * (1 + k_neg) + 1 + k
                        after_features_list.append(feats)
                        after_bls[slot] = bl
                        after_stms[slot] = stm
            X_after = densify(after_features_list, np.arange(len(after_features_list)))
            _ap1, _ah1, _ap2, _ah2, out_after, v_after, _cp_after = _forward_full(
                X_after, after_bls, after_stms, weights
            )
            # Reshape v_after to (B, 1+k_neg). Score from CURRENT side POV = -V(after)
            v_after_grouped = v_after.reshape(B, 1 + k_neg)
            scores = -v_after_grouped  # (B, 1+k_neg), index 0 = played
            played_scores = scores[:, 0:1]                # (B, 1)
            alt_scores = scores[:, 1:]                    # (B, k_neg)
            # Logistic ranking: -log σ((played - alt)/T)
            # = softplus(-(played - alt)/T)
            diffs = (played_scores - alt_scores) / RANK_TEMPERATURE  # (B, k_neg)
            # Mask invalid alts (pads with same as played, diff=0)
            valid_mask = slot_valid.astype(np.float32)  # (B, k_neg)
            rank_loss_per = np.logaddexp(0.0, -diffs)
            masked_rank_loss = rank_loss_per * valid_mask
            n_valid = float(valid_mask.sum())
            ep_rank += float(masked_rank_loss.sum())
            if n_valid:
                ep_rank_acc += float(((diffs > 0).astype(np.float32) * valid_mask).sum())
            rank_seen += n_valid

            seen += B

            # --- Build effective gradients ---
            # tanh deriv on v_cur:
            tanh_d_cur = 1.0 - v_cur ** 2
            sign_cur = np.where(stm_cur, 1.0, -1.0).astype(np.float32)
            # d_cp_loss/d_out_cur = huber_d * tanh_d_cur * sign_cur * (EVAL_SCALE / VALUE_SCALE)
            d_cp_d_out = huber_d * tanh_d_cur * sign_cur * (EVAL_SCALE / VALUE_SCALE)
            d_out_d_out = d_out_grad * tanh_d_cur * sign_cur * (EVAL_SCALE / VALUE_SCALE)
            # Ranking loss gradient flows through v_after (B*(1+K) outputs).
            # ∂L_rank/∂diff = -σ(-diff) (sigmoid of -diff)
            sig = 1.0 / (1.0 + np.exp(diffs))  # (B, k_neg), = σ(-diff)
            sig_masked = -sig * valid_mask
            # ∂diff/∂played_score = +1/T; ∂diff/∂alt_score = -1/T
            # ∂L/∂played_score = sum_k sig_masked[k] * (1/T)  averaged over valid? we'll mean over all pairs.
            # Normalize by n_valid to be per-pair mean
            if n_valid > 0:
                # Per-pair loss = mean over all B*k_neg, but mask zeros out pads. Use sum/n_valid.
                grad_scale = ranking_weight / n_valid
                # played: d_L/d_score_played = sum_k sig_masked[b,k] * (1/T) * grad_scale
                d_score_played = sig_masked.sum(axis=1, keepdims=True) * (1.0 / RANK_TEMPERATURE) * grad_scale  # (B,1)
                # alt: d_L/d_score_alt = - sig_masked[b,k] * (1/T) * grad_scale
                d_score_alt = -sig_masked * (1.0 / RANK_TEMPERATURE) * grad_scale  # (B, k_neg)
                # score = -V(after) → d_score/d_v_after = -1
                # d_L/d_v_after_played = -d_score_played
                # d_L/d_v_after_alt = -d_score_alt
                d_v_after_played = -d_score_played  # (B,1)
                d_v_after_alt = -d_score_alt        # (B,k_neg)
                d_v_after = np.concatenate([d_v_after_played, d_v_after_alt], axis=1).reshape(-1)  # (B*(1+K),)
                # d_v_after = tanh_d_after * sign_after * d_cp_side / d_out_after * (EVAL_SCALE/VALUE_SCALE) ...
                # Same chain as current:
                tanh_d_after = 1.0 - v_after ** 2
                sign_after = np.where(after_stms, 1.0, -1.0).astype(np.float32)
                d_rank_d_out_after = d_v_after * tanh_d_after * sign_after * (EVAL_SCALE / VALUE_SCALE)
            else:
                d_rank_d_out_after = None

            # Combine cp + outcome gradients for current outputs:
            d_loss_d_out_cur = d_cp_d_out + outcome_weight * d_out_d_out
            # Synthetic y to feed cc._backward (mimics ∂MSE/∂out = 2*(out - y_eff)/N).
            # We want gradient d_loss_d_out_cur. cc._backward uses (out - y_eff) * 2 / N.
            # So y_eff_cur = out_cur[:,0] - d_loss_d_out_cur * (B / 2)
            y_eff_cur = out_cur[:, 0] - d_loss_d_out_cur * (B / 2.0)
            grads_cur = cc._backward(X_cur, y_eff_cur, weights, pre1, h1, pre2, h2, out_cur)

            if d_rank_d_out_after is not None:
                # Build backward over the after-state forward.
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

        mean_cp = ep_cp / seen if seen else 0.0
        mean_out = ep_out / seen if seen else 0.0
        mean_rank = ep_rank / rank_seen if rank_seen else 0.0
        rank_pair_acc = ep_rank_acc / rank_seen if rank_seen else 0.0
        # Dev evaluation
        dev_metrics = evaluate_dev(weights, dev_data)
        rank_metrics = evaluate_ranking_on_dev(weights, dev_data, n_samples=1000, k_neg=k_neg)
        combined_dev_loss = dev_metrics.get("huber_value_loss", float("inf")) + 0.3 * (1.0 - rank_metrics.get("ranking_pair_acc", 0))
        improved = combined_dev_loss < best_dev_loss - 1e-5
        if improved:
            best_dev_loss = combined_dev_loss
            best_weights = NeuralWeights(
                W1=weights.W1.copy(), b1=weights.b1.copy(),
                W2=weights.W2.copy(), b2=weights.b2.copy(),
                W3=weights.W3.copy(), b3=weights.b3.copy(),
                side_to_move_bias_cp=weights.side_to_move_bias_cp,
            )
            stale = 0
        else:
            stale += 1
        history.append({
            "epoch": epoch + 1,
            "train_cp": mean_cp, "train_out": mean_out, "train_rank": mean_rank,
            "train_rank_pair_acc": rank_pair_acc,
            "dev": dev_metrics, "dev_rank": rank_metrics,
            "combined_dev_loss": combined_dev_loss,
        })
        tag = "✓best" if improved else f"stale{stale}"
        print(f"  epoch {epoch+1:3d}/{epochs}: cp={mean_cp:.4f} out={mean_out:.4f} "
              f"rank={mean_rank:.4f} train_rank_acc={rank_pair_acc:.3f}  "
              f"dev_huber={dev_metrics.get('huber_value_loss', 0):.4f}  "
              f"dev_sign={dev_metrics.get('sign_accuracy', 0):.3f}  "
              f"dev_rank_acc={rank_metrics.get('ranking_pair_acc', 0):.3f}  {tag}", flush=True)
        if stale >= patience:
            print(f"  early stop at epoch {epoch+1}", flush=True)
            break
    return history, best_weights if best_weights is not None else weights


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-weights", type=Path,
                    default=exp6_artifacts_root() / "curriculum" / "snapshots" / "chess_experiment_6_neural_stage02.npz")
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    ap.add_argument("--lr", type=float, default=LR)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--patience", type=int, default=EARLY_STOP_PATIENCE)
    ap.add_argument("--outcome-weight", type=float, default=0.04)
    ap.add_argument("--ranking-weight", type=float, default=0.15)
    ap.add_argument("--k-neg", type=int, default=K_NEGATIVES)
    ap.add_argument("--out-snapshot-name", default="v7_3_best.npz")
    ap.add_argument("--labels-path", type=Path, default=None,
                    help="Override labels jsonl path (default v7.1's 1k labels).")
    ap.add_argument("--played-moves-path", type=Path, default=None,
                    help="Override played_moves jsonl path.")
    ap.add_argument("--train-game-id-max", type=int, default=None,
                    help="Override train/dev split upper edge (inclusive). For 10K dataset use 8999.")
    ap.add_argument("--dev-game-id-max", type=int, default=None,
                    help="Override dev range upper edge (inclusive). For 10K dataset use 9999.")
    args = ap.parse_args()
    # Apply game_id range overrides for v9 (10K) dataset
    if args.train_game_id_max is not None and args.dev_game_id_max is not None:
        global TRAIN_GAME_ID_RANGE, DEV_GAME_ID_RANGE
        TRAIN_GAME_ID_RANGE = (0, args.train_game_id_max)
        DEV_GAME_ID_RANGE = (args.train_game_id_max + 1, args.dev_game_id_max)
        print(f"  override split: train [0..{args.train_game_id_max}], dev [{args.train_game_id_max+1}..{args.dev_game_id_max}]", flush=True)

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    played_moves_path = args.played_moves_path if args.played_moves_path else PLAYED_MOVES_PATH
    labels_path = args.labels_path if args.labels_path else cc.LABELS_PATH
    print(f"loading played moves from {played_moves_path} ...", flush=True)
    t0 = time.perf_counter()
    played_map = load_played_moves(played_moves_path)
    print(f"  {len(played_map)} (game_idx, fen) → played_move entries ({time.perf_counter()-t0:.1f}s)", flush=True)

    print(f"loading labels from {labels_path} + splitting by game_id...", flush=True)
    t0 = time.perf_counter()
    train_rows, dev_rows = load_split(played_map, labels_path=labels_path)
    print(f"  train={len(train_rows)}, dev={len(dev_rows)} ({time.perf_counter()-t0:.1f}s)", flush=True)

    print(f"precomputing features...", flush=True)
    t0 = time.perf_counter()
    train_data = precompute_features(train_rows)
    dev_data = precompute_features(dev_rows)
    print(f"  precomputed ({time.perf_counter()-t0:.1f}s)", flush=True)

    if args.seed_weights.exists():
        weights = load_weights(args.seed_weights)
        print(f"  warm-start from {args.seed_weights}", flush=True)
    else:
        weights = make_initial_weights(seed=SEED)
        print(f"  fresh random init", flush=True)

    initial_dev = evaluate_dev(weights, dev_data)
    initial_rank = evaluate_ranking_on_dev(weights, dev_data, n_samples=2000, k_neg=args.k_neg)
    print(f"\ninitial dev:  huber={initial_dev['huber_value_loss']:.4f}  "
          f"sign={initial_dev['sign_accuracy']:.3f}  "
          f"cp_corr={initial_dev['cp_correlation']:.3f}  "
          f"avg_cp_err={initial_dev['avg_abs_cp_err']:.1f}  "
          f"dev_rank_acc={initial_rank['ranking_pair_acc']:.3f}  "
          f"({initial_rank['ranking_pairs']} pairs)", flush=True)

    print(f"\nstarting training: epochs={args.epochs}, lr={args.lr}, batch={args.batch_size}, "
          f"outcome_weight={args.outcome_weight}, ranking_weight={args.ranking_weight}, "
          f"k_neg={args.k_neg}", flush=True)
    history, best_weights = train_loop(
        weights, train_data, dev_data,
        epochs=args.epochs, lr=args.lr, momentum=MOMENTUM, batch_size=args.batch_size,
        patience=args.patience, outcome_weight=args.outcome_weight,
        ranking_weight=args.ranking_weight, k_neg=args.k_neg,
    )

    final_dev = evaluate_dev(best_weights, dev_data)
    final_rank = evaluate_ranking_on_dev(best_weights, dev_data, n_samples=2000, k_neg=args.k_neg)
    print(f"\nfinal best dev:  huber={final_dev['huber_value_loss']:.4f}  "
          f"sign={final_dev['sign_accuracy']:.3f}  cp_corr={final_dev['cp_correlation']:.3f}  "
          f"avg_cp_err={final_dev['avg_abs_cp_err']:.1f}  rank_acc={final_rank['ranking_pair_acc']:.3f}", flush=True)

    snap_path = SNAPSHOTS_DIR / args.out_snapshot_name
    save_weights(snap_path, best_weights)
    print(f"saved -> {snap_path}", flush=True)

    print(f"\nstaged-10 sanity check vs Stockfish:", flush=True)
    results = cc.play_staged_test(snap_path)
    summ = cc.score_summary(results)
    sr = cc.score_rate(results)
    print(f"  {summ['W']}W/{summ['D']}D/{summ['L']}L score={summ['total_score']:+d}/{summ['max_possible_score']} "
          f"(norm={sr:.2%})", flush=True)

    REPORT_JSON.write_text(json.dumps({
        "config": {
            "epochs": args.epochs, "lr": args.lr, "batch_size": args.batch_size,
            "outcome_weight": args.outcome_weight, "ranking_weight": args.ranking_weight,
            "k_neg": args.k_neg, "patience": args.patience,
            "seed_weights": str(args.seed_weights),
            "train_size": len(train_rows), "dev_size": len(dev_rows),
        },
        "initial_dev": initial_dev,
        "initial_rank": initial_rank,
        "final_dev": final_dev,
        "final_rank": final_rank,
        "history": history,
        "staged_10_sanity": {
            "W": summ["W"], "D": summ["D"], "L": summ["L"],
            "score_total": summ["total_score"],
            "score_max": summ["max_possible_score"],
            "score_rate": sr,
        },
    }, indent=2, default=str))
    print(f"\nreport -> {REPORT_JSON}", flush=True)
    print(f"\nNOTE: v7.3 — run chess_exp6_match.py vs v6.2 S2 to match-gate.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
