#!/usr/bin/env python3
"""Exp6 v7.1 Phase 1: position-level supervised value pretrain.

Per user spec (overrides earlier self-play-first plan):

  1000 Stockfish-filtered games → position-level samples
  Split by game_id: 900 train / 100 internal dev
  10 staged-Stockfish-1-5 games stay as fixed regression suite

  Value convention: V(s) is side-to-move expected outcome ∈ [-1, +1].
  Target = tanh(clip(cp_white, ±2000) / 600), measured from side-to-move
  POV (negate if black-to-move).

  Loss: Huber on (predicted_value - target_value).

  NN output meaning unchanged from v6.2: residual on top of static
  baseline (material + PST). At training time we convert the residual
  to a value via tanh((residual_cp + baseline) / 600) before computing
  Huber loss. This keeps the existing eval_board / search interface
  intact (engine still consumes cp) but ensures the LEARNING SIGNAL
  is bounded and well-conditioned (tanh saturates large cp).

v7.2 will add ply-weighted outcome auxiliary loss; v7.3 will add
move-ranking. This script handles ONLY Phase 1.
"""
from __future__ import annotations

import argparse
import json
import math
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
from scripts.games.common_paths import exp6_artifacts_root  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402


# ── v7.1 hyperparameters ───────────────────────────────────────────
TRAIN_GAME_ID_RANGE = (0, 899)      # inclusive
DEV_GAME_ID_RANGE = (900, 999)
CP_CLAMP = 2000.0                   # cp clipped to ±2000 before tanh
VALUE_SCALE = 600.0                 # tanh(cp / VALUE_SCALE)
HUBER_DELTA = 0.1                   # Huber loss threshold in value space
OUTCOME_PLY_FULL_WEIGHT_AT = 40.0   # outcome weight reaches 1.0 at this ply (per user spec)
BATCH_SIZE = 128
LR = 0.001
MOMENTUM = 0.9
GRAD_CLIP = 1.0
EPOCHS = 80                         # single-stage training; user's spec doesn't curriculum-stage
EARLY_STOP_PATIENCE = 10            # epochs without dev improvement
SEED = 20260517

PERSISTENT_DIR = exp6_artifacts_root() / "v7_1"
SNAPSHOTS_DIR = PERSISTENT_DIR / "snapshots"
REPORT_JSON = PERSISTENT_DIR / "report.json"


def _value_target_from_cp_white(cp_white: float, side_to_move_is_white: bool) -> float:
    """Convert white-perspective cp into side-to-move value ∈ (-1, +1)."""
    cp_side = cp_white if side_to_move_is_white else -cp_white
    cp_clipped = max(-CP_CLAMP, min(CP_CLAMP, cp_side))
    return math.tanh(cp_clipped / VALUE_SCALE)


def load_split() -> tuple[list, list]:
    """Load cached labels, split by game_id. Returns (train_rows, dev_rows).
    Each row = (fen, cp_white, outcome_white, game_idx, side_to_move_is_white).
    """
    if not cc.LABELS_PATH.exists():
        raise FileNotFoundError(f"Labels cache missing: {cc.LABELS_PATH}")
    train, dev = [], []
    with cc.LABELS_PATH.open() as f:
        for line in f:
            rec = json.loads(line)
            fen = rec["fen"]
            cp_w = float(rec["cp_white"])
            out_w = float(rec.get("outcome_white", 0.0))
            game_idx = int(rec["game_idx"])
            board = chess.Board(fen)
            stm_is_white = (board.turn == chess.WHITE)
            row = (fen, cp_w, out_w, game_idx, stm_is_white, board)
            if TRAIN_GAME_ID_RANGE[0] <= game_idx <= TRAIN_GAME_ID_RANGE[1]:
                train.append(row)
            elif DEV_GAME_ID_RANGE[0] <= game_idx <= DEV_GAME_ID_RANGE[1]:
                dev.append(row)
    return train, dev


