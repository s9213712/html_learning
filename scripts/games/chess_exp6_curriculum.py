"""Exp6 curriculum learning experiment.

10 stages × 100 positions each (cumulative). After each stage:
- save snapshot ``chess_experiment_6_neural_stage{N}.npz``
- run staged-5 vs Stockfish at depths 1, 2, 3, 4, 5
- record W/D/L

Output:
- snapshots/   per-stage .npz files
- curriculum_report.json  full experimental record
- curriculum_report.md    human-readable summary

The script is intentionally self-contained so it can be re-run for
later experiments without depending on the Exp5 blockfish_match
plumbing.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import chess  # noqa: E402
import numpy as np  # noqa: E402

from services.games.chess import FEN_KEY  # noqa: E402
from services.games.chess_neural import (  # noqa: E402
    EVAL_SCALE, HIDDEN_1_DIM, HIDDEN_2_DIM, INPUT_DIM,
    NeuralWeights, active_features, load_weights,
    make_initial_weights, save_weights,
)
from services.games.chess_stockfish_teacher import (  # noqa: E402
    UciStockfish, analysis_limit, resolve_stockfish_path,
)
from services.games.chess_exp6 import (  # noqa: E402
    EXP6_DEFAULT_SEARCH_PROFILE,
    choose_experiment_neural_move,
)
from scripts.games.common_paths import exp6_artifacts_root, exp6_private_dir, runtime_model_path  # noqa: E402


STOCKFISH_BIN = resolve_stockfish_path(os.environ.get("STOCKFISH_PATH", ""))
# Quality games come from ``chess_exp6_download_quality.py``. The script
# produces ``quality_1000_games.jsonl`` after filtering multi-source
# downloads through the elite gate.
OUT_DIR = exp6_private_dir()
SOURCE_GAMES_JSONL = OUT_DIR / "quality_1000_games.jsonl"
LABELS_PATH = OUT_DIR / "curriculum_labels.jsonl"
# Per user spec: snapshots + reports live outside the repo tree so a
# /tmp wipe or repo cleanup can't take them out. Includes the stage 00
# random-init snapshot for full reproducibility of the curriculum.
PERSISTENT_DIR = exp6_artifacts_root() / "curriculum"
SNAPSHOTS_DIR = PERSISTENT_DIR / "snapshots"
REPORT_JSON = PERSISTENT_DIR / "curriculum_report.json"
REPORT_MD = PERSISTENT_DIR / "curriculum_report.md"
# Mirror the report into the configured external runtime for operators. The
# standalone artifact copy is the source of truth for this run.
REPORT_JSON_RUNTIME = OUT_DIR / "curriculum_report.json"
REPORT_MD_RUNTIME = OUT_DIR / "curriculum_report.md"

# 1000 games × ~50 latter-half plies ≈ 50000 positions. We label every
# latter-50% position with Stockfish but split the GAMES into 10
# cumulative stages of 100 games each (per user spec).
GAMES_PER_STAGE = 100
N_STAGES = 10
TOTAL_GAMES = GAMES_PER_STAGE * N_STAGES
# v6 PRIMARY FIX: material-residual eval.
#   v3/v4/v5 all hit +6 ceiling vs staged Stockfish 1-5 despite clean
#   loss convergence (v5 final loss 0.55-1.0). Engine comparison
#   showed exp3 (-22 score, 49 hand-crafted move features) and
#   exp4 (-22, policy+value with same features) both beat exp5/exp6
#   (-28, raw piece-square only). Root cause: 200K-param NN spends
#   most of its capacity re-learning piece values from cp labels,
#   leaving little for positional patterns.
#
#   Fix: ``evaluate_board`` now composes material_balance(board)
#   + NN_residual(board). The NN learns deviation from material,
#   not absolute cp. Untrained random init -> material-only eval
#   (matches exp0/-34 baseline immediately). Training target is
#   (blended_cp - material_white) instead of blended_cp.
#
# v4 audit fixes (still in effect):
#
#   #1 CP_CLIP_ABS: clip teacher cp to ±1000 before blending. The
#      raw labels include mate-score outliers (100000 - dist) that
#      after blending give targets ~30000 cp (300× normal). MSE
#      loss was dominated by these outliers and pulled all weights
#      toward fitting them — useless for depth-2 search since alpha-
#      beta already handles mate.
#
#   #4 cold-start curriculum: each stage re-initializes from
#      random and trains on the cumulative dataset from scratch.
#      v3 warm-start compounded a bad direction at S6+ (loss kept
#      dropping but staged score collapsed). Cold-start tests each
#      stage independently and lets us pick a best on merit, not
#      on accumulated drift.
#
#   #5 drop latter-50% cut: sample ALL plies (LATTER_HALF_FRACTION
#      = 0.0). v3 was endgame-biased (training only on latter 50%);
#      staged-test schedule starts from openings (start/open_game/
#      queen_pawn/sicilian/english) so train/test distributions
#      were mismatched.
#
# Untouched v4 (deferred to v5 if v4 still hits ceiling):
#   #2 encoding bits — actually applied in chess_neural.py
#      (INPUT_DIM 768→774) since cost was low and the fix is
#      mechanical. Bumps gradient surface, no other downside.
#   #3 ranking loss / multipv labels — deferred: needs full
#      re-labelling at multipv ≥ 2 (~2-3 hr extra compute).
LATTER_HALF_FRACTION = 1.0  # v4 fix #5: record 100% of plies (was 0.5 endgame-biased)
CP_CLIP_ABS = 1000.0  # v4 fix #1: clip teacher cp to ±1000 before blending
COLD_START_PER_STAGE = True  # v4 fix #4: re-init weights each stage
# v5 fix: v4 underfit each stage because 8 epochs from cold-start
# cannot converge on growing cumulative data. v4 final_loss went UP
# across stages (2.45 → 3.18) — opposite of what curriculum learning
# should produce. v5 increases epochs to 50 so each cold-start stage
# can actually converge.
EPOCHS_PER_STAGE = 50
BATCH_SIZE = 128
LR = 0.002
MOMENTUM = 0.9
GRAD_CLIP = 1.0
STOCKFISH_DEPTH_FOR_LABEL = 4
OUTCOME_BLEND = 0.0  # v6.1: residual NN learns pure cp residual (not outcome). Outcome blend was injecting noise the NN couldn't generalize.
OUTCOME_CP_AMPLITUDE = 400.0  # white win contributes +400 cp influence
STAGED_OPENINGS = [
    ("start", []),
    ("open_game", ["e2e4", "e7e5"]),
    ("queen_pawn", ["d2d4", "d7d5"]),
    ("sicilian", ["e2e4", "c7c5"]),
    ("english", ["c2c4", "g8f6"]),
]
# Two games per Stockfish depth, alternating colours so each depth
# tests both white-side and black-side Exp6 play. Total 10 games per
# staged check.
STAGED_DEPTHS = [1, 2, 3, 4, 5]
STAGED_GAMES_PER_DEPTH = 2
# Score formula: win = +4, draw = -1, loss = -4 (per user spec).
# Punishes draws relative to wins so the comparison rewards attempts
# at conversion rather than threefold-cycle escapes.
SCORE_WIN = 4
SCORE_DRAW = -1
SCORE_LOSS = -4


_SHUFFLE_SEED = 20260516


def load_games(target: int) -> list[dict]:
    """Read the curriculum input JSONL, shuffle deterministically with
    ``_SHUFFLE_SEED``, and take the first ``target`` games.

    Shuffling prevents stage 1 / stage 2 / ... from being dominated by
    one source (e.g. all DrNykterstein then all TWIC). With per-stage
    cumulative training, this means every stage's added 100 games is
    a random mix of all data sources — necessary for the learning
    curve to reflect generalisation rather than source-distribution
    artefacts.
    """
    games: list[dict] = []
    if not SOURCE_GAMES_JSONL.exists():
        raise SystemExit(
            f"missing {SOURCE_GAMES_JSONL}; run "
            "scripts/games/chess_exp6_download_quality.py first."
        )
    with SOURCE_GAMES_JSONL.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            games.append(json.loads(line))
    import random
    rng = random.Random(_SHUFFLE_SEED)
    rng.shuffle(games)
    if target and target < len(games):
        games = games[:target]
    return games


def _game_outcome_white(rec: dict) -> float:
    """Return per-game outcome from white's perspective in [-1, +1].
    White wins -> +1, black wins -> -1, draw or unknown -> 0."""
    winner = (rec.get("winner_color") or "").lower().strip()
    if winner == "white":
        return 1.0
    if winner == "black":
        return -1.0
    # Some replay rows use ``result`` field like "1-0"/"0-1"/"1/2-1/2"
    result = (rec.get("result") or "").strip()
    if result == "1-0":
        return 1.0
    if result == "0-1":
        return -1.0
    return 0.0


def collect_positions_latter_half(games: list[dict]) -> list[tuple[int, str, float]]:
    """For each game, replay the moves and keep FENs from the latter
    ``LATTER_HALF_FRACTION`` of plies. v4 (audit fix #5) sets
    ``LATTER_HALF_FRACTION = 0.0`` so this returns ALL plies — train
    distribution now matches the opening-heavy staged-test schedule.
    Returns a list of (game_index, fen, game_outcome_white) tuples
    ordered by source game. ``game_outcome_white`` is the final game
    outcome from white's perspective in [-1, +1] (constant across all
    positions extracted from the same game).

    Per-game extraction keeps games disjoint across stages so the
    100-games-per-stage curriculum can be sliced cleanly.
    """
    out: list[tuple[int, str, float]] = []
    for gi, rec in enumerate(games):
        moves = rec.get("move_history") or []
        n = len(moves)
        if n < 8:
            continue
        outcome_w = _game_outcome_white(rec)
        cutoff = max(1, int(n * (1.0 - LATTER_HALF_FRACTION)))
        board = chess.Board()
        # advance through the first half without recording
        for i in range(cutoff):
            uci = moves[i].get("uci") if isinstance(moves[i], dict) else str(moves[i])
            try:
                mv = chess.Move.from_uci(uci)
                if mv not in board.legal_moves:
                    break
                board.push(mv)
            except Exception:
                break
        # latter half — record after each push
        for i in range(cutoff, n):
            uci = moves[i].get("uci") if isinstance(moves[i], dict) else str(moves[i])
            try:
                mv = chess.Move.from_uci(uci)
                if mv not in board.legal_moves:
                    break
                board.push(mv)
            except Exception:
                break
            out.append((gi, board.fen(), outcome_w))
    return out


def label_positions(positions: list[tuple[int, str, float]]) -> list[tuple[int, str, float, float, float]]:
    """Stockfish-label each (game_index, fen, outcome_white) tuple.
    Returns (game_index, fen, cp_white_d{STOCKFISH_DEPTH_FOR_LABEL},
    outcome_white, blended_cp_white).

    ``blended_cp_white`` mixes the depth-${STOCKFISH_DEPTH_FOR_LABEL}
    cp estimate with the game's final outcome scaled to cp units:

        blended = (1 - OUTCOME_BLEND) * cp_d{N}
                + OUTCOME_BLEND * outcome_white * OUTCOME_CP_AMPLITUDE

    The NN regresses against ``blended_cp_white`` so the training
    target is self-consistent with the runtime search horizon
    (matching depth) AND grounded in actual game results (immune to
    per-position Stockfish low-depth noise).
    """
    rows: list[tuple[int, str, float, float, float]] = []
    limit = analysis_limit(depth=STOCKFISH_DEPTH_FOR_LABEL, movetime_ms=0)
    t0 = time.perf_counter()
    with UciStockfish(STOCKFISH_BIN) as engine:
        for i, (game_idx, fen, outcome_w) in enumerate(positions, 1):
            board = chess.Board(fen)
            if board.is_game_over():
                continue
            try:
                pv = engine.analyse(board, limit=limit, multipv=1)
            except Exception:
                continue
            if not pv:
                continue
            cp = pv[0].get("teacher_eval_cp")
            if cp is None:
                continue
            cp_white = float(cp) if board.turn == chess.WHITE else -float(cp)
            # v4 fix #1: clip teacher cp to ±CP_CLIP_ABS to suppress
            # mate-score outliers (raw mate cp = 100000 - dist). Mate
            # positions are already handled correctly by alpha-beta;
            # the regressor only needs reasonable cp magnitudes.
            cp_white = max(-CP_CLIP_ABS, min(CP_CLIP_ABS, cp_white))
            blended = (
                (1.0 - OUTCOME_BLEND) * cp_white
                + OUTCOME_BLEND * outcome_w * OUTCOME_CP_AMPLITUDE
            )
            rows.append((game_idx, fen, cp_white, outcome_w, blended))
            if i % 500 == 0:
                print(f"  labelled {i}/{len(positions)} ({time.perf_counter()-t0:.1f}s)", flush=True)
    return rows


def _crelu(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0.0, 127.0)


def _crelu_grad(pre: np.ndarray) -> np.ndarray:
    return ((pre > 0.0) & (pre < 127.0)).astype(np.float32)


def _forward(X: np.ndarray, w: NeuralWeights):
    pre1 = X @ w.W1 + w.b1
    h1 = _crelu(pre1)
    pre2 = h1 @ w.W2 + w.b2
    h2 = _crelu(pre2)
    y = h2 @ w.W3 + w.b3
    return pre1, h1, pre2, h2, y


def _backward(X, y_true, w, pre1, h1, pre2, h2, y_pred):
    n = X.shape[0]
    err = y_pred[:, 0] - y_true
    dy = (2.0 / n) * err.reshape(-1, 1)
    dW3 = h2.T @ dy
    db3 = dy.sum(axis=0)
    dh2 = dy @ w.W3.T
    dpre2 = dh2 * _crelu_grad(pre2)
    dW2 = h1.T @ dpre2
    db2 = dpre2.sum(axis=0)
    dh1 = dpre2 @ w.W2.T
    dpre1 = dh1 * _crelu_grad(pre1)
    dW1 = X.T @ dpre1
    db1 = dpre1.sum(axis=0)
    return dW1, db1, dW2, db2, dW3, db3


def densify(features_idx, indices: np.ndarray) -> np.ndarray:
    X = np.zeros((indices.size, INPUT_DIM), dtype=np.float32)
    for r, src in enumerate(indices):
        X[r, features_idx[src]] = 1.0
    return X


def train_cumulative(labelled, weights: NeuralWeights, *, epochs: int):
    """``labelled`` rows are (game_idx, fen, cp_d{N}, outcome_white,
    blended_cp).

    v6.2 RESIDUAL TARGET on static baseline (material + PST): give
    the NN a chess-knowledge head start so 200K params don't need to
    re-derive piece values or basic positional logic. The static
    baseline captures: piece values + knight/bishop centralization +
    pawn advancement + king safety on first rank. Target =
    (blended_cp - static_baseline_cp_white(board)) / EVAL_SCALE; the
    NN now only has to learn subtle deviations the PSTs can't
    express (king attack threats, passed-pawn races, etc.). At
    inference, ``evaluate_board`` adds static_baseline back.
    """
    from services.games.chess_neural import static_baseline_cp_white  # local to avoid top-level cycles
    n = len(labelled)
    boards = [chess.Board(row[1]) for row in labelled]
    features_idx = [np.asarray(active_features(b), dtype=np.int64) for b in boards]
    baselines = np.array([static_baseline_cp_white(b) for b in boards], dtype=np.float32)
    raw_targets = np.array([row[4] for row in labelled], dtype=np.float32)
    labels = ((raw_targets - baselines) / EVAL_SCALE).astype(np.float32)
    rng = np.random.default_rng(42)
    vel = {name: np.zeros_like(getattr(weights, name)) for name in ("W1", "b1", "W2", "b2", "W3", "b3")}
    losses: list[float] = []
    # v5: log intermediate loss so we can see WITHIN-stage convergence
    # (v4 only logged final_loss; we couldn't tell if more epochs would
    # have helped). Print at epoch 0, every 10 epochs, and final.
    log_milestones = {0, epochs - 1} | set(range(9, epochs, 10))
    for epoch in range(epochs):
        order = rng.permutation(n)
        ep_loss = 0.0
        seen = 0
        for start in range(0, n, BATCH_SIZE):
            idx = order[start:start + BATCH_SIZE]
            X = densify(features_idx, idx)
            y = labels[idx]
            pre1, h1, pre2, h2, out = _forward(X, weights)
            loss = float(((out[:, 0] - y) ** 2).mean())
            if not np.isfinite(loss):
                return losses
            ep_loss += loss * idx.size
            seen += idx.size
            grads = _backward(X, y, weights, pre1, h1, pre2, h2, out)
            for name, grad in zip(("W1", "b1", "W2", "b2", "W3", "b3"), grads):
                norm = float(np.linalg.norm(grad))
                if norm > GRAD_CLIP:
                    grad = grad * (GRAD_CLIP / norm)
                vel[name] = MOMENTUM * vel[name] - LR * grad
                getattr(weights, name)[...] += vel[name]
        mean_loss = ep_loss / seen if seen else 0.0
        losses.append(mean_loss)
        if epoch in log_milestones:
            print(f"    epoch {epoch+1:3d}/{epochs}: loss={mean_loss:.4f}", flush=True)
    return losses


def play_one_game(opening_id: str, opening_moves: list[str], exp6_color_name: str,
                  weights_path: Path, stockfish_depth: int, engine: UciStockfish,
                  max_plies: int = 400) -> dict:
    import resource  # rusage for process-level CPU/MEM at game start/end
    import statistics
    board = chess.Board()
    for u in opening_moves:
        board.push_uci(u)
    exp6_color = chess.WHITE if exp6_color_name == "white" else chess.BLACK
    os.environ["EXP6_NEURAL_WEIGHTS_PATH"] = str(weights_path)
    invalid_actor = None
    exp6_decision_seconds: list[float] = []
    sf_decision_seconds: list[float] = []
    rusage0 = resource.getrusage(resource.RUSAGE_SELF)
    wall0 = time.perf_counter()
    for ply in range(max_plies):
        if board.is_game_over(claim_draw=True):
            break
        if board.turn == exp6_color:
            state = {chess.square_name(sq): p.symbol() for sq, p in board.piece_map().items()}
            state[FEN_KEY] = board.fen()
            t_move = time.perf_counter()
            payload = choose_experiment_neural_move(state, exp6_color_name, search_profile="balanced")
            exp6_decision_seconds.append(time.perf_counter() - t_move)
            if not payload:
                invalid_actor = "exp6"; break
            promo = payload.get("promotion") or ""
            try:
                move = chess.Move.from_uci(f"{payload['from']}{payload['to']}{promo}")
            except Exception:
                invalid_actor = "exp6"; break
            if move not in board.legal_moves:
                invalid_actor = "exp6"; break
        else:
            t_move = time.perf_counter()
            try:
                pv = engine.analyse(board, limit=analysis_limit(depth=stockfish_depth, movetime_ms=0), multipv=1)
            except Exception:
                sf_decision_seconds.append(time.perf_counter() - t_move)
                invalid_actor = "stockfish"; break
            sf_decision_seconds.append(time.perf_counter() - t_move)
            if not pv:
                invalid_actor = "stockfish"; break
            try:
                move = chess.Move.from_uci(pv[0]["move"])
            except Exception:
                invalid_actor = "stockfish"; break
            if move not in board.legal_moves:
                invalid_actor = "stockfish"; break
        board.push(move)
    elapsed_wall = time.perf_counter() - wall0
    rusage1 = resource.getrusage(resource.RUSAGE_SELF)
    outcome = board.outcome(claim_draw=True)
    if invalid_actor:
        winner = "stockfish" if invalid_actor == "exp6" else "exp6"
        result = f"{winner}_win"
        reason = "invalid_move"
    elif outcome is None:
        result = "incomplete"
        reason = "max_plies"
        winner = None
    elif outcome.winner is None:
        result = "draw"
        reason = outcome.termination.name.lower()
        winner = None
    else:
        winner = "exp6" if outcome.winner == exp6_color else "stockfish"
        result = f"{winner}_win"
        reason = outcome.termination.name.lower()

    def _stat(seconds: list[float]) -> dict:
        if not seconds:
            return {"count": 0, "sum_s": 0.0, "mean_s": 0.0, "max_s": 0.0, "median_s": 0.0}
        return {
            "count": len(seconds),
            "sum_s": round(sum(seconds), 4),
            "mean_s": round(statistics.fmean(seconds), 4),
            "max_s": round(max(seconds), 4),
            "median_s": round(statistics.median(seconds), 4),
        }

    return {
        "opening_id": opening_id,
        "stockfish_depth": stockfish_depth,
        "exp6_color": exp6_color_name,
        "result": result,
        "winner": winner,
        "reason": reason,
        "plies": len(board.move_stack),
        "elapsed_wall_s": round(elapsed_wall, 3),
        "score_points": SCORE_WIN if result == "exp6_win" else (SCORE_DRAW if result == "draw" else SCORE_LOSS),
        "exp6_decision_times": _stat(exp6_decision_seconds),
        "stockfish_decision_times": _stat(sf_decision_seconds),
        "process_resource_delta": {
            # rusage utime/stime are process-cumulative seconds, so the
            # delta isolates this game's CPU spend on top of prior work.
            "user_cpu_s": round(rusage1.ru_utime - rusage0.ru_utime, 3),
            "sys_cpu_s": round(rusage1.ru_stime - rusage0.ru_stime, 3),
            # ru_maxrss is in KB on Linux; report as MB for readability.
            "max_rss_mb_after": round(rusage1.ru_maxrss / 1024.0, 1),
        },
        "final_fen_digest": __import__("hashlib").sha256(board.fen().encode()).hexdigest()[:12],
    }


def play_staged_test(weights_path: Path, *, games_per_depth: int = STAGED_GAMES_PER_DEPTH) -> list[dict]:
    """Play (depths × games_per_depth) games. For each depth play
    ``games_per_depth`` games with alternating Exp6 colour."""
    rows: list[dict] = []
    schedule = []
    for d in STAGED_DEPTHS:
        for k in range(games_per_depth):
            opening_id, opening_moves = STAGED_OPENINGS[(d + k - 1) % len(STAGED_OPENINGS)]
            exp6_color = "white" if k % 2 == 0 else "black"
            schedule.append((opening_id, opening_moves, exp6_color, d))
    with UciStockfish(STOCKFISH_BIN) as engine:
        for i, (opening_id, opening_moves, exp6_color, depth) in enumerate(schedule):
            row = play_one_game(opening_id, opening_moves, exp6_color, weights_path, depth, engine)
            rows.append(row)
            ex = row["exp6_decision_times"]; sf = row["stockfish_decision_times"]
            print(
                f"    g{i+1:02d} d{depth} {opening_id}/exp6={exp6_color}: "
                f"{row['result']:>14s} ({row['reason']}) "
                f"{row['plies']:3d}p  wall={row['elapsed_wall_s']:.1f}s  "
                f"exp6_mean={ex['mean_s']*1000:.0f}ms max={ex['max_s']*1000:.0f}ms  "
                f"sf_mean={sf['mean_s']*1000:.0f}ms max={sf['max_s']*1000:.0f}ms  "
                f"score={row['score_points']:+d}",
                flush=True,
            )
    return rows


def score_summary(rows: list[dict]) -> dict:
    wins = sum(1 for r in rows if r["result"] == "exp6_win")
    draws = sum(1 for r in rows if r["result"] == "draw")
    losses = sum(1 for r in rows if r["result"] == "stockfish_win")
    incomplete = sum(1 for r in rows if r["result"] == "incomplete")
    total_score = sum(r["score_points"] for r in rows)
    # Per-depth breakdown
    by_depth: dict[int, dict] = {}
    for r in rows:
        d = r["stockfish_depth"]
        bd = by_depth.setdefault(d, {"games": 0, "W": 0, "D": 0, "L": 0, "score": 0})
        bd["games"] += 1
        if r["result"] == "exp6_win": bd["W"] += 1
        elif r["result"] == "draw":   bd["D"] += 1
        elif r["result"] == "stockfish_win": bd["L"] += 1
        bd["score"] += r["score_points"]
    return {
        "games": len(rows),
        "W": wins, "D": draws, "L": losses, "incomplete": incomplete,
        "total_score": total_score,
        "max_possible_score": SCORE_WIN * len(rows),
        "min_possible_score": SCORE_LOSS * len(rows),
        "by_depth": by_depth,
    }


# Backwards-compat name used elsewhere in this script.
def play_staged_5(weights_path: Path) -> list[dict]:
    return play_staged_test(weights_path)

def score_rate(rows: list[dict]) -> float:
    s = score_summary(rows)
    span = s["max_possible_score"] - s["min_possible_score"]
    return (s["total_score"] - s["min_possible_score"]) / span if span else 0.0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="Run only the random-init Stockfish baseline (stage 0) and exit. "
             "Useful to verify the test harness before launching the full "
             "10-stage curriculum.",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip the random-init baseline and start from stage 1.",
    )
    args = parser.parse_args()

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    if not args.baseline_only:
        games = load_games(TOTAL_GAMES)
        print(f"loaded {len(games)} games from {SOURCE_GAMES_JSONL}", flush=True)
    else:
        games = []

    # ── BASELINE + LABELLING IN PARALLEL ────────────────────────────
    # Random-init stage 00 staged-10 runs in the main thread while the
    # Stockfish labelling (depth 10, ~30 min) runs in a background
    # thread. Both spawn separate Stockfish UCI subprocesses so they
    # compete only at the OS scheduler level — modern multi-core
    # systems handle this fine. Net wall-clock saving: ~25 min.
    import threading
    weights = make_initial_weights(seed=20260516)
    init_snap = SNAPSHOTS_DIR / "chess_experiment_6_neural_stage00_init.npz"
    save_weights(init_snap, weights)

    label_thread = None
    label_box: dict = {"rows": None, "error": None}
    if not args.baseline_only and not LABELS_PATH.exists():
        positions = collect_positions_latter_half(games)
        pct = int(LATTER_HALF_FRACTION * 100)
        print(f"extracted {len(positions)} positions (latter {pct}% of each game); starting parallel Stockfish "
              f"labelling (depth {STOCKFISH_DEPTH_FOR_LABEL}) in background...", flush=True)
        def _label_worker():
            try:
                rows = label_positions(positions)
                with LABELS_PATH.open("w") as f:
                    for game_idx, fen, cp, outcome, blended in rows:
                        f.write(json.dumps({
                            "game_idx": game_idx,
                            "fen": fen,
                            "cp_white": cp,
                            "outcome_white": outcome,
                            "blended_cp": blended,
                            "label_depth": STOCKFISH_DEPTH_FOR_LABEL,
                            "outcome_blend": OUTCOME_BLEND,
                        }) + "\n")
                label_box["rows"] = rows
            except Exception as exc:
                label_box["error"] = exc
        label_thread = threading.Thread(target=_label_worker, daemon=False)
        label_thread.start()

    stage_records: list[dict] = []
    if not args.skip_baseline:
        print(f"\nstage 00 (random init) saved -> {init_snap}\n", flush=True)
        print(f"baseline ({STAGED_GAMES_PER_DEPTH * len(STAGED_DEPTHS)}-game test vs Stockfish):", flush=True)
        init_results = play_staged_test(init_snap)
        init_summary = score_summary(init_results)
        init_sr = score_rate(init_results)
        print(
            f"  baseline: {init_summary['W']}W/{init_summary['D']}D/{init_summary['L']}L "
            f"score={init_summary['total_score']:+d}/{init_summary['max_possible_score']} (normalized {init_sr:.2%})",
            flush=True,
        )
        stage_records.append({
            "stage": 0, "label": "random_init",
            "n_games_cumulative": 0, "n_positions_cumulative": 0,
            "final_loss": None, "train_seconds": 0.0,
            "snapshot_path": str(init_snap),
            "staged_5": init_results,
            "wins": init_summary["W"], "draws": init_summary["D"], "losses_g": init_summary["L"],
            "score_total": init_summary["total_score"],
            "score_max": init_summary["max_possible_score"],
            "score_min": init_summary["min_possible_score"],
            "score_by_depth": init_summary["by_depth"],
            "score_rate": init_sr,
        })
        _write_report_json(stage_records, complete=False, baseline_only=args.baseline_only)
        _write_markdown_report(stage_records, best=None, complete=False)
        if args.baseline_only:
            print(f"\nbaseline complete. Report: {REPORT_MD}", flush=True)
            return 0

    # ── LABELLING: collect (joined from background thread) ─────────
    labelled: list[tuple[int, str, float, float, float]] = []
    game_indices_sorted: list[int] = []
    if not args.baseline_only:
        if LABELS_PATH.exists() and label_thread is None:
            # cache hit at startup — no background thread was spawned.
            # Always recompute blended_cp from cp_white + outcome_white
            # using the CURRENT OUTCOME_BLEND so blend tweaks (e.g.
            # v2→v3: 0.5→0.7) don't require re-running Stockfish.
            # v4 fix #1: also clip cp_white to ±CP_CLIP_ABS at load
            # time so cached labels gain the outlier suppression
            # without needing a re-label pass.
            with LABELS_PATH.open() as f:
                for line in f:
                    rec = json.loads(line)
                    cp_raw = float(rec["cp_white"])
                    cp = max(-CP_CLIP_ABS, min(CP_CLIP_ABS, cp_raw))
                    outcome = float(rec.get("outcome_white", 0.0))
                    blended = (1.0 - OUTCOME_BLEND) * cp + OUTCOME_BLEND * outcome * OUTCOME_CP_AMPLITUDE
                    labelled.append((int(rec["game_idx"]), rec["fen"], cp, outcome, blended))
            print(f"\nloaded {len(labelled)} cached labels from {LABELS_PATH} (cp clipped ±{CP_CLIP_ABS:.0f}, blend recomputed at OUTCOME_BLEND={OUTCOME_BLEND})", flush=True)
        else:
            if label_thread is not None and label_thread.is_alive():
                print(f"\nwaiting for background Stockfish labelling thread...", flush=True)
                label_thread.join()
            if label_box.get("error"):
                raise label_box["error"]
            labelled = label_box.get("rows") or []
            if not labelled and LABELS_PATH.exists():
                with LABELS_PATH.open() as f:
                    for line in f:
                        rec = json.loads(line)
                        cp_raw = float(rec["cp_white"])
                        cp = max(-CP_CLIP_ABS, min(CP_CLIP_ABS, cp_raw))
                        outcome = float(rec.get("outcome_white", 0.0))
                        blended = (1.0 - OUTCOME_BLEND) * cp + OUTCOME_BLEND * outcome * OUTCOME_CP_AMPLITUDE
                        labelled.append((int(rec["game_idx"]), rec["fen"], cp, outcome, blended))
            print(f"labelling complete: {len(labelled)} positions -> {LABELS_PATH}", flush=True)

        by_game: dict[int, list[tuple[int, str, float]]] = {}
        for row in labelled:
            by_game.setdefault(row[0], []).append(row)
        game_indices_sorted = sorted(by_game.keys())
        if len(game_indices_sorted) < TOTAL_GAMES:
            print(f"WARNING: only {len(game_indices_sorted)} games have labels (wanted {TOTAL_GAMES}); proceeding.")
        print(f"label inventory: {len(game_indices_sorted)} games, {len(labelled)} total positions", flush=True)

    for stage in range(1, N_STAGES + 1):
        n_games_target = stage * GAMES_PER_STAGE
        if n_games_target > len(game_indices_sorted):
            n_games_target = len(game_indices_sorted)
        stage_game_ids = set(game_indices_sorted[:n_games_target])
        subset = [row for row in labelled if row[0] in stage_game_ids]
        print(f"\n=== Stage {stage}/{N_STAGES}: cumulative {n_games_target} games, {len(subset)} positions ===", flush=True)
        if COLD_START_PER_STAGE:
            # v4 fix #4: discard previous stage's weights and start
            # fresh from a deterministic random init for THIS stage.
            # Deterministic seed = base_seed + stage so stages are
            # reproducible but independent.
            weights = make_initial_weights(seed=20260516 + stage)
            print(f"  cold-start: re-init weights at stage {stage} (seed={20260516 + stage})", flush=True)
        t0 = time.perf_counter()
        losses = train_cumulative(subset, weights, epochs=EPOCHS_PER_STAGE)
        train_dt = time.perf_counter() - t0
        final_loss = losses[-1] if losses else None
        print(f"  train {train_dt:.1f}s, final loss {final_loss}", flush=True)
        snap_path = SNAPSHOTS_DIR / f"chess_experiment_6_neural_stage{stage:02d}.npz"
        save_weights(snap_path, weights)
        print(f"  snapshot -> {snap_path}", flush=True)

        print(f"  staged-5 vs Stockfish (depths {STAGED_DEPTHS}):", flush=True)
        t0 = time.perf_counter()
        results = play_staged_5(snap_path)
        test_dt = time.perf_counter() - t0
        sr = score_rate(results)
        wins = sum(1 for r in results if r["result"] == "exp6_win")
        draws = sum(1 for r in results if r["result"] == "draw")
        losses_g = sum(1 for r in results if r["result"] == "stockfish_win")
        score_total = wins * SCORE_WIN + draws * SCORE_DRAW + losses_g * SCORE_LOSS
        score_max = SCORE_WIN * len(results)
        print(f"  staged-5 {test_dt:.1f}s: {wins}W/{draws}D/{losses_g}L score_total={score_total:+d}/{score_max} (norm={sr:.2%})", flush=True)
        stage_records.append({
            "stage": stage,
            "label": f"cumulative_{n_games_target}_games_{len(subset)}_positions",
            "n_games_cumulative": n_games_target,
            "n_positions_cumulative": len(subset),
            "final_loss": final_loss,
            "loss_history": losses,
            "train_seconds": round(train_dt, 2),
            "snapshot_path": str(snap_path),
            "staged_5": results,
            "wins": wins, "draws": draws, "losses_g": losses_g,
            "score_total": score_total,
            "score_max": score_max,
            "score_rate": sr,
            "test_seconds": round(test_dt, 2),
        })

        # incremental report after each stage so we can read progress mid-run
        _write_report_json(stage_records, complete=(stage == N_STAGES), baseline_only=False)

    best = None
    if any(r["stage"] > 0 for r in stage_records):
        best = max(
            (r for r in stage_records if r["stage"] > 0),
            key=lambda r: (r["score_total"], r["wins"], r["draws"], -r["losses_g"], -r["stage"]),
        )
        print(
            f"\nBEST STAGE: {best['stage']} ({best['wins']}W/{best['draws']}D/{best['losses_g']}L "
            f"score={best['score_total']:+d}/{best['score_max']})",
            flush=True,
        )
        production_model = runtime_model_path("chess_experiment_6_neural.npz")
        production_model.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(best["snapshot_path"], production_model)
        print(f"promoted to runtime: {production_model}", flush=True)
    _write_markdown_report(stage_records, best=best, complete=True)
    print(f"\nreport -> {REPORT_MD}", flush=True)
    return 0


def _write_report_json(stage_records: list[dict], *, complete: bool, baseline_only: bool) -> None:
    """Dual-write the curriculum report JSON: persistent copy under
    the standalone artifact root plus the configured external runtime."""
    payload = {"stages": stage_records, "complete": complete, "baseline_only": baseline_only}
    body = json.dumps(payload, indent=2, default=str)
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(body)
    REPORT_JSON_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_RUNTIME.write_text(body)


def _sha256_file(path: Path, *, head_bytes: int = 0) -> str:
    """Return the hex SHA-256 of a file (full file unless ``head_bytes`` set)."""
    import hashlib
    h = hashlib.sha256()
    if not path.exists():
        return ""
    with path.open("rb") as f:
        if head_bytes:
            h.update(f.read(head_bytes))
        else:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()


def _load_quality_summary() -> dict:
    summary_path = OUT_DIR / "quality_1000_summary.json"
    if not summary_path.exists():
        return {}
    try:
        return json.loads(summary_path.read_text())
    except Exception:
        return {}


def _write_markdown_report(stage_records: list[dict], *, best, complete: bool) -> None:
    md: list[str] = []
    md.append("# Exp6 Curriculum Learning Report\n\n")
    md.append(f"Date: 2026-05-16  \n")
    md.append(f"Status: {'COMPLETE' if complete else 'IN PROGRESS / BASELINE ONLY'}\n\n")

    md.append("## 1. 實驗目的\n\n")
    md.append(
        "驗證 Exp6 真實神經網路評估器是否隨著 1000 局優質棋局累積、分 10 梯次訓練，"
        "在固定的 Stockfish 對局 benchmark 上 (深度 1-5，每深度 2 局，共 10 局) 表現上升 — "
        "即「是否在學習」。同時記錄各階段所需資源（CPU time, RSS）與決策耗時，"
        "建立未來架構決策的成本/效益基準。  \n\n"
        "計分公式 (per game)：WIN=+4，DRAW=-1，LOSS=-4。"
        "10 局 staged 測試總分範圍 [-40, +40]，DRAW 給負分以避免引擎走 fivefold cycle 假裝平手。\n\n"
    )

    qs = _load_quality_summary()
    md.append("## 2. 資料來源\n\n")
    md.append(f"目標：1000 局優質棋局，多源以提升分布多樣性。\n\n")
    if qs:
        md.append("### 來源分布（通過篩選後）\n\n")
        md.append("| 來源檔 | 通過篩選局數 |\n|---|---:|\n")
        for src, cnt in (qs.get("per_source") or {}).items():
            md.append(f"| {src} | {cnt} |\n")
        md.append(f"\n- 最終保留: **{qs.get('kept', '?')}** 局  \n")
        md.append(f"- 平均 Elo 範圍: {qs.get('min_avg_elo', '?')} – {qs.get('max_avg_elo', '?')}  \n\n")
        md.append("### 原始下載\n\n")
        md.append("- TWIC 公開週刊 zip（theweekinchess.com）  \n")
        md.append("- Lichess 公開 API：DrNykterstein, penguingm1, nihalsarin2004, Konevlad, Crest64, DanielNaroditsky, alireza2003, manwithavan  \n")
        md.append("- 既有 trusted JSONL：imported_replay_top_supplement / imported_replay / imported_replay_multi  \n\n")
    else:
        md.append("（資料來源摘要 ``quality_1000_summary.json`` 尚未產生 — 是 baseline-only 模式）\n\n")

    md.append("## 3. 品質篩選\n\n")
    md.append("篩選條件（全部必須通過）：\n\n")
    md.append("- ``collection_tier == 'trusted'``  \n")
    md.append("- ``suspicious_flag == False``  \n")
    md.append("- ``duplicate_flag == False``  \n")
    md.append("- ``pgn_labels.avg_elo >= 2600``（雙方平均 Elo 至少 2600）  \n")
    md.append("- ``'elite' in pgn_labels.categories``（通過 chess_pgn_to_replay.py 的 elite gate）  \n")
    md.append("- ``len(move_history) >= 30``（避免極短局 — 後 50% positions 才有意義）  \n\n")
    if qs:
        md.append(f"- 保留 / 目標: {qs.get('kept','?')}/{qs.get('target','?')} 局，全部 100% 通過上述條件。\n\n")

    md.append("## 4. 訓練設定\n\n")
    md.append(f"- 梯次：{N_STAGES} stages  \n")
    md.append(f"- 每梯次新增：{GAMES_PER_STAGE} games  \n")
    cs_desc = "cold-start (每階段從 random init 重訓累積資料)" if COLD_START_PER_STAGE else "warm-start (累積訓練，沿用前梯次權重)"
    md.append(f"- 累積式訓練：stage N 用 1..N×{GAMES_PER_STAGE} 局，{cs_desc}  \n")
    md.append(f"- 位置抽取：每局後 {int(LATTER_HALF_FRACTION*100)}% 棋步 (決勝段)，提高每局訓練位置的決定性意義  \n")
    md.append(f"- Stockfish 標註：固定 depth {STOCKFISH_DEPTH_FOR_LABEL}，multipv 1，每位置一次  \n")
    md.append(f"- 教師 cp clip 範圍: ±{CP_CLIP_ABS:.0f} (v4 fix #1，抑制 mate-score outlier)  \n")
    md.append(f"- 輸出 blend: target = (1-{OUTCOME_BLEND})·cp + {OUTCOME_BLEND}·outcome·{OUTCOME_CP_AMPLITUDE:.0f}  \n")
    md.append(f"- Epochs/stage: {EPOCHS_PER_STAGE}  \n")
    md.append(f"- Batch size: {BATCH_SIZE}  \n")
    md.append(f"- Learning rate: {LR}  \n")
    md.append(f"- Momentum: {MOMENTUM}  \n")
    md.append(f"- Gradient clip (per-tensor L2): {GRAD_CLIP}  \n")
    md.append(f"- Seed (init weights): 20260516 (cold-start: +stage 1..10)  \n")
    md.append(f"- Optimizer: SGD with momentum, per-tensor grad clip  \n")
    md.append(f"- 架構: {INPUT_DIM} → {HIDDEN_1_DIM} → {HIDDEN_2_DIM} → 1 (clipped ReLU, clip [0,127])  \n")
    md.append(f"  輸入 = 768 piece-square + 6 state bits (stm, castling×4, ep flag) (v4 fix #2)  \n\n")

    md.append("## 5. 每階段模型備份\n\n")
    md.append("| Stage | Snapshot path | Size (B) | SHA-256 (head 16 hex) |\n|---|---|---:|---|\n")
    for rec in stage_records:
        snap = Path(rec.get("snapshot_path") or "")
        size = snap.stat().st_size if snap.exists() else 0
        digest = _sha256_file(snap)[:16] if snap.exists() else "—"
        md.append(f"| {rec['stage']:02d} | `{snap}` | {size:,} | `{digest}` |\n")
    md.append("\n")

    md.append("## 6. 每階段 staged-10 測試結果\n\n")
    games_total = STAGED_GAMES_PER_DEPTH * len(STAGED_DEPTHS)
    md.append(f"每階段對 Stockfish 深度 {STAGED_DEPTHS} 各打 {STAGED_GAMES_PER_DEPTH} 局 = {games_total} 局。"
              f"Score 公式 WIN={SCORE_WIN}/DRAW={SCORE_DRAW}/LOSS={SCORE_LOSS}，總分範圍 [{games_total*SCORE_LOSS}, {games_total*SCORE_WIN}]。\n\n")
    md.append("| Stage | Cum games | Train s | Final loss | W | D | L | Score | Best so far |\n")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|:---:|\n")
    best_score_so_far = -10**9
    for rec in stage_records:
        marker = ""
        if rec.get("score_total", 0) > best_score_so_far:
            best_score_so_far = rec.get("score_total", 0)
            marker = "✓"
        loss = f"{rec['final_loss']:.2f}" if rec.get("final_loss") is not None else "—"
        train_s = f"{rec.get('train_seconds', 0):.0f}" if rec.get('train_seconds') else "—"
        md.append(
            f"| {rec['stage']:02d} | {rec.get('n_games_cumulative',0)} | {train_s} | {loss} | "
            f"{rec.get('wins',0)} | {rec.get('draws',0)} | {rec.get('losses_g',0)} | "
            f"{rec.get('score_total',0):+d}/{rec.get('score_max',0)} | {marker} |\n"
        )
    md.append("\n### Per-depth W/D/L per stage\n\n")
    md.append("| Stage |")
    for d in STAGED_DEPTHS:
        md.append(f" d{d} W/D/L (score) |")
    md.append("\n|---|")
    for _ in STAGED_DEPTHS:
        md.append("---|")
    md.append("\n")
    for rec in stage_records:
        md.append(f"| {rec['stage']:02d} |")
        by_depth = rec.get("score_by_depth", {})
        for d in STAGED_DEPTHS:
            cell = by_depth.get(d) or by_depth.get(str(d))
            if cell:
                md.append(f" {cell['W']}/{cell['D']}/{cell['L']} ({cell['score']:+d}) |")
            else:
                md.append(" — |")
        md.append("\n")

    md.append("\n## 7. 是否有在學習\n\n")
    if len(stage_records) >= 2:
        baseline = stage_records[0]
        latest = stage_records[-1]
        delta_score = latest.get("score_total", 0) - baseline.get("score_total", 0)
        # find trend
        score_seq = [r.get("score_total", 0) for r in stage_records]
        score_trend_up = all(score_seq[i+1] >= score_seq[i] for i in range(len(score_seq)-1))
        score_trend_avg_up = (sum(score_seq[len(score_seq)//2:]) / max(1, len(score_seq)//2)) > \
                              (sum(score_seq[:len(score_seq)//2]) / max(1, len(score_seq)//2))
        loss_seq = [r.get("final_loss") for r in stage_records if r.get("final_loss") is not None]
        loss_falling = bool(loss_seq) and loss_seq[-1] < loss_seq[0]
        md.append(f"- baseline (stage 0) 分數：{baseline.get('score_total',0):+d}/{baseline.get('score_max',0)}  \n")
        md.append(f"- 最後 stage 分數：{latest.get('score_total',0):+d}/{latest.get('score_max',0)}  \n")
        md.append(f"- Δ 總分：{delta_score:+d}  \n")
        md.append(f"- 訓練 loss：{'下降' if loss_falling else '未持續下降 / 缺資料'}  \n")
        md.append(f"- Score 單調上升：{'是' if score_trend_up else '否'}  \n")
        md.append(f"- Score 後半 vs 前半平均：{'上升' if score_trend_avg_up else '未上升'}  \n\n")
        if delta_score > 0 and (score_trend_up or score_trend_avg_up):
            md.append("→ **可判定模型有學到東西**：對 Stockfish 的對局表現隨累積訓練而提升。\n\n")
        elif loss_falling and delta_score <= 0:
            md.append("→ **loss 在下降但 staged-10 沒提升**：可能模型過度擬合 cp 預測而沒有轉化為對局贏面 — "
                      "需要更多資料、或評估 + search 不夠深以利用評估改善、或 cp regression 與實戰 W/D/L 解耦。\n\n")
        else:
            md.append("→ **未觀察到清楚的學習信號**：可能 lr 過大 / 過小、資料量不足、或 Stockfish 對深度 1-5 太強。\n\n")

    md.append("## 8. 最優模型\n\n")
    if best:
        md.append(f"**Stage {best['stage']:02d}** 是觀察區間內最優：\n\n")
        md.append(f"- W/D/L: {best['wins']}/{best['draws']}/{best['losses_g']}  \n")
        md.append(f"- 總分: {best['score_total']:+d}/{best['score_max']}  \n")
        md.append(f"- 累積訓練局數: {best.get('n_games_cumulative', 0)}  \n")
        md.append(f"- 累積訓練位置數: {best.get('n_positions_cumulative', 0)}  \n")
        md.append(f"- Snapshot: `{best['snapshot_path']}`  \n")
        md.append(f"- 已 promote 到 `runtime/games/models/chess_experiment_6_neural.npz`  \n\n")
        md.append("理由：score formula 設計為 WIN+4/DRAW-1/LOSS-4，總分最高代表「實際贏球或避免輸球」相對最佳，"
                  "勝於其他 stage。若 baseline (stage 0) 已是最佳，代表訓練未帶來實戰改善，不建議 promote 訓練後模型；"
                  "本報告中的 best 是純粹依分數選出，promote 動作是自動執行的 — 是否實際採用要看「是否在學習」章節的綜合判斷。\n\n")
    else:
        md.append("（baseline-only 模式：尚未產生最優模型）\n\n")

    md.append("## 附錄: 每階段每局完整資料\n\n")
    for rec in stage_records:
        md.append(f"\n### Stage {rec['stage']:02d} — {rec.get('label','')}\n\n")
        md.append("| # | Depth | Open. | Color | Result | Reason | Plies | Wall s | Exp6 mean ms (max) | SF mean ms (max) | UserCPU s | MaxRSS MB | Score |\n")
        md.append("|---:|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|\n")
        for i, g in enumerate(rec.get("staged_5", []), 1):
            ex = g.get("exp6_decision_times", {})
            sf = g.get("stockfish_decision_times", {})
            res = g.get("process_resource_delta", {})
            md.append(
                f"| {i} | {g['stockfish_depth']} | {g['opening_id']} | {g['exp6_color']} | {g['result']} | {g['reason']} | "
                f"{g['plies']} | {g.get('elapsed_wall_s', 0):.1f} | "
                f"{ex.get('mean_s',0)*1000:.0f} ({ex.get('max_s',0)*1000:.0f}) | "
                f"{sf.get('mean_s',0)*1000:.0f} ({sf.get('max_s',0)*1000:.0f}) | "
                f"{res.get('user_cpu_s', 0):.2f} | {res.get('max_rss_mb_after', 0):.0f} | "
                f"{g.get('score_points', 0):+d} |\n"
            )
    body = "".join(md)
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(body)
    # Mirror into the configured external runtime for operator visibility.
    REPORT_MD_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD_RUNTIME.write_text(body)


if __name__ == "__main__":
    raise SystemExit(main())
