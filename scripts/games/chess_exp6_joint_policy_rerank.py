#!/usr/bin/env python3
"""Exp6 conservative joint policy/value rerank experiment.

This script is deliberately an experiment harness, not a runtime promotion.
It does not modify the locked champion weights. It trains a small
board-by-move interaction model from generic curriculum positions, then
allows that model to rerank only the top candidates already produced by the
existing Exp6 search. If a risk guard trips, the original champion decision is
kept.

Important safety constraints:
- no staged FEN/move traces are persisted by default;
- no exact-position lookup table is used at runtime;
- the policy head is not connected as a root move-order bonus;
- staged evaluation aborts early when the candidate clearly breaks defence.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import chess
import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except Exception as exc:  # pragma: no cover - environment guard
    raise SystemExit(f"PyTorch is required for this experiment: {exc}")


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.chess_nn import _candidate_features  # noqa: E402
from services.games.chess_neural import (  # noqa: E402
    INPUT_DIM,
    NeuralEvaluator,
    active_features,
    load_weights,
)
from services.games.chess_exp6 import (  # noqa: E402
    _SEARCH_PROFILES,
    _move_order_score,
    _opening_principle_filter,
    _principled_move_order_score,
)
from services.games.chess_search import ZobristHasher, opening_sanity_filter, search_best_move  # noqa: E402
from services.games.chess_stockfish_teacher import UciStockfish, analysis_limit, resolve_stockfish_path  # noqa: E402
from scripts.games.common_paths import exp6_artifacts_root, exp6_private_dir, runtime_model_path  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402


DEFAULT_WEIGHTS = runtime_model_path("chess_experiment_6_neural.npz")
DEFAULT_LABELS = exp6_private_dir() / "curriculum_labels_10k.jsonl"
DEFAULT_OUT = exp6_artifacts_root() / "joint_policy_rerank_probe.json"
SEED = 20260520
MOVE_DIM = len(
    _candidate_features(
        chess.Board(),
        chess.Move.from_uci("e2e4"),
        chess.Board("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"),
        "white",
    )
)
VALUE_SCALE_CP = 600.0
MAX_POLICY_Z = 3.0
PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


@dataclass
class LabelledPosition:
    fen: str
    cp_white: float
    multipv: list[dict]
    champion_move: str
    champion_eval_side: int
    candidate_moves: tuple[str, ...] = ()
    candidate_teacher_eval_side: dict[str, float] | None = None
    target_move: str = ""
    baseline_teacher_eval_side: float = 0.0
    target_teacher_eval_side: float = 0.0


class JointPolicyValue(nn.Module):
    """Small CPU-friendly board/value + board-by-move policy model."""

    def __init__(self, *, hidden: int = 128) -> None:
        super().__init__()
        self.board = nn.Sequential(
            nn.Linear(INPUT_DIM, hidden),
            nn.Tanh(),
            nn.Linear(hidden, hidden),
            nn.Tanh(),
        )
        self.move = nn.Sequential(
            nn.Linear(MOVE_DIM, hidden),
            nn.Tanh(),
            nn.Linear(hidden, hidden),
            nn.Tanh(),
        )
        self.value = nn.Linear(hidden, 1)
        self.move_linear = nn.Linear(MOVE_DIM, 1)
        self.scale = math.sqrt(float(hidden))

    def board_hidden(self, board_x: torch.Tensor) -> torch.Tensor:
        return self.board(board_x)

    def value_score(self, board_x: torch.Tensor) -> torch.Tensor:
        return torch.tanh(self.value(self.board_hidden(board_x)).squeeze(-1))

    def policy_score(self, board_x: torch.Tensor, move_x: torch.Tensor) -> torch.Tensor:
        board_h = self.board_hidden(board_x)
        move_h = self.move(move_x)
        return (board_h * move_h).sum(dim=-1) / self.scale + self.move_linear(move_x).squeeze(-1)


def _board_features(board: chess.Board) -> np.ndarray:
    x = np.zeros(INPUT_DIM, dtype=np.float32)
    x[active_features(board)] = 1.0
    return x


def _move_features(board: chess.Board, move: chess.Move) -> np.ndarray:
    side = "white" if board.turn == chess.WHITE else "black"
    after = board.copy(stack=False)
    after.push(move)
    return np.asarray(_candidate_features(board, move, after, side), dtype=np.float32)


def _cp_side_from_white(cp_white: float, board: chess.Board) -> float:
    return float(cp_white) if board.turn == chess.WHITE else -float(cp_white)


def _material_margin_for_color(board: chess.Board, color: chess.Color) -> int:
    margin = 0
    for piece in board.piece_map().values():
        value = PIECE_VALUES.get(piece.piece_type, 0)
        margin += value if piece.color == color else -value
    return int(margin)


def _after(board: chess.Board, move: chess.Move) -> chess.Board:
    after = board.copy(stack=True)
    after.push(move)
    return after


def _opponent_has_mate_in_one(board: chess.Board) -> bool:
    for reply in board.legal_moves:
        board.push(reply)
        try:
            if board.is_checkmate():
                return True
        finally:
            board.pop()
    return False


def _collect_candidate_fens(labels_path: Path, *, max_fullmove: int, pool_size: int) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    with labels_path.open() as f:
        for line in f:
            rec = json.loads(line)
            try:
                board = chess.Board(str(rec["fen"]))
            except Exception:
                continue
            if board.is_game_over() or board.legal_moves.count() < 2 or board.fullmove_number > max_fullmove:
                continue
            try:
                cp_white = float(rec["cp_white"])
            except Exception:
                continue
            rows.append((board.fen(), cp_white))
            if len(rows) >= pool_size:
                break
    rng = random.Random(SEED)
    rng.shuffle(rows)
    return rows


def _champion_baseline_move(
    board: chess.Board,
    *,
    evaluator: NeuralEvaluator,
    profile: dict,
) -> tuple[chess.Move | None, int]:
    result = search_best_move(
        board,
        max_depth=int(profile["depth"]),
        evaluate=evaluator,
        move_order_fn=_principled_move_order_score,
        quiescence_depth=int(profile["quiescence_depth"]),
        hasher=ZobristHasher(seed=20260601),
        time_budget_ms=profile.get("time_budget_ms"),
        enable_pvs=bool(profile.get("enable_pvs")),
        enable_lmr=bool(profile.get("enable_lmr")),
        enable_null_move=bool(profile.get("enable_null_move")),
        enable_futility=bool(profile.get("enable_futility")),
    )
    best = opening_sanity_filter(
        board,
        result.best_move,
        score_move=lambda mv: _principled_move_order_score(board, mv, 0),
    )
    best = _opening_principle_filter(board, best)
    return best, int(result.score)


def build_training_labels(
    *,
    labels_path: Path,
    weights_path: Path,
    label_limit: int,
    dev_size: int,
    max_fullmove: int,
    stockfish_depth: int,
    multipv: int,
    label_mode: str,
    candidate_top_n: int,
    min_switch_improve_cp: int,
) -> tuple[list[LabelledPosition], list[LabelledPosition]]:
    sf_path = resolve_stockfish_path()
    if not sf_path:
        raise SystemExit("Stockfish not found")
    rng = random.Random(SEED)
    pool = _collect_candidate_fens(labels_path, max_fullmove=max_fullmove, pool_size=max(label_limit * 12, 2000))
    weights = load_weights(weights_path)
    evaluator = NeuralEvaluator(weights)
    profile = dict(_SEARCH_PROFILES["balanced"])
    labelled: list[LabelledPosition] = []
    t0 = time.perf_counter()
    with UciStockfish(sf_path) as engine:
        for fen, cp_white in pool:
            if len(labelled) >= label_limit + dev_size:
                break
            board = chess.Board(fen)
            champ, champ_score = _champion_baseline_move(board, evaluator=evaluator, profile=profile)
            if label_mode == "candidate_topn":
                if champ is None:
                    continue
                root_scores = _root_search_scores(
                    board,
                    evaluator=evaluator,
                    profile=profile,
                    top_n=max(1, int(candidate_top_n)),
                )
                candidates: list[chess.Move] = []
                for move, _score in root_scores:
                    if move not in candidates:
                        candidates.append(move)
                if champ not in candidates:
                    candidates.append(champ)
                candidates = candidates[: max(1, int(candidate_top_n))]
                if champ not in candidates:
                    candidates.append(champ)
                if len(candidates) < 2:
                    continue
                try:
                    rows = engine.analyse(
                        board,
                        limit=analysis_limit(depth=stockfish_depth, movetime_ms=0),
                        multipv=len(candidates),
                        root_moves=candidates,
                    )
                except Exception:
                    continue
                rows = [row for row in rows if str(row.get("move") or "")]
                eval_by_move = {
                    str(row.get("move")): float(row.get("teacher_eval_cp") or 0.0)
                    for row in rows
                    if str(row.get("move") or "")
                }
                champ_uci = champ.uci()
                if champ_uci not in eval_by_move:
                    continue
                best_uci, best_eval = max(
                    eval_by_move.items(),
                    key=lambda item: (float(item[1]), item[0]),
                )
                baseline_eval = float(eval_by_move[champ_uci])
                target_uci = champ_uci
                target_eval = baseline_eval
                if best_uci != champ_uci and best_eval >= baseline_eval + int(min_switch_improve_cp):
                    target_uci = best_uci
                    target_eval = float(best_eval)
                labelled.append(
                    LabelledPosition(
                        fen=fen,
                        cp_white=cp_white,
                        multipv=rows[: len(candidates)],
                        champion_move=champ_uci,
                        champion_eval_side=champ_score,
                        candidate_moves=tuple(move.uci() for move in candidates),
                        candidate_teacher_eval_side=eval_by_move,
                        target_move=target_uci,
                        baseline_teacher_eval_side=baseline_eval,
                        target_teacher_eval_side=target_eval,
                    )
                )
            else:
                try:
                    rows = engine.analyse(
                        board,
                        limit=analysis_limit(depth=stockfish_depth, movetime_ms=0),
                        multipv=max(1, int(multipv)),
                    )
                except Exception:
                    continue
                rows = [row for row in rows if str(row.get("move") or "")]
                if not rows:
                    continue
                labelled.append(
                    LabelledPosition(
                        fen=fen,
                        cp_white=cp_white,
                        multipv=rows[:multipv],
                        champion_move=champ.uci() if champ is not None else "",
                        champion_eval_side=champ_score,
                    )
                )
            if len(labelled) % 200 == 0:
                print(f"  labelled {len(labelled)}/{label_limit + dev_size} ({time.perf_counter() - t0:.1f}s)", flush=True)
    rng.shuffle(labelled)
    dev = labelled[:dev_size]
    train = labelled[dev_size:]
    return train, dev


def _sample_training_arrays(rows: list[LabelledPosition], *, k_neg: int) -> dict[str, np.ndarray]:
    rng = random.Random(SEED + 1)
    board_x: list[np.ndarray] = []
    pos_x: list[np.ndarray] = []
    neg_x: list[list[np.ndarray]] = []
    value_board_x: list[np.ndarray] = []
    value_targets: list[float] = []
    preserve_x: list[np.ndarray] = []
    preserve_mask: list[float] = []

    for row in rows:
        board = chess.Board(row.fen)
        legal = list(board.legal_moves)
        if len(legal) < 2:
            continue
        if row.target_move and row.candidate_moves:
            try:
                positive = chess.Move.from_uci(row.target_move)
            except Exception:
                continue
            candidate_pool: list[chess.Move] = []
            for uci in row.candidate_moves:
                try:
                    mv = chess.Move.from_uci(uci)
                except Exception:
                    continue
                if mv in legal and mv not in candidate_pool:
                    candidate_pool.append(mv)
            if positive not in legal:
                continue
            negatives = [move for move in candidate_pool if move != positive]
            if not negatives:
                negatives = [move for move in legal if move != positive]
        else:
            pv_moves: list[chess.Move] = []
            for pv in row.multipv:
                try:
                    mv = chess.Move.from_uci(str(pv.get("move") or ""))
                except Exception:
                    continue
                if mv in legal and mv not in pv_moves:
                    pv_moves.append(mv)
            if not pv_moves:
                continue
            positive = pv_moves[0]
            negatives = [move for move in legal if move not in pv_moves]
        rng.shuffle(negatives)
        if not negatives:
            negatives = [move for move in legal if move != positive]
        selected_negs = negatives[:k_neg]
        if not selected_negs:
            continue
        while len(selected_negs) < k_neg:
            selected_negs.append(selected_negs[-1])

        bx = _board_features(board)
        board_x.append(bx)
        pos_x.append(_move_features(board, positive))
        neg_x.append([_move_features(board, move) for move in selected_negs])

        # Value preservation: blend generic Stockfish cp with champion search score.
        sf_side = _cp_side_from_white(row.cp_white, board)
        champ_side = float(row.champion_eval_side)
        target_cp = 0.55 * max(-2000.0, min(2000.0, sf_side)) + 0.45 * max(-2000.0, min(2000.0, champ_side))
        value_board_x.append(bx)
        value_targets.append(math.tanh(target_cp / VALUE_SCALE_CP))
        if row.candidate_teacher_eval_side:
            for uci, teacher_eval_side in row.candidate_teacher_eval_side.items():
                try:
                    candidate = chess.Move.from_uci(uci)
                except Exception:
                    continue
                if candidate not in legal:
                    continue
                after = _after(board, candidate)
                value_board_x.append(_board_features(after))
                # Teacher eval is from the root side-to-move; after the move
                # the side-to-move is the opponent, so flip the sign.
                value_targets.append(math.tanh(-float(teacher_eval_side) / VALUE_SCALE_CP))

        if row.champion_move:
            try:
                champ_mv = chess.Move.from_uci(row.champion_move)
            except Exception:
                champ_mv = None
            if champ_mv in legal:
                preserve_x.append(_move_features(board, champ_mv))
                if row.target_move and row.target_move != row.champion_move:
                    # Candidate-topN labels already require a teacher margin
                    # before switching, so do not over-regularize against the
                    # switch target.
                    preserve_mask.append(0.05)
                else:
                    # Only force preservation in broadly drawable / unclear
                    # positions.
                    preserve_mask.append(1.0 if abs(sf_side) <= 220.0 else 0.35)
            else:
                preserve_x.append(pos_x[-1])
                preserve_mask.append(0.0)
        else:
            preserve_x.append(pos_x[-1])
            preserve_mask.append(0.0)

    return {
        "board": np.stack(board_x).astype(np.float32),
        "positive": np.stack(pos_x).astype(np.float32),
        "negative": np.asarray(neg_x, dtype=np.float32),
        "value_board": np.stack(value_board_x).astype(np.float32),
        "value": np.asarray(value_targets, dtype=np.float32),
        "preserve": np.stack(preserve_x).astype(np.float32),
        "preserve_mask": np.asarray(preserve_mask, dtype=np.float32),
    }


def train_joint_model(
    train_rows: list[LabelledPosition],
    *,
    epochs: int,
    k_neg: int,
    hidden: int,
) -> JointPolicyValue:
    data = _sample_training_arrays(train_rows, k_neg=k_neg)
    model = JointPolicyValue(hidden=hidden)
    optimizer = torch.optim.AdamW(model.parameters(), lr=8e-4, weight_decay=1e-4)
    board = torch.from_numpy(data["board"])
    positive = torch.from_numpy(data["positive"])
    negative = torch.from_numpy(data["negative"])
    value_board = torch.from_numpy(data["value_board"])
    value = torch.from_numpy(data["value"])
    preserve = torch.from_numpy(data["preserve"])
    preserve_mask = torch.from_numpy(data["preserve_mask"])
    n = int(board.shape[0])
    batch_size = min(256, max(32, n))
    print(f"training joint model: rows={n} epochs={epochs} k_neg={k_neg}", flush=True)
    for epoch in range(epochs):
        perm = torch.randperm(n)
        total = 0.0
        pair_acc = 0.0
        pairs = 0
        t0 = time.perf_counter()
        for start in range(0, n, batch_size):
            idx = perm[start:start + batch_size]
            bx = board[idx]
            pos = positive[idx]
            neg = negative[idx]
            preserve_mv = preserve[idx]
            pmask = preserve_mask[idx]

            pos_score = model.policy_score(bx, pos)
            bsz, k, dim = neg.shape
            neg_score = model.policy_score(
                bx[:, None, :].expand(bsz, k, INPUT_DIM).reshape(bsz * k, INPUT_DIM),
                neg.reshape(bsz * k, dim),
            ).reshape(bsz, k)
            diff = pos_score[:, None] - neg_score
            rank_loss = F.softplus(-diff).mean()

            vidx = torch.randint(0, int(value_board.shape[0]), (int(idx.numel()),))
            value_loss = F.smooth_l1_loss(model.value_score(value_board[vidx]), value[vidx])

            # Champion-preservation auxiliary: do not push model away from the
            # champion move in unclear positions. This is not a lookup at
            # runtime; it is only a training regularizer on generic positions.
            preserve_score = model.policy_score(bx, preserve_mv)
            preserve_loss = (F.softplus(-(preserve_score - neg_score.mean(dim=1))) * pmask).mean()

            loss = rank_loss + 0.35 * value_loss + 0.25 * preserve_loss
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            optimizer.step()

            total += float(loss.detach()) * int(diff.numel())
            pair_acc += float((diff > 0).float().sum())
            pairs += int(diff.numel())
        print(
            f"  epoch {epoch + 1:02d}/{epochs}: loss={total / max(1, pairs):.4f} "
            f"pair_acc={pair_acc / max(1, pairs):.3f} dt={time.perf_counter() - t0:.1f}s",
            flush=True,
        )
    return model


@torch.no_grad()
def _policy_scores(model: JointPolicyValue, board: chess.Board, moves: list[chess.Move]) -> np.ndarray:
    if not moves:
        return np.zeros(0, dtype=np.float32)
    bx = np.repeat(_board_features(board)[None, :], len(moves), axis=0)
    mx = np.stack([_move_features(board, move) for move in moves]).astype(np.float32)
    scores = model.policy_score(torch.from_numpy(bx), torch.from_numpy(mx)).detach().cpu().numpy()
    return scores.astype(np.float32)


@torch.no_grad()
def _joint_scores(
    model: JointPolicyValue,
    board: chess.Board,
    moves: list[chess.Move],
    *,
    value_weight: float,
) -> np.ndarray:
    policy = _policy_scores(model, board, moves)
    policy_z = (policy - float(policy.mean())) / (float(policy.std()) or 1.0)
    if not moves or float(value_weight) <= 0.0:
        return policy_z.astype(np.float32)
    after_x = np.stack([_board_features(_after(board, move)) for move in moves]).astype(np.float32)
    # value_score(after) is from after.side-to-move's perspective; the root
    # side is the opponent after a candidate move, so flip the sign.
    root_value = -model.value_score(torch.from_numpy(after_x)).detach().cpu().numpy()
    value_z = (root_value - float(root_value.mean())) / (float(root_value.std()) or 1.0)
    return (policy_z + float(value_weight) * value_z).astype(np.float32)


def evaluate_policy_sanity(
    model: JointPolicyValue,
    dev_rows: list[LabelledPosition],
    *,
    min_policy_margin: float,
    value_weight: float,
) -> dict:
    top1 = 0
    top3 = 0
    top5 = 0
    total = 0
    champion_top3 = 0
    cand_total = 0
    cand_target_top1 = 0
    cand_target_top2 = 0
    cand_target_top3 = 0
    cand_target_top5 = 0
    cand_keep_total = 0
    cand_keep_acc = 0
    cand_switch_total = 0
    cand_switch_recall = 0
    cand_runtime_acc = 0
    cand_safe_choice = 0
    cand_model_switches = 0
    cand_target_switches = 0
    for row in dev_rows:
        board = chess.Board(row.fen)
        legal = list(board.legal_moves)
        if not legal:
            continue
        pv_move = None
        for item in row.multipv:
            try:
                candidate = chess.Move.from_uci(str(item.get("move") or ""))
            except Exception:
                continue
            if candidate in legal:
                pv_move = candidate
                break
        if pv_move is None:
            continue
        scores = _policy_scores(model, board, legal)
        ranked = [legal[i] for i in np.argsort(-scores)]
        total += 1
        top1 += int(ranked[0] == pv_move)
        top3 += int(pv_move in ranked[:3])
        top5 += int(pv_move in ranked[:5])
        if row.champion_move:
            try:
                champ = chess.Move.from_uci(row.champion_move)
            except Exception:
                champ = None
            if champ in legal:
                champion_top3 += int(champ in ranked[:3])
        if row.target_move and row.candidate_moves and row.candidate_teacher_eval_side:
            candidates: list[chess.Move] = []
            for uci in row.candidate_moves:
                try:
                    candidate = chess.Move.from_uci(uci)
                except Exception:
                    continue
                if candidate in legal and candidate not in candidates:
                    candidates.append(candidate)
            try:
                target = chess.Move.from_uci(row.target_move)
                baseline = chess.Move.from_uci(row.champion_move)
            except Exception:
                continue
            if target not in candidates or baseline not in candidates:
                continue
            cand_scores = _joint_scores(model, board, candidates, value_weight=float(value_weight))
            cand_ranked = [candidates[i] for i in np.argsort(-cand_scores)]
            cand_total += 1
            cand_target_top1 += int(cand_ranked[0] == target)
            cand_target_top2 += int(target in cand_ranked[:2])
            cand_target_top3 += int(target in cand_ranked[:3])
            cand_target_top5 += int(target in cand_ranked[:5])
            target_is_switch = target != baseline
            cand_target_switches += int(target_is_switch)
            if target_is_switch:
                cand_switch_total += 1
            else:
                cand_keep_total += 1
            baseline_idx = candidates.index(baseline)
            z = (cand_scores - float(cand_scores.mean())) / (float(cand_scores.std()) or 1.0)
            best_idx = int(np.argmax(z))
            runtime_choice = baseline
            if candidates[best_idx] != baseline and float(z[best_idx]) >= float(z[baseline_idx]) + float(min_policy_margin):
                runtime_choice = candidates[best_idx]
            cand_model_switches += int(runtime_choice != baseline)
            cand_runtime_acc += int(runtime_choice == target)
            cand_keep_acc += int((not target_is_switch) and runtime_choice == baseline)
            cand_switch_recall += int(target_is_switch and runtime_choice == target)
            eval_by_move = row.candidate_teacher_eval_side
            chosen_eval = float(eval_by_move.get(runtime_choice.uci(), row.baseline_teacher_eval_side))
            cand_safe_choice += int(chosen_eval >= float(row.baseline_teacher_eval_side) - 40.0)
    result = {
        "n": total,
        "top1": round(top1 / max(1, total), 4),
        "top3": round(top3 / max(1, total), 4),
        "top5": round(top5 / max(1, total), 4),
        "champion_top3": round(champion_top3 / max(1, total), 4),
    }
    if cand_total:
        result["candidate_topn"] = {
            "n": cand_total,
            "target_top1": round(cand_target_top1 / max(1, cand_total), 4),
            "target_top2": round(cand_target_top2 / max(1, cand_total), 4),
            "target_top3": round(cand_target_top3 / max(1, cand_total), 4),
            "target_top5": round(cand_target_top5 / max(1, cand_total), 4),
            "runtime_choice_acc": round(cand_runtime_acc / max(1, cand_total), 4),
            "baseline_keep_acc": round(cand_keep_acc / max(1, cand_keep_total), 4),
            "switch_recall": round(cand_switch_recall / max(1, cand_switch_total), 4),
            "safe_choice_rate": round(cand_safe_choice / max(1, cand_total), 4),
            "target_switch_rate": round(cand_target_switches / max(1, cand_total), 4),
            "model_switch_rate": round(cand_model_switches / max(1, cand_total), 4),
        }
    return result


def _root_search_scores(
    board: chess.Board,
    *,
    evaluator: NeuralEvaluator,
    profile: dict,
    top_n: int,
) -> list[tuple[chess.Move, int]]:
    scored: list[tuple[chess.Move, int]] = []
    for move in board.legal_moves:
        after = _after(board, move)
        if after.is_checkmate():
            scored.append((move, 10_000_000))
            continue
        child = search_best_move(
            after,
            max_depth=1,
            evaluate=evaluator,
            move_order_fn=_principled_move_order_score,
            quiescence_depth=int(profile["quiescence_depth"]),
            hasher=ZobristHasher(seed=20260601),
            time_budget_ms=120,
            enable_pvs=True,
            enable_lmr=False,
            enable_null_move=False,
            enable_futility=True,
        )
        scored.append((move, -int(child.score)))
    return sorted(scored, key=lambda item: (item[1], item[0].uci()), reverse=True)[:max(1, top_n)]


def _verified_root_score(
    board: chess.Board,
    move: chess.Move,
    *,
    evaluator: NeuralEvaluator,
    profile: dict,
    verify_depth: int,
    verify_time_budget_ms: int,
) -> int:
    try:
        after = _after(board, move)
    except Exception:
        return -10_000_000
    if after.is_checkmate():
        return 10_000_000
    child = search_best_move(
        after,
        max_depth=max(1, int(verify_depth)),
        evaluate=evaluator,
        move_order_fn=_principled_move_order_score,
        quiescence_depth=int(profile["quiescence_depth"]),
        hasher=ZobristHasher(seed=20260601),
        time_budget_ms=max(1, int(verify_time_budget_ms)),
        enable_pvs=True,
        enable_lmr=bool(int(verify_depth) >= 2),
        enable_null_move=False,
        enable_futility=True,
    )
    return -int(child.score)


def _risk_guard(
    board: chess.Board,
    baseline_move: chess.Move,
    candidate: chess.Move,
    *,
    baseline_score: int,
    candidate_score: int,
    max_score_drop_cp: int,
    max_material_drop_cp: int,
) -> bool:
    if candidate == baseline_move:
        return True
    if candidate_score < baseline_score - int(max_score_drop_cp):
        return False
    try:
        base_after = _after(board, baseline_move)
        cand_after = _after(board, candidate)
    except Exception:
        return False
    if cand_after.is_stalemate() and not base_after.is_stalemate():
        return False
    if _opponent_has_mate_in_one(cand_after):
        return False
    color = board.turn
    base_margin = _material_margin_for_color(base_after, color)
    cand_margin = _material_margin_for_color(cand_after, color)
    if cand_margin < base_margin - int(max_material_drop_cp):
        return False
    return True


def choose_joint_rerank_move(
    board: chess.Board,
    *,
    weights_path: Path,
    model: JointPolicyValue,
    top_n: int,
    max_score_drop_cp: int,
    max_material_drop_cp: int,
    min_policy_margin: float,
    value_weight: float,
    verify_depth: int,
    verify_time_budget_ms: int,
    min_verified_improve_cp: int,
) -> tuple[chess.Move | None, dict]:
    for move in board.legal_moves:
        board.push(move)
        try:
            if board.is_checkmate():
                return move, {"mode": "forced_mate"}
        finally:
            board.pop()

    weights = load_weights(weights_path)
    evaluator = NeuralEvaluator(weights)
    profile = dict(_SEARCH_PROFILES["balanced"])
    baseline_move, baseline_score = _champion_baseline_move(board, evaluator=evaluator, profile=profile)
    if baseline_move is None:
        return None, {"mode": "no_baseline"}

    root_scores = _root_search_scores(board, evaluator=evaluator, profile=profile, top_n=max(top_n, 1))
    if baseline_move not in [move for move, _score in root_scores]:
        root_scores.append((baseline_move, baseline_score))
    by_move = {move: score for move, score in root_scores}
    candidates = [move for move, _score in sorted(root_scores, key=lambda item: (item[1], item[0].uci()), reverse=True)[:top_n]]
    if baseline_move not in candidates:
        candidates.append(baseline_move)

    policy_z = _joint_scores(model, board, candidates, value_weight=float(value_weight))
    baseline_idx = candidates.index(baseline_move)
    baseline_policy = float(policy_z[baseline_idx])
    ranked_indices = list(np.argsort(-policy_z))
    chosen = baseline_move
    chosen_reason = "fallback_baseline"
    chosen_score = int(by_move.get(baseline_move, baseline_score))
    for idx in ranked_indices:
        move = candidates[int(idx)]
        if move == baseline_move:
            continue
        candidate_policy = float(policy_z[int(idx)])
        if candidate_policy < baseline_policy + float(min_policy_margin):
            continue
        candidate_score = int(by_move.get(move, baseline_score - 10_000))
        if int(verify_depth) > 0:
            verified_baseline = _verified_root_score(
                board,
                baseline_move,
                evaluator=evaluator,
                profile=profile,
                verify_depth=int(verify_depth),
                verify_time_budget_ms=int(verify_time_budget_ms),
            )
            verified_candidate = _verified_root_score(
                board,
                move,
                evaluator=evaluator,
                profile=profile,
                verify_depth=int(verify_depth),
                verify_time_budget_ms=int(verify_time_budget_ms),
            )
            if verified_candidate < verified_baseline + int(min_verified_improve_cp):
                continue
        if _risk_guard(
            board,
            baseline_move,
            move,
            baseline_score=chosen_score,
            candidate_score=candidate_score,
            max_score_drop_cp=max_score_drop_cp,
            max_material_drop_cp=max_material_drop_cp,
        ):
            chosen = move
            chosen_reason = "joint_rerank_guard_passed"
            break

    return chosen, {
        "mode": chosen_reason,
        "baseline_move": baseline_move.uci(),
        "chosen_move": chosen.uci(),
        "candidate_count": len(candidates),
        "changed": bool(chosen != baseline_move),
    }


def play_one_game(
    *,
    model: JointPolicyValue,
    weights_path: Path,
    opening_id: str,
    opening_moves: list[str],
    exp6_color_name: str,
    stockfish_depth: int,
    engine: UciStockfish,
    top_n: int,
    max_score_drop_cp: int,
    max_material_drop_cp: int,
    min_policy_margin: float,
    value_weight: float,
    verify_depth: int,
    verify_time_budget_ms: int,
    min_verified_improve_cp: int,
    max_plies: int = 400,
) -> dict:
    board = chess.Board()
    for uci in opening_moves:
        board.push_uci(uci)
    exp6_color = chess.WHITE if exp6_color_name == "white" else chess.BLACK
    invalid_actor = None
    exp6_times: list[float] = []
    changed = 0
    decisions = 0
    wall0 = time.perf_counter()
    for _ply in range(max_plies):
        if board.is_game_over(claim_draw=True):
            break
        if board.turn == exp6_color:
            t0 = time.perf_counter()
            move, audit = choose_joint_rerank_move(
                board,
                weights_path=weights_path,
                model=model,
                top_n=top_n,
                max_score_drop_cp=max_score_drop_cp,
                max_material_drop_cp=max_material_drop_cp,
                min_policy_margin=min_policy_margin,
                value_weight=value_weight,
                verify_depth=verify_depth,
                verify_time_budget_ms=verify_time_budget_ms,
                min_verified_improve_cp=min_verified_improve_cp,
            )
            exp6_times.append(time.perf_counter() - t0)
            decisions += 1
            changed += int(bool(audit.get("changed")))
            if move is None or move not in board.legal_moves:
                invalid_actor = "exp6"
                break
        else:
            try:
                pv = engine.analyse(board, limit=analysis_limit(depth=stockfish_depth, movetime_ms=0), multipv=1)
            except Exception:
                invalid_actor = "stockfish"
                break
            if not pv:
                invalid_actor = "stockfish"
                break
            try:
                move = chess.Move.from_uci(str(pv[0]["move"]))
            except Exception:
                invalid_actor = "stockfish"
                break
            if move not in board.legal_moves:
                invalid_actor = "stockfish"
                break
        board.push(move)

    outcome = board.outcome(claim_draw=True)
    if invalid_actor:
        result = "stockfish_win" if invalid_actor == "exp6" else "exp6_win"
        reason = "invalid_move"
    elif outcome is None:
        result = "incomplete"
        reason = "max_plies"
    elif outcome.winner is None:
        result = "draw"
        reason = outcome.termination.name.lower()
    else:
        result = "exp6_win" if outcome.winner == exp6_color else "stockfish_win"
        reason = outcome.termination.name.lower()
    score = cc.SCORE_WIN if result == "exp6_win" else (cc.SCORE_DRAW if result == "draw" else cc.SCORE_LOSS)
    mean_ms = (sum(exp6_times) / len(exp6_times) * 1000.0) if exp6_times else 0.0
    max_ms = (max(exp6_times) * 1000.0) if exp6_times else 0.0
    return {
        "opening_id": opening_id,
        "stockfish_depth": int(stockfish_depth),
        "exp6_color": exp6_color_name,
        "result": result,
        "reason": reason,
        "plies": len(board.move_stack),
        "score_points": int(score),
        "elapsed_wall_s": round(time.perf_counter() - wall0, 3),
        "exp6_mean_ms": round(mean_ms, 1),
        "exp6_max_ms": round(max_ms, 1),
        "rerank_changed": int(changed),
        "rerank_decisions": int(decisions),
    }


def run_staged_gate(
    *,
    model: JointPolicyValue,
    weights_path: Path,
    out_path: Path,
    top_n: int,
    max_score_drop_cp: int,
    max_material_drop_cp: int,
    min_policy_margin: float,
    value_weight: float,
    verify_depth: int,
    verify_time_budget_ms: int,
    min_verified_improve_cp: int,
    games_limit: int,
) -> dict:
    sf_path = resolve_stockfish_path()
    if not sf_path:
        raise SystemExit("Stockfish not found")
    schedule: list[tuple[str, list[str], str, int]] = []
    for depth in cc.STAGED_DEPTHS:
        for k in range(cc.STAGED_GAMES_PER_DEPTH):
            opening_id, opening_moves = cc.STAGED_OPENINGS[(depth + k - 1) % len(cc.STAGED_OPENINGS)]
            exp6_color = "white" if k % 2 == 0 else "black"
            schedule.append((opening_id, opening_moves, exp6_color, depth))
    if games_limit > 0:
        schedule = schedule[:games_limit]

    rows: list[dict] = []
    early_abort = ""
    with UciStockfish(sf_path) as engine:
        for idx, (opening_id, opening_moves, exp6_color, depth) in enumerate(schedule, start=1):
            row = play_one_game(
                model=model,
                weights_path=weights_path,
                opening_id=opening_id,
                opening_moves=opening_moves,
                exp6_color_name=exp6_color,
                stockfish_depth=depth,
                engine=engine,
                top_n=top_n,
                max_score_drop_cp=max_score_drop_cp,
                max_material_drop_cp=max_material_drop_cp,
                min_policy_margin=min_policy_margin,
                value_weight=value_weight,
                verify_depth=verify_depth,
                verify_time_budget_ms=verify_time_budget_ms,
                min_verified_improve_cp=min_verified_improve_cp,
            )
            rows.append(row)
            print(
                f"    joint_rerank g{idx:02d} d{depth} {opening_id}/exp6={exp6_color}: "
                f"{row['result']:>14s} ({row['reason']}) {row['plies']:3d}p "
                f"mean={row['exp6_mean_ms']:.0f}ms max={row['exp6_max_ms']:.0f}ms "
                f"changed={row['rerank_changed']}/{row['rerank_decisions']} score={row['score_points']:+d}",
                flush=True,
            )
            if len(rows) >= 4:
                losses = sum(1 for item in rows[:4] if item["result"] == "stockfish_win")
                draws = sum(1 for item in rows[:4] if item["result"] == "draw")
                prefix_score = sum(int(item["score_points"]) for item in rows[:4])
                if losses == 4:
                    early_abort = "0W/0D/4L early defence break"
                    break
                if prefix_score < -4 and losses >= 2:
                    early_abort = f"prefix below current defence ({draws}D/{losses}L, score {prefix_score:+d})"
                    break

    wins = sum(1 for item in rows if item["result"] == "exp6_win")
    draws = sum(1 for item in rows if item["result"] == "draw")
    losses = sum(1 for item in rows if item["result"] == "stockfish_win")
    total = sum(int(item["score_points"]) for item in rows)
    summary = {
        "W": wins,
        "D": draws,
        "L": losses,
        "score_total": total,
        "score_max": len(rows) * cc.SCORE_WIN,
        "early_abort": early_abort,
        "games": len(rows),
        "mean_exp6_ms": round(sum(float(item["exp6_mean_ms"]) for item in rows) / max(1, len(rows)), 1),
        "max_exp6_ms": max((float(item["exp6_max_ms"]) for item in rows), default=0.0),
        "rerank_changes": sum(int(item["rerank_changed"]) for item in rows),
        "rerank_decisions": sum(int(item["rerank_decisions"]) for item in rows),
    }
    payload = {"summary": summary, "games": rows}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--label-limit", type=int, default=900)
    parser.add_argument("--dev-size", type=int, default=160)
    parser.add_argument("--stockfish-depth", type=int, default=4)
    parser.add_argument("--multipv", type=int, default=3)
    parser.add_argument("--max-fullmove", type=int, default=18)
    parser.add_argument("--label-mode", choices=("candidate_topn", "legal_pv"), default="candidate_topn")
    parser.add_argument("--candidate-top-n", type=int, default=4)
    parser.add_argument("--min-switch-improve-cp", type=int, default=90)
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--k-neg", type=int, default=5)
    parser.add_argument("--hidden", type=int, default=128)
    parser.add_argument("--top-n", type=int, default=2)
    parser.add_argument("--max-score-drop-cp", type=int, default=0)
    parser.add_argument("--max-material-drop-cp", type=int, default=0)
    parser.add_argument("--min-policy-margin", type=float, default=2.0)
    parser.add_argument("--value-rerank-weight", type=float, default=0.0)
    parser.add_argument("--verify-depth", type=int, default=0)
    parser.add_argument("--verify-time-budget-ms", type=int, default=220)
    parser.add_argument("--min-verified-improve-cp", type=int, default=60)
    parser.add_argument("--min-policy-top3", type=float, default=0.35)
    parser.add_argument("--min-policy-top5", type=float, default=0.50)
    parser.add_argument("--min-candidate-target-top1", type=float, default=0.50)
    parser.add_argument("--min-candidate-keep-acc", type=float, default=0.75)
    parser.add_argument("--min-candidate-safe-rate", type=float, default=0.90)
    parser.add_argument("--games-limit", type=int, default=4)
    args = parser.parse_args()

    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.set_num_threads(2)

    print("building generic labels...", flush=True)
    train_rows, dev_rows = build_training_labels(
        labels_path=args.labels,
        weights_path=args.weights,
        label_limit=max(1, int(args.label_limit)),
        dev_size=max(1, int(args.dev_size)),
        max_fullmove=max(1, int(args.max_fullmove)),
        stockfish_depth=max(1, int(args.stockfish_depth)),
        multipv=max(1, int(args.multipv)),
        label_mode=str(args.label_mode),
        candidate_top_n=max(2, int(args.candidate_top_n)),
        min_switch_improve_cp=max(0, int(args.min_switch_improve_cp)),
    )
    print(f"labels: train={len(train_rows)} dev={len(dev_rows)}", flush=True)
    model = train_joint_model(
        train_rows,
        epochs=max(1, int(args.epochs)),
        k_neg=max(1, int(args.k_neg)),
        hidden=max(16, int(args.hidden)),
    )
    sanity = evaluate_policy_sanity(
        model,
        dev_rows,
        min_policy_margin=float(args.min_policy_margin),
        value_weight=float(args.value_rerank_weight),
    )
    print(f"policy sanity: {sanity}", flush=True)

    candidate_sanity = sanity.get("candidate_topn") if isinstance(sanity.get("candidate_topn"), dict) else {}
    if str(args.label_mode) == "candidate_topn":
        sanity_failed = (
            float(candidate_sanity.get("target_top1") or 0.0) < float(args.min_candidate_target_top1)
            or float(candidate_sanity.get("target_top3") or 0.0) < float(args.min_policy_top3)
            or float(candidate_sanity.get("target_top5") or 0.0) < float(args.min_policy_top5)
            or float(candidate_sanity.get("baseline_keep_acc") or 0.0) < float(args.min_candidate_keep_acc)
            or float(candidate_sanity.get("safe_choice_rate") or 0.0) < float(args.min_candidate_safe_rate)
        )
    else:
        sanity_failed = (
            float(sanity.get("top3") or 0.0) < float(args.min_policy_top3)
            or float(sanity.get("top5") or 0.0) < float(args.min_policy_top5)
        )
    if sanity_failed:
        payload = {
            "summary": {
                "W": 0,
                "D": 0,
                "L": 0,
                "score_total": 0,
                "score_max": 0,
                "early_abort": "policy_sanity_failed_before_staged",
                "games": 0,
            },
            "games": [],
            "training": {
                "train_rows": len(train_rows),
                "dev_rows": len(dev_rows),
                "stockfish_depth": int(args.stockfish_depth),
                "multipv": int(args.multipv),
                "label_mode": str(args.label_mode),
                "candidate_top_n": int(args.candidate_top_n),
                "min_switch_improve_cp": int(args.min_switch_improve_cp),
                "policy_sanity": sanity,
                "policy_sanity_gate": {
                    "min_top3": float(args.min_policy_top3),
                    "min_top5": float(args.min_policy_top5),
                    "min_candidate_target_top1": float(args.min_candidate_target_top1),
                    "min_candidate_keep_acc": float(args.min_candidate_keep_acc),
                    "min_candidate_safe_rate": float(args.min_candidate_safe_rate),
                },
                "runtime_policy": "conservative_topN_rerank_only",
                "value_rerank_weight": float(args.value_rerank_weight),
                "verify_depth": int(args.verify_depth),
                "min_verified_improve_cp": int(args.min_verified_improve_cp),
                "no_root_policy_bonus": True,
            },
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2))
        print("policy sanity failed; staged gate skipped", flush=True)
        print(f"saved -> {args.out}", flush=True)
        return 0

    staged = run_staged_gate(
        model=model,
        weights_path=args.weights,
        out_path=args.out,
        top_n=max(1, int(args.top_n)),
        max_score_drop_cp=max(0, int(args.max_score_drop_cp)),
        max_material_drop_cp=max(0, int(args.max_material_drop_cp)),
        min_policy_margin=float(args.min_policy_margin),
        value_weight=float(args.value_rerank_weight),
        verify_depth=max(0, int(args.verify_depth)),
        verify_time_budget_ms=max(1, int(args.verify_time_budget_ms)),
        min_verified_improve_cp=int(args.min_verified_improve_cp),
        games_limit=max(0, int(args.games_limit)),
    )
    payload = json.loads(args.out.read_text())
    payload["training"] = {
        "train_rows": len(train_rows),
        "dev_rows": len(dev_rows),
        "stockfish_depth": int(args.stockfish_depth),
        "multipv": int(args.multipv),
        "label_mode": str(args.label_mode),
        "candidate_top_n": int(args.candidate_top_n),
        "min_switch_improve_cp": int(args.min_switch_improve_cp),
        "policy_sanity": sanity,
        "runtime_policy": "conservative_topN_rerank_only",
        "value_rerank_weight": float(args.value_rerank_weight),
        "verify_depth": int(args.verify_depth),
        "min_verified_improve_cp": int(args.min_verified_improve_cp),
        "no_root_policy_bonus": True,
    }
    args.out.write_text(json.dumps(payload, indent=2))
    summary = staged["summary"]
    print(
        f"summary: {summary['W']}W/{summary['D']}D/{summary['L']}L "
        f"score={summary['score_total']:+d}/{summary['score_max']} "
        f"early_abort={summary['early_abort'] or 'none'}",
        flush=True,
    )
    print(f"saved -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