def precompute_features(rows: list) -> tuple[list[np.ndarray], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Returns features_idx, baseline_cp_white, target_value (side-to-move POV),
    outcome_target_side_to_move (∈ {-1,0,+1}), ply_weight (∈ (0,1]).
    """
    features_idx = []
    baselines = np.zeros(len(rows), dtype=np.float32)
    targets_cp = np.zeros(len(rows), dtype=np.float32)
    outcome_targets = np.zeros(len(rows), dtype=np.float32)
    ply_weights = np.zeros(len(rows), dtype=np.float32)
    for i, (fen, cp_w, out_w, gid, stm_is_white, board) in enumerate(rows):
        features_idx.append(np.asarray(active_features(board), dtype=np.int64))
        baselines[i] = static_baseline_cp_white(board)
        targets_cp[i] = _value_target_from_cp_white(cp_w, stm_is_white)
        # Outcome target from side-to-move POV
        out_side = out_w if stm_is_white else -out_w
        outcome_targets[i] = out_side
        # Ply weight (user spec): min(1, ply/40). Compute ply from FEN.
        ply = 2 * (board.fullmove_number - 1) + (0 if board.turn == chess.WHITE else 1)
        ply_weights[i] = min(1.0, ply / OUTCOME_PLY_FULL_WEIGHT_AT)
    return features_idx, baselines, targets_cp, outcome_targets, ply_weights


def huber(err: np.ndarray, delta: float = HUBER_DELTA) -> np.ndarray:
    """Huber loss elementwise. ½err² for |err|<δ, δ(|err|-½δ) otherwise."""
    abs_err = np.abs(err)
    quad_mask = abs_err <= delta
    out = np.zeros_like(err)
    out[quad_mask] = 0.5 * err[quad_mask] ** 2
    out[~quad_mask] = delta * (abs_err[~quad_mask] - 0.5 * delta)
    return out


def huber_grad(err: np.ndarray, delta: float = HUBER_DELTA) -> np.ndarray:
    """∂Huber/∂err."""
    return np.clip(err, -delta, delta)


def forward_with_value(X: np.ndarray, baseline_cp_white: np.ndarray,
                       stm_is_white: np.ndarray, weights: NeuralWeights):
    """Forward pass returning per-sample side-to-move value prediction.

    Returns (pre1, h1, pre2, h2, out, predicted_value, predicted_cp_side).
    """
    pre1, h1, pre2, h2, out = cc._forward(X, weights)
    nn_residual_cp_white = out[:, 0] * EVAL_SCALE
    predicted_cp_white = nn_residual_cp_white + baseline_cp_white
    # Convert to side-to-move POV
    sign = np.where(stm_is_white, 1.0, -1.0).astype(np.float32)
    predicted_cp_side = predicted_cp_white * sign
    predicted_value = np.tanh(predicted_cp_side / VALUE_SCALE)
    return pre1, h1, pre2, h2, out, predicted_value, predicted_cp_side


def evaluate_dev(weights: NeuralWeights, dev_features: list[np.ndarray],
                 dev_baselines: np.ndarray, dev_stm: np.ndarray,
                 dev_targets: np.ndarray, dev_cp_white: np.ndarray) -> dict:
    """Compute per-sample metrics on dev set."""
    n = len(dev_features)
    if n == 0:
        return {}
    # Forward in batches to keep memory bounded
    all_value_pred = np.zeros(n, dtype=np.float32)
    all_cp_side_pred = np.zeros(n, dtype=np.float32)
    for start in range(0, n, BATCH_SIZE):
        end = min(start + BATCH_SIZE, n)
        idx = np.arange(start, end)
        X = cc.densify(dev_features, idx)
        _, _, _, _, _, v_pred, cp_pred = forward_with_value(
            X, dev_baselines[idx], dev_stm[idx], weights
        )
        all_value_pred[idx] = v_pred
        all_cp_side_pred[idx] = cp_pred
    # Side-to-move target cp for comparison
    dev_cp_side = np.where(dev_stm, dev_cp_white, -dev_cp_white)
    err_value = all_value_pred - dev_targets
    huber_loss = float(huber(err_value).mean())
    sign_acc = float(((all_value_pred > 0) == (dev_targets > 0)).mean())
    # Pearson correlation between predicted cp and target cp (side POV)
    if n > 1 and dev_cp_side.std() > 1e-6:
        cp_corr = float(np.corrcoef(all_cp_side_pred, dev_cp_side)[0, 1])
    else:
        cp_corr = 0.0
    abs_cp_err = np.abs(all_cp_side_pred - dev_cp_side)
    avg_delta_cp = float(abs_cp_err.mean())
    return {
        "n": n,
        "huber_value_loss": huber_loss,
        "sign_accuracy": sign_acc,
        "cp_correlation": cp_corr,
        "avg_abs_cp_err": avg_delta_cp,
        "mean_pred_value": float(all_value_pred.mean()),
        "std_pred_value": float(all_value_pred.std()),
    }


def train_loop(weights: NeuralWeights,
               train_features: list[np.ndarray], train_baselines: np.ndarray,
               train_stm: np.ndarray, train_targets: np.ndarray,
               train_outcomes: np.ndarray, train_ply_w: np.ndarray,
               dev_features: list[np.ndarray], dev_baselines: np.ndarray,
               dev_stm: np.ndarray, dev_targets: np.ndarray, dev_cp_white: np.ndarray,
               *, epochs: int, lr: float, momentum: float, batch_size: int,
               patience: int, outcome_weight: float) -> tuple[list[dict], dict, NeuralWeights]:
    n = len(train_features)
    rng = np.random.default_rng(SEED)
    vel = {name: np.zeros_like(getattr(weights, name)) for name in ("W1", "b1", "W2", "b2", "W3", "b3")}
    history: list[dict] = []
    best_dev_loss = float("inf")
    best_weights = None
    stale_epochs = 0
    for epoch in range(epochs):
        order = rng.permutation(n)
        ep_loss_cp = 0.0
        ep_loss_out = 0.0
        seen = 0
        for start in range(0, n, batch_size):
            idx = order[start:start + batch_size]
            X = cc.densify(train_features, idx)
            baseline_batch = train_baselines[idx]
            stm_batch = train_stm[idx]
            target_cp_batch = train_targets[idx]
            target_out_batch = train_outcomes[idx]
            ply_w_batch = train_ply_w[idx]
            pre1, h1, pre2, h2, out, v_pred, cp_pred = forward_with_value(
                X, baseline_batch, stm_batch, weights
            )
            # --- cp value loss (Huber) ---
            err_cp = v_pred - target_cp_batch
            loss_cp = float(huber(err_cp).mean())
            ep_loss_cp += loss_cp * idx.size
            huber_d_cp = huber_grad(err_cp)
            # --- outcome auxiliary loss (ply-weighted MSE in value space) ---
            # outcome_loss = w_ply * (v_pred - outcome_side)^2 (mean)
            err_out = v_pred - target_out_batch
            ply_weighted_err = ply_w_batch * err_out
            loss_out = float((ply_weighted_err * err_out).mean()) * 0.5  # 0.5 from MSE convention
            ep_loss_out += loss_out * idx.size
            # d(outcome_loss)/d_v_pred = ply_w * err_out
            d_out_grad = ply_weighted_err
            # --- combined gradient ---
            seen += idx.size
            tanh_d = 1.0 - v_pred ** 2
            sign_arr = np.where(stm_batch, 1.0, -1.0).astype(np.float32)
            # Combined value-space gradient (per-sample):
            d_loss_d_v = huber_d_cp + outcome_weight * d_out_grad
            d_loss_d_out = d_loss_d_v * tanh_d * sign_arr * (EVAL_SCALE / VALUE_SCALE)
            y_eff = out[:, 0] - d_loss_d_out * (idx.size / 2.0)
            grads = cc._backward(X, y_eff, weights, pre1, h1, pre2, h2, out)
            for name, grad in zip(("W1", "b1", "W2", "b2", "W3", "b3"), grads):
                norm = float(np.linalg.norm(grad))
                if norm > GRAD_CLIP:
                    grad = grad * (GRAD_CLIP / norm)
                vel[name] = momentum * vel[name] - lr * grad
                getattr(weights, name)[...] += vel[name]
        train_cp = ep_loss_cp / seen if seen else 0.0
        train_out = ep_loss_out / seen if seen else 0.0
        train_loss = train_cp + outcome_weight * train_out
        dev_metrics = evaluate_dev(
            weights, dev_features, dev_baselines, dev_stm, dev_targets, dev_cp_white
        )
        history.append({
            "epoch": epoch + 1, "train_loss": train_loss,
            "train_loss_cp": train_cp, "train_loss_outcome": train_out,
            "dev": dev_metrics,
        })
        dev_loss = dev_metrics.get("huber_value_loss", float("inf"))
        improved = dev_loss < best_dev_loss - 1e-5
        if improved:
            best_dev_loss = dev_loss
            best_weights = NeuralWeights(
                W1=weights.W1.copy(), b1=weights.b1.copy(),
                W2=weights.W2.copy(), b2=weights.b2.copy(),
                W3=weights.W3.copy(), b3=weights.b3.copy(),
                side_to_move_bias_cp=weights.side_to_move_bias_cp,
            )
            stale_epochs = 0
        else:
            stale_epochs += 1
        if epoch < 3 or (epoch + 1) % 5 == 0 or improved or stale_epochs == patience:
            tag = "✓best" if improved else f"stale{stale_epochs}"
            print(f"  epoch {epoch+1:3d}/{epochs}: train_cp={train_cp:.4f} train_out={train_out:.4f}  "
                  f"dev_huber={dev_metrics.get('huber_value_loss', 0):.4f}  "
                  f"sign_acc={dev_metrics.get('sign_accuracy', 0):.3f}  "
                  f"cp_corr={dev_metrics.get('cp_correlation', 0):.3f}  "
                  f"avg_abs_cp_err={dev_metrics.get('avg_abs_cp_err', 0):.1f}  "
                  f"{tag}", flush=True)
        if stale_epochs >= patience:
            print(f"  early stop at epoch {epoch+1} (patience={patience})", flush=True)
            break
    best_metrics = {"best_dev_huber_loss": best_dev_loss, "epochs_run": len(history)}
    return history, best_metrics, (best_weights if best_weights is not None else weights)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-weights", type=Path,
                    default=exp6_artifacts_root() / "curriculum" / "snapshots" / "chess_experiment_6_neural_stage02.npz",
                    help="Optional seed weights to warm-start. Default v6.2 S2.")
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    ap.add_argument("--lr", type=float, default=LR)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--patience", type=int, default=EARLY_STOP_PATIENCE)
    ap.add_argument("--fresh-init", action="store_true", help="Ignore seed weights, start from random")
    ap.add_argument("--outcome-weight", type=float, default=0.0,
                    help="Phase 2 outcome auxiliary loss weight (user spec: 0.25). "
                         "0.0 = Phase 1 (cp only). >0 enables ply-weighted outcome aux.")
    ap.add_argument("--out-snapshot-name", default="v7_1_best.npz",
                    help="Output snapshot filename in v7_1_snapshots/ dir.")
    args = ap.parse_args()

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"loading position-level samples from {cc.LABELS_PATH} ...", flush=True)
    t0 = time.perf_counter()
    train_rows, dev_rows = load_split()
    print(f"  loaded train={len(train_rows)}, dev={len(dev_rows)} ({time.perf_counter()-t0:.1f}s)", flush=True)
    if not train_rows or not dev_rows:
        print("missing train or dev rows; abort", flush=True)
        return 1

    print(f"precomputing features ...", flush=True)
    t0 = time.perf_counter()
    train_features, train_baselines, train_targets, train_outcomes, train_ply_w = precompute_features(train_rows)
    dev_features, dev_baselines, dev_targets, _dev_outcomes, _dev_ply_w = precompute_features(dev_rows)
    train_stm = np.array([r[4] for r in train_rows], dtype=bool)
    dev_stm = np.array([r[4] for r in dev_rows], dtype=bool)
    dev_cp_white = np.array([r[1] for r in dev_rows], dtype=np.float32)
    print(f"  precomputed ({time.perf_counter()-t0:.1f}s)", flush=True)
    print(f"  train mean ply weight: {train_ply_w.mean():.3f}  (samples >= 1.0: {(train_ply_w >= 0.999).mean():.2%})", flush=True)

    # Seed weights
    if args.fresh_init or not args.seed_weights.exists():
        weights = make_initial_weights(seed=SEED)
        print(f"  fresh random init (seed={SEED})", flush=True)
    else:
        weights = load_weights(args.seed_weights)
        print(f"  warm-start from {args.seed_weights}", flush=True)

    # Pre-training dev evaluation
    initial_dev = evaluate_dev(weights, dev_features, dev_baselines, dev_stm, dev_targets, dev_cp_white)
    print(f"\ninitial dev:  huber={initial_dev['huber_value_loss']:.4f}  "
          f"sign={initial_dev['sign_accuracy']:.3f}  cp_corr={initial_dev['cp_correlation']:.3f}  "
          f"avg_cp_err={initial_dev['avg_abs_cp_err']:.1f}", flush=True)

    print(f"\nstarting training: epochs={args.epochs}, lr={args.lr}, batch={args.batch_size}, "
          f"outcome_weight={args.outcome_weight}", flush=True)
    history, summary, best_weights = train_loop(
        weights,
        train_features, train_baselines, train_stm, train_targets,
        train_outcomes, train_ply_w,
        dev_features, dev_baselines, dev_stm, dev_targets, dev_cp_white,
        epochs=args.epochs, lr=args.lr, momentum=MOMENTUM, batch_size=args.batch_size,
        patience=args.patience, outcome_weight=args.outcome_weight,
    )

    final_dev = evaluate_dev(best_weights, dev_features, dev_baselines, dev_stm, dev_targets, dev_cp_white)
    print(f"\nfinal best dev:  huber={final_dev['huber_value_loss']:.4f}  "
          f"sign={final_dev['sign_accuracy']:.3f}  cp_corr={final_dev['cp_correlation']:.3f}  "
          f"avg_cp_err={final_dev['avg_abs_cp_err']:.1f}", flush=True)

    snap_path = SNAPSHOTS_DIR / args.out_snapshot_name
    save_weights(snap_path, best_weights)
    print(f"saved best snapshot -> {snap_path}", flush=True)
    # Final staged-10 sanity check vs Stockfish (NOT match-gating; that's separate)
    print(f"\nstaged-10 sanity check vs Stockfish (depths 1-5):", flush=True)
    results = cc.play_staged_test(snap_path)
    summ = cc.score_summary(results)
    sr = cc.score_rate(results)
    print(f"  {summ['W']}W/{summ['D']}D/{summ['L']}L score={summ['total_score']:+d}/{summ['max_possible_score']} "
          f"(norm={sr:.2%})", flush=True)

    REPORT_JSON.write_text(json.dumps({
        "config": {
            "epochs": args.epochs, "lr": args.lr, "batch_size": args.batch_size,
            "patience": args.patience, "huber_delta": HUBER_DELTA,
            "value_scale": VALUE_SCALE, "cp_clamp": CP_CLAMP,
            "train_size": len(train_rows), "dev_size": len(dev_rows),
            "seed_weights": str(args.seed_weights) if not args.fresh_init else None,
        },
        "initial_dev": initial_dev,
        "final_dev": final_dev,
        "history": history,
        "summary": summary,
        "staged_10_sanity": {
            "W": summ["W"], "D": summ["D"], "L": summ["L"],
            "score_total": summ["total_score"],
            "score_max": summ["max_possible_score"],
            "score_rate": sr,
        },
    }, indent=2))
    print(f"\nreport -> {REPORT_JSON}", flush=True)
    print(f"\nNOTE: v7.1 is supervised pretrain. NOT auto-promoting.", flush=True)
    print(f"      Run chess_exp6_match.py vs v6.2 S2 to gate promotion.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
