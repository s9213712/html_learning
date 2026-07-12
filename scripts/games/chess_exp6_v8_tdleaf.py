#!/usr/bin/env python3
"""Exp6 v8.0: TD(λ) self-play fine-tune (TDLeaf-spirit, root-eval based).

Per user spec (revised again after v7.3 reject):

  v7.x supervised on 1000 games all match-gate at 0W/27D/3L = 45%
  vs v6.2 S2. dev metrics improve but real play doesn't. The
  paradigm itself (mimic Stockfish cp) doesn't translate to better
  search-time move selection.

  v8 changes target paradigm:

    target_V(s_t) = (1 - λ) · V_bootstrap(s_t+1)
                  + λ · z_t

  where:
    z_t = final outcome from s_t side-to-move POV
    V_bootstrap(s_t+1) = tanh(search_score(s_t+1)_current_POV / 600)
    λ controls Monte Carlo vs bootstrap blend (default 0.7)

  Loss:
    L = 1.0  * huber(V_pred(s) - target_V(s))
      + 0.01 * cp_huber_anchor   (kept very small to prevent drift)

  Self-play: current weights play both sides at fixed_depth_d2.
  Each iteration generates N self-play games, computes TD(λ)
  targets per position, trains NN, evaluates.

  Seed: v6.2 S2. Snapshots are written under the external Exp6 artifact root.
  No auto-promotion — match-gate against v6.2 S2 manually.

  Sanity check loop:
    iter 0: baseline staged-10 with seed weights
    iter k: self-play → TD targets → train → staged-10 → save snap
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
    EVAL_SCALE, INPUT_DIM, NeuralEvaluator, NeuralWeights, active_features,
    load_weights, make_initial_weights, save_weights,
    static_baseline_cp_white,
)
from services.games.chess_search import ZobristHasher, search_best_move  # noqa: E402
from services.games.chess_exp6 import _move_order_score, _resolve_search_profile  # noqa: E402
from scripts.games.common_paths import exp6_artifacts_root  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402


# ── v8.0 hyperparameters ───────────────────────────────────────────
N_ITERATIONS = 6
N_SELFPLAY_GAMES_PER_ITER = 30
SELFPLAY_MAX_PLIES = 200
SELFPLAY_SEARCH_PROFILE = "balanced"  # depth 2, quiescence 2 (matches runtime)
TD_LAMBDA = 0.7                       # 0 = pure bootstrap, 1 = pure outcome
RANDOM_EXPLORE_PLIES_CHOICES = (6, 8, 10, 12)  # wider opening prelude
# ε=0.15 STILL produced all-draws (W0/D20/L0) — two-engine-self-play
# with identical weights is too robust to single-move noise. Need
# DIVERSE STARTING POSITIONS instead. Sample 50% of self-play games
# from a random labeled FEN (mid-game) — different start positions
# break the deterministic equilibrium and yield real decisive games.
EPS_GREEDY = 0.10
RANDOM_FEN_START_PROB = 0.6  # fraction of games starting from a random labeled mid-game FEN
VALUE_SCALE = 600.0
CP_CLAMP = 2000.0
HUBER_DELTA = 0.1
N_STOCKFISH_ANCHOR_SAMPLES = 2000     # tiny aux dataset to prevent value drift
ANCHOR_LOSS_WEIGHT = 0.01
TD_LOSS_WEIGHT = 1.0
BATCH_SIZE = 64
LR = 0.0003
MOMENTUM = 0.9
GRAD_CLIP = 1.0
EPOCHS_PER_ITER = 12
SEED = 20260519

EXP6_ARTIFACTS = exp6_artifacts_root()
SNAPSHOTS_DIR = EXP6_ARTIFACTS / "v8" / "snapshots"
REPORT_JSON = EXP6_ARTIFACTS / "v8" / "report.json"


def _board_features_idx(board: chess.Board) -> np.ndarray:
    return np.asarray(active_features(board), dtype=np.int64)


def play_self_play_game_with_eval(weights: NeuralWeights, opening_moves: list[str],
                                   random_explore_plies: int, rng: random.Random,
                                   start_fen: str | None = None,
                                   max_plies: int = SELFPLAY_MAX_PLIES,
                                   ) -> tuple[list[dict], float, str]:
    """Returns (positions, outcome_white, reason).
    Each position dict: {fen, stm_is_white, score_stm, played_uci}.
    score_stm is the search's reported score from side-to-move POV.

    If ``start_fen`` is given, start the game from that position (and
    ignore opening_moves / random_explore_plies). This is used to
    inject diverse mid-game starting positions sampled from the
    labeled dataset — defeats the deterministic equilibrium that
    keeps two-identical-engine self-play stuck at all-draws.
    """
    evaluator = NeuralEvaluator(weights)
    hasher = ZobristHasher(seed=20260601)
    profile = _resolve_search_profile(SELFPLAY_SEARCH_PROFILE)
    if start_fen is not None:
        try:
            board = chess.Board(start_fen)
        except Exception:
            board = chess.Board()
    else:
        board = chess.Board()
        for uci in opening_moves:
            try:
                mv = chess.Move.from_uci(uci)
                if mv in board.legal_moves:
                    board.push(mv)
            except Exception:
                break
        # Random exploration plies for opening diversity
        for _ in range(random_explore_plies):
            if board.is_game_over(claim_draw=True):
                break
            legal = list(board.legal_moves)
            if not legal:
                break
            board.push(rng.choice(legal))

    positions: list[dict] = []
    invalid = None
    for _ply in range(max_plies):
        if board.is_game_over(claim_draw=True):
            break
        try:
            result = search_best_move(
                board,
                max_depth=int(profile["depth"]),
                evaluate=evaluator,
                move_order_fn=_move_order_score,
                quiescence_depth=int(profile["quiescence_depth"]),
                hasher=hasher,
                time_budget_ms=profile.get("time_budget_ms"),
                enable_pvs=bool(profile.get("enable_pvs")),
                enable_lmr=bool(profile.get("enable_lmr")),
                enable_null_move=bool(profile.get("enable_null_move")),
                enable_futility=bool(profile.get("enable_futility")),
            )
        except Exception as exc:
            invalid = f"crash:{exc}"; break
        if result.best_move is None or result.best_move not in board.legal_moves:
            invalid = "no_move_or_illegal"; break
        # ε-greedy: pick a random legal move with prob EPS_GREEDY to
        # break deterministic self-play repetition deadlocks.
        chosen_move = result.best_move
        if rng.random() < EPS_GREEDY:
            legal = list(board.legal_moves)
            if legal:
                chosen_move = rng.choice(legal)
        positions.append({
            "fen": board.fen(),
            "stm_is_white": (board.turn == chess.WHITE),
            "score_stm": int(result.score),
            "played_uci": chosen_move.uci(),
        })
        board.push(chosen_move)

    res = board.result(claim_draw=True)
    if invalid:
        outcome_white = 0.0
        reason = invalid
    elif res == "1-0":
        outcome_white = 1.0
        reason = "white_win"
    elif res == "0-1":
        outcome_white = -1.0
        reason = "black_win"
    else:
        outcome_white = 0.0
        reason = "draw"
    return positions, outcome_white, reason


def build_td_targets(positions: list[dict], outcome_white: float, td_lambda: float) -> list[dict]:
    """For each recorded position s_t, compute TD(λ) target V from
    s_t side-to-move's POV.
    target = (1 - λ) · bootstrap + λ · z
    bootstrap = -V_pred(s_t+1) in s_t POV; if s_t was last recorded
    move (game ended after it), bootstrap = z (final outcome).
    Where V_pred(s) = tanh(search_score(s)_stm_POV / VALUE_SCALE).
    """
    n = len(positions)
    out: list[dict] = []
    for t, p in enumerate(positions):
        z = outcome_white if p["stm_is_white"] else -outcome_white
        if t + 1 >= n:
            target = z  # terminal — no bootstrap available
        else:
            nxt = positions[t + 1]
            # search_score is from next position's side POV
            sign = -1.0 if nxt["stm_is_white"] != p["stm_is_white"] else 1.0
            next_score_my_pov = sign * nxt["score_stm"]
            v_bootstrap = math.tanh(next_score_my_pov / VALUE_SCALE)
            target = (1.0 - td_lambda) * v_bootstrap + td_lambda * z
        out.append({
            "fen": p["fen"],
            "stm_is_white": p["stm_is_white"],
            "target_v": float(target),
            "ply": t,
        })
    return out


def huber(err: np.ndarray, delta: float = HUBER_DELTA) -> np.ndarray:
    abs_err = np.abs(err)
    quad_mask = abs_err <= delta
    out = np.zeros_like(err)
    out[quad_mask] = 0.5 * err[quad_mask] ** 2
    out[~quad_mask] = delta * (abs_err[~quad_mask] - 0.5 * delta)
    return out


def huber_grad(err: np.ndarray, delta: float = HUBER_DELTA) -> np.ndarray:
    return np.clip(err, -delta, delta)


def precompute_td_rows(rows: list[dict]) -> dict:
    n = len(rows)
    features_idx = []
    baselines = np.zeros(n, dtype=np.float32)
    stm = np.zeros(n, dtype=bool)
    targets = np.zeros(n, dtype=np.float32)
    for i, r in enumerate(rows):
        board = chess.Board(r["fen"])
        features_idx.append(_board_features_idx(board))
        baselines[i] = static_baseline_cp_white(board)
        stm[i] = r["stm_is_white"]
        targets[i] = r["target_v"]
    return {
        "features_idx": features_idx,
        "baselines": baselines,
        "stm": stm,
        "targets": targets,
    }


def sample_anchor_rows(n_samples: int, seed: int) -> dict:
    """Sample n_samples Stockfish-labeled positions from labels.jsonl
    as a tiny anchor against value drift. Target = tanh(cp_side / VALUE_SCALE).
    """
    if not cc.LABELS_PATH.exists():
        return {"features_idx": [], "baselines": np.array([]), "stm": np.array([]), "targets": np.array([])}
    rng = random.Random(seed)
    all_recs = []
    with cc.LABELS_PATH.open() as f:
        for line in f:
            try:
                all_recs.append(json.loads(line))
            except Exception:
                pass
    picked = rng.sample(all_recs, min(n_samples, len(all_recs)))
    features_idx = []
    baselines = []
    stm = []
    targets = []
    for r in picked:
        cp_w = max(-CP_CLAMP, min(CP_CLAMP, float(r["cp_white"])))
        board = chess.Board(r["fen"])
        is_white = (board.turn == chess.WHITE)
        cp_side = cp_w if is_white else -cp_w
        target_v = math.tanh(cp_side / VALUE_SCALE)
        features_idx.append(_board_features_idx(board))
        baselines.append(static_baseline_cp_white(board))
        stm.append(is_white)
        targets.append(target_v)
    return {
        "features_idx": features_idx,
        "baselines": np.array(baselines, dtype=np.float32),
        "stm": np.array(stm, dtype=bool),
        "targets": np.array(targets, dtype=np.float32),
    }


def densify(features_idx: list[np.ndarray], indices: np.ndarray) -> np.ndarray:
    X = np.zeros((indices.size, INPUT_DIM), dtype=np.float32)
    for r, src in enumerate(indices):
        X[r, features_idx[src]] = 1.0
    return X


def forward_with_value(X: np.ndarray, baselines: np.ndarray, stm: np.ndarray,
                       weights: NeuralWeights):
    pre1, h1, pre2, h2, out = cc._forward(X, weights)
    nn_residual = out[:, 0] * EVAL_SCALE
    cp_white = nn_residual + baselines
    sign = np.where(stm, 1.0, -1.0).astype(np.float32)
    cp_side = cp_white * sign
    v_pred = np.tanh(cp_side / VALUE_SCALE)
    return pre1, h1, pre2, h2, out, v_pred


def train_iter(weights: NeuralWeights, td_data: dict, anchor_data: dict, *,
               epochs: int, lr: float, momentum: float, batch_size: int,
               td_weight: float, anchor_weight: float) -> list[dict]:
    n_td = len(td_data["features_idx"])
    n_anchor = len(anchor_data["features_idx"])
    rng = np.random.default_rng(SEED)
    vel = {name: np.zeros_like(getattr(weights, name)) for name in ("W1", "b1", "W2", "b2", "W3", "b3")}
    epoch_logs = []
    for epoch in range(epochs):
        # TD samples loop
        order_td = rng.permutation(n_td) if n_td else np.array([], dtype=int)
        order_anchor = rng.permutation(n_anchor) if n_anchor else np.array([], dtype=int)
        td_loss_sum = 0.0
        anchor_loss_sum = 0.0
        td_seen = 0
        anchor_seen = 0
        for start in range(0, n_td, batch_size):
            idx = order_td[start:start + batch_size]
            X = densify(td_data["features_idx"], idx)
            bl = td_data["baselines"][idx]
            stm = td_data["stm"][idx]
            target = td_data["targets"][idx]
            pre1, h1, pre2, h2, out, v_pred = forward_with_value(X, bl, stm, weights)
            err = v_pred - target
            td_loss_sum += float(huber(err).sum())
            td_seen += idx.size
            d_v = huber_grad(err)
            tanh_d = 1.0 - v_pred ** 2
            sign = np.where(stm, 1.0, -1.0).astype(np.float32)
            d_loss_d_out = td_weight * d_v * tanh_d * sign * (EVAL_SCALE / VALUE_SCALE)
            y_eff = out[:, 0] - d_loss_d_out * (idx.size / 2.0)
            grads = cc._backward(X, y_eff, weights, pre1, h1, pre2, h2, out)
            for name, grad in zip(("W1", "b1", "W2", "b2", "W3", "b3"), grads):
                norm = float(np.linalg.norm(grad))
                if norm > GRAD_CLIP:
                    grad = grad * (GRAD_CLIP / norm)
                vel[name] = momentum * vel[name] - lr * grad
                getattr(weights, name)[...] += vel[name]
        # Anchor pass (tiny)
        for start in range(0, n_anchor, batch_size):
            idx = order_anchor[start:start + batch_size]
            X = densify(anchor_data["features_idx"], idx)
            bl = anchor_data["baselines"][idx]
            stm = anchor_data["stm"][idx]
            target = anchor_data["targets"][idx]
            pre1, h1, pre2, h2, out, v_pred = forward_with_value(X, bl, stm, weights)
            err = v_pred - target
            anchor_loss_sum += float(huber(err).sum())
            anchor_seen += idx.size
            d_v = huber_grad(err)
            tanh_d = 1.0 - v_pred ** 2
            sign = np.where(stm, 1.0, -1.0).astype(np.float32)
            d_loss_d_out = anchor_weight * d_v * tanh_d * sign * (EVAL_SCALE / VALUE_SCALE)
            y_eff = out[:, 0] - d_loss_d_out * (idx.size / 2.0)
            grads = cc._backward(X, y_eff, weights, pre1, h1, pre2, h2, out)
            for name, grad in zip(("W1", "b1", "W2", "b2", "W3", "b3"), grads):
                norm = float(np.linalg.norm(grad))
                if norm > GRAD_CLIP:
                    grad = grad * (GRAD_CLIP / norm)
                vel[name] = momentum * vel[name] - lr * grad
                getattr(weights, name)[...] += vel[name]
        td_mean = td_loss_sum / td_seen if td_seen else 0.0
        anchor_mean = anchor_loss_sum / anchor_seen if anchor_seen else 0.0
        epoch_logs.append({"epoch": epoch + 1, "td_huber": td_mean, "anchor_huber": anchor_mean})
        if epoch == 0 or (epoch + 1) % 3 == 0 or epoch == epochs - 1:
            print(f"    epoch {epoch+1:2d}/{epochs}: td_huber={td_mean:.4f} anchor_huber={anchor_mean:.4f}", flush=True)
    return epoch_logs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-weights", type=Path,
                    default=EXP6_ARTIFACTS / "curriculum" / "snapshots" / "chess_experiment_6_neural_stage02.npz")
    ap.add_argument("--iterations", type=int, default=N_ITERATIONS)
    ap.add_argument("--games-per-iter", type=int, default=N_SELFPLAY_GAMES_PER_ITER)
    ap.add_argument("--epochs-per-iter", type=int, default=EPOCHS_PER_ITER)
    ap.add_argument("--td-lambda", type=float, default=TD_LAMBDA)
    ap.add_argument("--anchor-samples", type=int, default=N_STOCKFISH_ANCHOR_SAMPLES)
    ap.add_argument("--lr", type=float, default=LR)
    args = ap.parse_args()

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    if not args.seed_weights.exists():
        print(f"missing seed weights {args.seed_weights}; abort")
        return 1
    weights = load_weights(args.seed_weights)
    print(f"loaded seed weights from {args.seed_weights}", flush=True)

    iter0_snap = SNAPSHOTS_DIR / "iter00_seed.npz"
    save_weights(iter0_snap, weights)

    print(f"\n=== Iter 0 (seed) staged-10 vs Stockfish ===", flush=True)
    init_results = cc.play_staged_test(iter0_snap)
    init_summ = cc.score_summary(init_results)
    print(f"  iter 0: {init_summ['W']}W/{init_summ['D']}D/{init_summ['L']}L "
          f"score={init_summ['total_score']:+d}/{init_summ['max_possible_score']} "
          f"(norm={cc.score_rate(init_results):.2%})", flush=True)

    iter_records = [{
        "iter": 0, "label": "seed",
        "snapshot_path": str(iter0_snap),
        "wins": init_summ["W"], "draws": init_summ["D"], "losses_g": init_summ["L"],
        "score_total": init_summ["total_score"],
        "score_max": init_summ["max_possible_score"],
        "score_rate": cc.score_rate(init_results),
    }]

    # Preload IMBALANCED candidate FENs for self-play diversity.
    # All-draw self-play happened even with random-FEN start because
    # most mid-game labels are nearly balanced and depth-2 search
    # converges to draws. Filter to positions where Stockfish cp shows
    # a clear advantage (|cp| > 100) — one side is already winning so
    # game has higher chance of decisive outcome. This produces real
    # TD signal (z = ±1 for many games).
    candidate_fens: list[str] = []
    if cc.LABELS_PATH.exists():
        n_total = 0
        with cc.LABELS_PATH.open() as f:
            for line in f:
                n_total += 1
                try:
                    rec = json.loads(line)
                    if abs(float(rec["cp_white"])) > 100.0:
                        candidate_fens.append(rec["fen"])
                except Exception:
                    pass
        print(f"  loaded {len(candidate_fens)} imbalanced (|cp|>100) start FENs from {n_total} labels", flush=True)

    for it in range(1, args.iterations + 1):
        print(f"\n=== Iter {it}/{args.iterations}: self-play + TD train ===", flush=True)
        cur_path = iter0_snap if it == 1 else snap_path_prev
        cur_weights = load_weights(cur_path)
        rng = random.Random(SEED + it)
        td_rows: list[dict] = []
        wins = draws = losses = 0
        avg_plies = 0
        t0 = time.perf_counter()
        for g in range(args.games_per_iter):
            # 60% of games start from a random labeled mid-game FEN, 40% from openings.
            use_random_fen = candidate_fens and rng.random() < RANDOM_FEN_START_PROB
            if use_random_fen:
                start_fen = rng.choice(candidate_fens)
                opening_moves: list[str] = []
                explore = 0
            else:
                _opening_id, opening_moves = cc.STAGED_OPENINGS[rng.randrange(len(cc.STAGED_OPENINGS))]
                explore = rng.choice(RANDOM_EXPLORE_PLIES_CHOICES)
                start_fen = None
            positions, outcome_w, reason = play_self_play_game_with_eval(
                cur_weights, opening_moves, explore, rng, start_fen=start_fen,
            )
            avg_plies += len(positions)
            if outcome_w > 0: wins += 1
            elif outcome_w < 0: losses += 1
            else: draws += 1
            td_rows.extend(build_td_targets(positions, outcome_w, args.td_lambda))
            if (g + 1) % 10 == 0 or g == args.games_per_iter - 1:
                dt = time.perf_counter() - t0
                print(f"    self-play {g+1}/{args.games_per_iter}: W{wins}/D{draws}/L{losses} "
                      f"plies_avg={avg_plies/(g+1):.1f}  {dt:.1f}s", flush=True)
        avg_plies /= args.games_per_iter

        print(f"  built {len(td_rows)} TD target rows from {args.games_per_iter} games (avg plies {avg_plies:.1f})", flush=True)
        td_data = precompute_td_rows(td_rows)
        anchor_data = sample_anchor_rows(args.anchor_samples, SEED + it + 999)
        print(f"  + {len(anchor_data['features_idx'])} Stockfish anchor samples", flush=True)

        t0 = time.perf_counter()
        epoch_logs = train_iter(
            weights, td_data, anchor_data,
            epochs=args.epochs_per_iter, lr=args.lr, momentum=MOMENTUM,
            batch_size=BATCH_SIZE, td_weight=TD_LOSS_WEIGHT, anchor_weight=ANCHOR_LOSS_WEIGHT,
        )
        train_dt = time.perf_counter() - t0
        print(f"  train {train_dt:.1f}s, final td_huber={epoch_logs[-1]['td_huber']:.4f} "
              f"anchor_huber={epoch_logs[-1]['anchor_huber']:.4f}", flush=True)

        snap_path = SNAPSHOTS_DIR / f"iter{it:02d}.npz"
        save_weights(snap_path, weights)
        snap_path_prev = snap_path

        # staged-10 evaluation
        print(f"  staged-10 vs Stockfish:", flush=True)
        t0 = time.perf_counter()
        results = cc.play_staged_test(snap_path)
        test_dt = time.perf_counter() - t0
        summ = cc.score_summary(results)
        sr = cc.score_rate(results)
        print(f"  staged-10 {test_dt:.1f}s: {summ['W']}W/{summ['D']}D/{summ['L']}L "
              f"score={summ['total_score']:+d}/{summ['max_possible_score']} (norm={sr:.2%})", flush=True)

        iter_records.append({
            "iter": it,
            "snapshot_path": str(snap_path),
            "n_selfplay_games": args.games_per_iter,
            "selfplay_W": wins, "selfplay_D": draws, "selfplay_L": losses,
            "selfplay_avg_plies": avg_plies,
            "n_td_rows": len(td_rows),
            "anchor_samples": len(anchor_data["features_idx"]),
            "final_td_huber": epoch_logs[-1]["td_huber"],
            "final_anchor_huber": epoch_logs[-1]["anchor_huber"],
            "train_seconds": round(train_dt, 2),
            "test_seconds": round(test_dt, 2),
            "wins": summ["W"], "draws": summ["D"], "losses_g": summ["L"],
            "score_total": summ["total_score"],
            "score_max": summ["max_possible_score"],
            "score_rate": sr,
            "td_lambda": args.td_lambda,
        })
        REPORT_JSON.write_text(json.dumps({"iterations": iter_records, "complete": (it == args.iterations)}, indent=2))

    # Final summary
    best = max(
        (r for r in iter_records if r["iter"] > 0),
        key=lambda r: (r["score_total"], r["wins"], r["draws"], -r["losses_g"], -r["iter"]),
    )
    print(f"\nBEST ITER (staged-10): {best['iter']} "
          f"({best['wins']}W/{best['draws']}D/{best['losses_g']}L score={best['score_total']:+d}/{best['score_max']})", flush=True)
    print(f"  snapshot: {best['snapshot_path']}", flush=True)
    print(f"\nNOTE: v8 is self-play TDLeaf. NOT auto-promoting.", flush=True)
    print(f"      Run chess_exp6_match.py {best['snapshot_path']} vs v6.2 S2 to gate.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
