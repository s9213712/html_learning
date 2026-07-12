#!/usr/bin/env python3
"""Exp6 v7: self-play TD-learning curriculum.

Replaces v6.x supervised-on-Stockfish-cp training with self-play
games. The NN plays both sides via alpha-beta depth-2 + quiescence
(same search the production engine uses). For every position visited
during a self-play game we record the FINAL game outcome from that
side's perspective; the NN is then trained to predict
``outcome * AMPLITUDE - static_baseline_cp_white`` (the deviation of
the game outcome from the hand-coded material+PST baseline).

The point (user's diagnosis):

    v3-v6 supervised on Stockfish-depth-4 cp was "blind fitting to
    specific game trajectories". The labels came from elite games
    the network would never play in. Training distribution did not
    match test distribution.

    Self-play uses positions the network ACTUALLY VISITS during
    play. The training signal is the actual outcome of those games
    — the only thing that matters for "did the model improve".

Each iteration produces one snapshot. Stop conditions:
  - N_ITERATIONS reached, or
  - improvement plateaus (heuristic, not implemented yet — manual
    stop is fine for now).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path
from typing import Iterable

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.chess import FEN_KEY  # noqa: E402
from services.games.chess_neural import (  # noqa: E402
    EVAL_SCALE, HIDDEN_1_DIM, HIDDEN_2_DIM, INPUT_DIM,
    NeuralWeights, active_features, load_weights, make_initial_weights,
    save_weights, static_baseline_cp_white,
)
from services.games.chess_exp6 import choose_experiment_neural_move  # noqa: E402
from scripts.games.common_paths import exp6_artifacts_root  # noqa: E402

# Reuse the curriculum's staged-10 test harness + training mechanics.
sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402


# ── v7.0 hyperparameters (per user design feedback) ────────────────
# Self-play TD-learning with composite loss:
#   loss = α * self_play_outcome_loss + β * stockfish_cp_aux_loss
# Stockfish aux signal is the v6.2 cached label set; serves as
# anchor / sanity prior to prevent pure-self-play model collapse
# (model becomes confident in its own blind spots).
#
# Self-play search depth raised from balanced (depth 2, quiescence 2)
# to fixed_depth_d3 (depth 3, quiescence 3). Per user note: at
# depth 2 too many tactics are invisible and the outcome signal is
# too noisy.
#
# Target shape: outcome × OUTCOME_AMPLITUDE is intentionally
# moderate (not pegged to ±1000 cp). The composite loss further
# damps it via β-weighted Stockfish cp anchor.
N_ITERATIONS = 8                # number of self-play / train rounds
N_SELFPLAY_GAMES_PER_ITER = 30  # depth-3 self-play is slower, fewer games
N_STOCKFISH_AUX_SAMPLES = 5000  # cached cp samples mixed in per iter
SELFPLAY_MAX_PLIES = 200
SELFPLAY_OPENINGS = cc.STAGED_OPENINGS
SELFPLAY_SEARCH_PROFILE = "fixed_depth_d3"  # depth 3 + quiescence 3
EPOCHS_PER_ITER = 20
OUTCOME_AMPLITUDE = 400.0        # outcome ±1 → ±400 cp target
SELFPLAY_LOSS_WEIGHT = 1.0       # α — primary self-play signal
STOCKFISH_AUX_LOSS_WEIGHT = 0.3  # β — Stockfish cp anchor
PERSISTENT_DIR = exp6_artifacts_root() / "selfplay_curriculum"
SNAPSHOTS_DIR = PERSISTENT_DIR / "snapshots"
LOG_PATH = PERSISTENT_DIR / "selfplay.log"
REPORT_JSON = PERSISTENT_DIR / "report.json"
STOCKFISH_LABELS_PATH = cc.LABELS_PATH  # 97k cached labels from v3-v6


def _board_state_from_chess(board: chess.Board) -> dict:
    state = {chess.square_name(sq): p.symbol() for sq, p in board.piece_map().items()}
    state[FEN_KEY] = board.fen()
    return state


def _game_outcome_white(board: chess.Board) -> float:
    """+1 if white wins, -1 if black wins, 0 otherwise."""
    result = board.result(claim_draw=True)
    if result == "1-0":
        return 1.0
    if result == "0-1":
        return -1.0
    return 0.0


def play_self_play_game(weights_path: Path, opening_id: str, opening_moves: list[str],
                        max_plies: int = SELFPLAY_MAX_PLIES,
                        random_explore_plies: int = 0,
                        rng: random.Random | None = None) -> tuple[list[tuple[str, bool]], float, int, str]:
    """Play one self-play game. Returns (positions, outcome_white, plies, reason).
    positions = [(fen, side_to_move_is_white), ...] — recorded BEFORE each move.

    ``random_explore_plies``: after the opening book moves, play this
    many additional plies as RANDOM legal moves before NN-driven play
    begins. Avoids deterministic self-play loops (user note: pure
    deterministic self-play converges to a narrow distribution and
    model learns own blind spots).
    """
    if rng is None:
        rng = random.Random()
    board = chess.Board()
    positions: list[tuple[str, bool]] = []
    invalid = None
    for uci in opening_moves:
        try:
            mv = chess.Move.from_uci(uci)
            if mv in board.legal_moves:
                board.push(mv)
            else:
                break
        except Exception:
            break
    # Optional random exploration ply(s) — diversify the start
    for _ in range(random_explore_plies):
        if board.is_game_over(claim_draw=True):
            break
        legal = list(board.legal_moves)
        if not legal:
            break
        board.push(rng.choice(legal))
    for ply in range(max_plies):
        if board.is_game_over(claim_draw=True):
            break
        positions.append((board.fen(), board.turn == chess.WHITE))
        side_name = "white" if board.turn == chess.WHITE else "black"
        try:
            payload = choose_experiment_neural_move(
                _board_state_from_chess(board), side_name,
                weights_path=str(weights_path),
                search_profile=SELFPLAY_SEARCH_PROFILE,
            )
        except Exception as exc:
            invalid = ("crash", str(exc)); break
        if not payload:
            invalid = ("no_move", side_name); break
        promo = payload.get("promotion") or ""
        try:
            move = chess.Move.from_uci(f"{payload['from']}{payload['to']}{promo}")
        except Exception:
            invalid = ("bad_uci", str(payload)); break
        if move not in board.legal_moves:
            invalid = ("illegal", str(move)); break
        board.push(move)
    outcome_white = _game_outcome_white(board)
    reason = (
        board.result(claim_draw=True)
        if invalid is None else f"invalid:{invalid[0]}"
    )
    return positions, outcome_white, board.ply(), reason


def gather_selfplay_data(weights_path: Path, n_games: int, seed: int) -> list[tuple[str, float, float]]:
    """Return rows of (fen, target_cp_white, static_baseline_cp_white)
    where target_cp_white = outcome_white * OUTCOME_AMPLITUDE.
    """
    rows: list[tuple[str, float, float]] = []
    rng = random.Random(seed)
    win_count = draw_count = loss_count = 0
    plies_total = 0
    t0 = time.perf_counter()
    for i in range(n_games):
        opening_id, opening_moves = SELFPLAY_OPENINGS[rng.randrange(len(SELFPLAY_OPENINGS))]
        # Vary random_explore_plies: most games 2 random plies, occasional 0/4
        explore_plies = rng.choice([0, 2, 2, 2, 4])
        positions, outcome_white, plies, reason = play_self_play_game(
            weights_path, opening_id, opening_moves,
            random_explore_plies=explore_plies, rng=rng,
        )
        plies_total += plies
        if outcome_white > 0: win_count += 1
        elif outcome_white < 0: loss_count += 1
        else: draw_count += 1
        # All recorded positions get the same white-perspective outcome label.
        target_cp_white = outcome_white * OUTCOME_AMPLITUDE
        for fen, _stm_is_white in positions:
            board = chess.Board(fen)
            baseline = static_baseline_cp_white(board)
            rows.append((fen, target_cp_white, baseline))
        if (i + 1) % 10 == 0 or i == n_games - 1:
            dt = time.perf_counter() - t0
            print(f"    self-play {i+1}/{n_games}: W{win_count}/D{draw_count}/L{loss_count} "
                  f"plies_avg={plies_total/(i+1):.1f}  {dt:.1f}s", flush=True)
    return rows


def sample_stockfish_aux(n_samples: int, seed: int) -> list[tuple[str, float, float]]:
    """Sample n_samples rows from cached Stockfish-cp labels. Returns
    same shape as self-play rows: (fen, target_cp_white, baseline).
    Stockfish labels have cp_white field; we cap to ±CP_CLIP_ABS to
    suppress mate-score outliers (matches v4/v5 cp-clip behavior).
    """
    if not STOCKFISH_LABELS_PATH.exists():
        return []
    rng = random.Random(seed)
    all_recs = []
    with STOCKFISH_LABELS_PATH.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
                all_recs.append(rec)
            except Exception:
                continue
    if not all_recs:
        return []
    picked = rng.sample(all_recs, min(n_samples, len(all_recs)))
    rows: list[tuple[str, float, float]] = []
    CP_CLIP_ABS = cc.CP_CLIP_ABS
    for rec in picked:
        fen = rec["fen"]
        cp_raw = float(rec["cp_white"])
        cp = max(-CP_CLIP_ABS, min(CP_CLIP_ABS, cp_raw))
        board = chess.Board(fen)
        baseline = static_baseline_cp_white(board)
        rows.append((fen, cp, baseline))
    return rows


def train_composite(selfplay_rows: list[tuple[str, float, float]],
                    stockfish_rows: list[tuple[str, float, float]],
                    weights: NeuralWeights, *, epochs: int,
                    alpha: float, beta: float) -> list[float]:
    """Composite loss training:
        loss = α * MSE(NN, selfplay_target_residual)
             + β * MSE(NN, stockfish_cp_target_residual)
    Implemented as a single dataset with per-sample weight: self-play
    rows weight α, Stockfish aux rows weight β. NN predicts
    (target_cp_white - baseline) / EVAL_SCALE in both cases.
    """
    all_rows = selfplay_rows + stockfish_rows
    n = len(all_rows)
    if n == 0:
        return []
    boards = [chess.Board(row[0]) for row in all_rows]
    features_idx = [np.asarray(active_features(b), dtype=np.int64) for b in boards]
    targets_cp = np.array([row[1] for row in all_rows], dtype=np.float32)
    baselines = np.array([row[2] for row in all_rows], dtype=np.float32)
    labels = ((targets_cp - baselines) / EVAL_SCALE).astype(np.float32)
    sample_weights = np.array(
        [alpha] * len(selfplay_rows) + [beta] * len(stockfish_rows),
        dtype=np.float32,
    )
    rng = np.random.default_rng(42)
    vel = {name: np.zeros_like(getattr(weights, name)) for name in ("W1", "b1", "W2", "b2", "W3", "b3")}
    losses: list[float] = []
    LOG_AT = {0, epochs - 1} | set(range(4, epochs, 5))
    for epoch in range(epochs):
        order = rng.permutation(n)
        ep_loss_sp = 0.0
        ep_loss_sf = 0.0
        ep_w_sp = 0.0
        ep_w_sf = 0.0
        for start in range(0, n, cc.BATCH_SIZE):
            idx = order[start:start + cc.BATCH_SIZE]
            X = cc.densify(features_idx, idx)
            y = labels[idx]
            w = sample_weights[idx]
            pre1, h1, pre2, h2, out = cc._forward(X, weights)
            err = out[:, 0] - y
            per_sample = err * err
            # Per-loss-component tracking
            sp_mask = w >= alpha - 1e-6  # heuristic split
            ep_loss_sp += float((per_sample[sp_mask]).sum())
            ep_loss_sf += float((per_sample[~sp_mask]).sum())
            ep_w_sp += float(sp_mask.sum())
            ep_w_sf += float((~sp_mask).sum())
            # Weighted MSE gradient. The custom _backward path uses
            # mean over samples; we mimic that with weighted mean.
            # Simplest: scale labels' contribution via duplicating
            # — but per-sample weighting via gradient scaling is
            # easier. Use closure: multiply (out - y) by sqrt(w) for
            # both forward loss and backward grad.
            # Actually cc._backward computes grad from (out - y) so
            # we scale y to inject weight: use (out - y) * w as the
            # effective residual, by replacing y with (out - w*(out - y)).
            # Cleaner: just multiply input/output through proper
            # weighting. For simplicity, we approximate by training
            # with uniform weight (alpha == beta would give equal),
            # but call _backward with the original y. To honor the
            # weights, we shrink stockfish samples by setting their
            # effective y closer to the model's current prediction —
            # achieved by linear interpolation:
            #     y_effective[i] = (1 - w[i]) * out[:,0][i] + w[i] * y[i]
            # so when w=1 the sample contributes fully, when w<1 the
            # sample contributes less (gradient toward its label is
            # smaller). max weight = max(alpha, beta) = 1.0 normally.
            max_w = max(alpha, beta)
            w_norm = w / max_w if max_w > 0 else w
            y_eff = (1.0 - w_norm) * out[:, 0] + w_norm * y
            grads = cc._backward(X, y_eff, weights, pre1, h1, pre2, h2, out)
            for name, grad in zip(("W1", "b1", "W2", "b2", "W3", "b3"), grads):
                norm = float(np.linalg.norm(grad))
                if norm > cc.GRAD_CLIP:
                    grad = grad * (cc.GRAD_CLIP / norm)
                vel[name] = cc.MOMENTUM * vel[name] - cc.LR * grad
                getattr(weights, name)[...] += vel[name]
        mean_loss_sp = ep_loss_sp / ep_w_sp if ep_w_sp else 0.0
        mean_loss_sf = ep_loss_sf / ep_w_sf if ep_w_sf else 0.0
        losses.append((mean_loss_sp + mean_loss_sf) / 2)
        if epoch in LOG_AT:
            print(f"    epoch {epoch+1:3d}/{epochs}: loss_sp={mean_loss_sp:.4f}  loss_sf={mean_loss_sf:.4f}", flush=True)
    return losses


# Back-compat alias used in main()
def train_on_selfplay(rows, weights, *, epochs):
    return train_composite(rows, [], weights, epochs=epochs, alpha=1.0, beta=0.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-weights", type=Path,
                        default=exp6_artifacts_root() / "curriculum" / "snapshots" / "chess_experiment_6_neural_stage02.npz",
                        help="Starting weights file. Default: v6.2 best stage (S2).")
    parser.add_argument("--iterations", type=int, default=N_ITERATIONS)
    parser.add_argument("--games-per-iter", type=int, default=N_SELFPLAY_GAMES_PER_ITER)
    parser.add_argument("--aux-samples", type=int, default=N_STOCKFISH_AUX_SAMPLES,
                        help="Stockfish-cp aux samples mixed into each iter's training (anchor / collapse prevention).")
    parser.add_argument("--epochs-per-iter", type=int, default=EPOCHS_PER_ITER)
    args = parser.parse_args()

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Load seed weights ────────────────────────────────────────
    if args.seed_weights.exists():
        weights = load_weights(args.seed_weights)
        print(f"loaded seed weights from {args.seed_weights}", flush=True)
    else:
        weights = make_initial_weights(seed=20260516)
        print(f"seed weights file missing; using fresh random init", flush=True)

    iter0_snap = SNAPSHOTS_DIR / "iter00_seed.npz"
    save_weights(iter0_snap, weights)

    # ── Iter 0 baseline staged-10 ─────────────────────────────────
    print("\n=== Iter 0 (seed) staged-10 vs Stockfish ===", flush=True)
    init_results = cc.play_staged_test(iter0_snap)
    init_summary = cc.score_summary(init_results)
    print(
        f"  iter 0 seed: {init_summary['W']}W/{init_summary['D']}D/{init_summary['L']}L "
        f"score={init_summary['total_score']:+d}/{init_summary['max_possible_score']} "
        f"(norm={cc.score_rate(init_results):.2%})",
        flush=True,
    )

    iter_records: list[dict] = [{
        "iter": 0, "label": "seed",
        "snapshot_path": str(iter0_snap),
        "wins": init_summary["W"], "draws": init_summary["D"], "losses_g": init_summary["L"],
        "score_total": init_summary["total_score"],
        "score_max": init_summary["max_possible_score"],
        "score_rate": cc.score_rate(init_results),
    }]

    for it in range(1, args.iterations + 1):
        print(f"\n=== Iter {it}/{args.iterations}: self-play + train ===", flush=True)
        seed_for_iter = 20260516 + it
        sp_rows = gather_selfplay_data(iter0_snap if it == 1 else snap_path_prev,
                                        args.games_per_iter, seed_for_iter)
        sf_rows = sample_stockfish_aux(args.aux_samples, seed_for_iter + 9999)
        print(f"  collected {len(sp_rows)} self-play positions + {len(sf_rows)} Stockfish aux samples", flush=True)

        t0 = time.perf_counter()
        losses = train_composite(sp_rows, sf_rows, weights,
                                  epochs=args.epochs_per_iter,
                                  alpha=SELFPLAY_LOSS_WEIGHT,
                                  beta=STOCKFISH_AUX_LOSS_WEIGHT)
        train_dt = time.perf_counter() - t0
        final_loss = losses[-1] if losses else None
        print(f"  train {train_dt:.1f}s, final composite loss {final_loss}", flush=True)

        snap_path = SNAPSHOTS_DIR / f"iter{it:02d}.npz"
        save_weights(snap_path, weights)
        snap_path_prev = snap_path

        # Staged-10 evaluation
        print(f"  staged-5 vs Stockfish (depths {cc.STAGED_DEPTHS}):", flush=True)
        t0 = time.perf_counter()
        results = cc.play_staged_test(snap_path)
        test_dt = time.perf_counter() - t0
        summ = cc.score_summary(results)
        sr = cc.score_rate(results)
        print(f"  staged-5 {test_dt:.1f}s: {summ['W']}W/{summ['D']}D/{summ['L']}L "
              f"score={summ['total_score']:+d}/{summ['max_possible_score']} (norm={sr:.2%})", flush=True)
        iter_records.append({
            "iter": it,
            "snapshot_path": str(snap_path),
            "n_selfplay_games": args.games_per_iter,
            "n_selfplay_positions": len(sp_rows),
            "n_stockfish_aux": len(sf_rows),
            "final_loss": final_loss,
            "train_seconds": round(train_dt, 2),
            "test_seconds": round(test_dt, 2),
            "wins": summ["W"], "draws": summ["D"], "losses_g": summ["L"],
            "score_total": summ["total_score"],
            "score_max": summ["max_possible_score"],
            "score_rate": sr,
            "selfplay_loss_weight": SELFPLAY_LOSS_WEIGHT,
            "stockfish_aux_loss_weight": STOCKFISH_AUX_LOSS_WEIGHT,
            "search_profile": SELFPLAY_SEARCH_PROFILE,
        })
        REPORT_JSON.write_text(json.dumps({"iterations": iter_records, "complete": (it == args.iterations)}, indent=2))

    # Pick best iter (non-zero) BY STAGED-5 SCORE — for reporting only.
    # v7.0 is a PIPELINE SMOKE TEST. Per user design feedback:
    # "loss 下降可能只是模型更會預測自己的 self-play distribution，
    # 不代表更會下棋." So DO NOT auto-promote to runtime here.
    # Promotion requires a separate match against the current best
    # (see chess_exp6_match.py for v7.1 gating).
    best = max(
        (r for r in iter_records if r["iter"] > 0),
        key=lambda r: (r["score_total"], r["wins"], r["draws"], -r["losses_g"], -r["iter"]),
    )
    print(f"\nBEST ITER (by staged-5): {best['iter']} ({best['wins']}W/{best['draws']}D/{best['losses_g']}L "
          f"score={best['score_total']:+d}/{best['score_max']})", flush=True)
    print(f"  snapshot: {best['snapshot_path']}", flush=True)
    print(f"\nNOTE: v7.0 is a pipeline smoke test. NOT auto-promoting to runtime.", flush=True)
    print(f"      Run match-gating (v7.1) before claiming strength gain.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
