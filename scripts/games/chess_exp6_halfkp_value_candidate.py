#!/usr/bin/env python3
"""Exp6 HalfKP-style value evaluator candidate.

This is an experiment harness, not a runtime promotion path. It trains and
tests a lightweight HalfKP/HalfKAv2-inspired value evaluator that can be
plugged into the existing Exp6 search. It does not modify the locked champion,
does not train a policy head, and does not add a root policy bonus.

Staged traces are redacted: no move lists and no FENs are persisted.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import chess
import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"PyTorch is required for this experiment: {exc}")


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.chess import FEN_KEY, to_chess_board  # noqa: E402
from services.games.chess_exp6 import (  # noqa: E402
    _SEARCH_PROFILES,
    _move_order_score,
    _opening_principle_filter,
    _principled_move_order_score,
    choose_experiment_neural_move,
)
from services.games.chess_neural import static_baseline_cp_white  # noqa: E402
from services.games.chess_search import ZobristHasher, opening_sanity_filter, search_best_move  # noqa: E402
from services.games.chess_stockfish_teacher import UciStockfish, analysis_limit, resolve_stockfish_path  # noqa: E402
from scripts.games.common_paths import exp6_artifacts_root, exp6_private_dir, runtime_model_path  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402


SEED = 20260520
DEFAULT_LABELS = exp6_private_dir() / "curriculum_labels_10k.jsonl"
DEFAULT_CHAMPION = runtime_model_path("chess_experiment_6_neural.npz")
DEFAULT_OUT = exp6_artifacts_root() / "halfkp_value" / "exp6_halfkp_v1.pt"
DEFAULT_REPORT = exp6_artifacts_root() / "halfkp_value" / "exp6_halfkp_v1_report.json"
EXPECTED_CHAMPION_MD5 = "1c27627adb3c4597561bc7509438e25c"

FEATURE_DIM = 64 * 12 * 64
MAX_PIECES = 32
VALUE_SCALE_CP = 600.0
CP_CLIP = 2000.0


@dataclass
class SearchVariant:
    name: str
    depth: int
    qdepth: int
    time_budget_ms: int | None
    move_order: str
    opening_filter: bool
    enable_pvs: bool
    enable_lmr: bool
    enable_null_move: bool
    enable_futility: bool
    lmr_min_move_index: int = 4


SEARCH_VARIANTS: dict[str, SearchVariant] = {
    "same_search": SearchVariant(
        "same_search", 2, 2, 600, "principled", True, True, True, False, True, 4
    ),
    "depth2_narrow": SearchVariant(
        "depth2_narrow", 2, 1, 420, "principled", True, True, True, False, True, 2
    ),
    "depth3_narrow": SearchVariant(
        "depth3_narrow", 3, 1, 1100, "principled", True, True, True, False, True, 2
    ),
    "q_off": SearchVariant(
        "q_off", 2, 0, 600, "principled", True, True, True, False, True, 4
    ),
    "order_off": SearchVariant(
        "order_off", 2, 2, 600, "none", False, True, True, False, True, 4
    ),
}


TACTICAL_FENS = [
    ("white_mate_in_1", "6k1/5Q2/6K1/8/8/8/8/8 w - - 0 1", "mate_in_one"),
    ("black_mate_in_1", "8/8/8/8/8/6k1/5q2/6K1 b - - 0 1", "mate_in_one"),
    ("white_promotion", "4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "legal"),
]

BLUNDER_FENS = [
    ("start_position", chess.STARTING_FEN),
    ("quiet_opening", "rnbqkbnr/pppp1ppp/4p3/8/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"),
    ("simple_endgame", "8/8/3k4/8/8/4K3/4R3/8 w - - 0 1"),
]

ABLATION_FENS = [
    chess.STARTING_FEN,
    "rnbqkbnr/pppp1ppp/4p3/8/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
    "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/2N2N2/PPPP1PPP/R1BQKB1R w KQkq - 2 3",
    "8/8/3k4/8/8/4K3/4R3/8 w - - 0 1",
]


def champion_md5(path: Path = DEFAULT_CHAMPION) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def current_commit_hash() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _mirror_square(square: int) -> int:
    return chess.square(chess.square_file(square), 7 - chess.square_rank(square))


def _relative_piece_index(piece: chess.Piece, perspective: chess.Color) -> int:
    base = int(piece.piece_type) - 1
    return base if piece.color == perspective else 6 + base


def _halfkp_indices_for(board: chess.Board, perspective: chess.Color) -> list[int]:
    king = board.king(perspective)
    if king is None:
        return []
    king_sq = king if perspective == chess.WHITE else _mirror_square(king)
    base = int(king_sq) * 12 * 64
    out: list[int] = []
    for square, piece in board.piece_map().items():
        sq = square if perspective == chess.WHITE else _mirror_square(square)
        pidx = _relative_piece_index(piece, perspective)
        out.append(base + pidx * 64 + int(sq))
    return out[:MAX_PIECES]


def halfkp_pair(board: chess.Board) -> tuple[np.ndarray, np.ndarray]:
    white = _halfkp_indices_for(board, chess.WHITE)
    black = _halfkp_indices_for(board, chess.BLACK)
    return np.asarray(white, dtype=np.int64), np.asarray(black, dtype=np.int64)


def _pad_indices(rows: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    arr = np.zeros((len(rows), MAX_PIECES), dtype=np.int64)
    mask = np.zeros((len(rows), MAX_PIECES), dtype=np.float32)
    for i, row in enumerate(rows):
        n = min(MAX_PIECES, len(row))
        if n:
            arr[i, :n] = row[:n]
            mask[i, :n] = 1.0
    return arr, mask


class HalfKPValueNet(nn.Module):
    def __init__(self, hidden: int = 64) -> None:
        super().__init__()
        self.hidden = int(hidden)
        self.emb = nn.Embedding(FEATURE_DIM, hidden)
        self.bias = nn.Parameter(torch.zeros(hidden))
        self.out = nn.Linear(hidden, 1)
        self.stm_bias = nn.Parameter(torch.zeros(()))
        nn.init.normal_(self.emb.weight, mean=0.0, std=0.015)
        nn.init.zeros_(self.out.weight)
        nn.init.zeros_(self.out.bias)

    def residual_scaled(
        self,
        white_idx: torch.Tensor,
        white_mask: torch.Tensor,
        black_idx: torch.Tensor,
        black_mask: torch.Tensor,
        stm_white: torch.Tensor,
    ) -> torch.Tensor:
        white_sum = (self.emb(white_idx) * white_mask.unsqueeze(-1)).sum(dim=1)
        black_sum = (self.emb(black_idx) * black_mask.unsqueeze(-1)).sum(dim=1)
        hidden = torch.tanh((white_sum - black_sum + self.bias) / math.sqrt(float(self.hidden)))
        stm = torch.where(stm_white > 0.5, 1.0, -1.0)
        return self.out(hidden).squeeze(-1) + self.stm_bias * stm


def save_model(path: Path, model: HalfKPValueNet, *, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": model.state_dict(), "config": config}, path)


def load_model(path: Path) -> HalfKPValueNet:
    payload = torch.load(path, map_location="cpu")
    hidden = int((payload.get("config") or {}).get("hidden", 64))
    model = HalfKPValueNet(hidden=hidden)
    model.load_state_dict(payload["state_dict"])
    model.eval()
    return model


def _record_from_label(rec: dict) -> dict | None:
    try:
        board = chess.Board(str(rec["fen"]))
        cp_white = float(rec.get("cp_white", rec.get("blended_cp")))
    except Exception:
        return None
    if board.is_game_over() or board.king(chess.WHITE) is None or board.king(chess.BLACK) is None:
        return None
    white, black = halfkp_pair(board)
    return {
        "fen": board.fen(),
        "white": white,
        "black": black,
        "stm_white": board.turn == chess.WHITE,
        "baseline": static_baseline_cp_white(board),
        "target_cp": max(-CP_CLIP, min(CP_CLIP, cp_white)),
    }


def load_dataset(labels_path: Path, *, train_limit: int, dev_size: int, max_fullmove: int) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    with labels_path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            item = _record_from_label(rec)
            if item is None:
                continue
            if chess.Board(item["fen"]).fullmove_number > int(max_fullmove):
                continue
            rows.append(item)
            if len(rows) >= max(train_limit + dev_size, 1) * 8:
                break
    rng = random.Random(SEED)
    rng.shuffle(rows)
    rows = rows[: train_limit + dev_size]
    return rows[dev_size:], rows[:dev_size]


def _batchify(rows: list[dict]) -> dict[str, torch.Tensor]:
    widx, wmask = _pad_indices([row["white"] for row in rows])
    bidx, bmask = _pad_indices([row["black"] for row in rows])
    return {
        "white_idx": torch.from_numpy(widx),
        "white_mask": torch.from_numpy(wmask),
        "black_idx": torch.from_numpy(bidx),
        "black_mask": torch.from_numpy(bmask),
        "stm_white": torch.tensor([1.0 if row["stm_white"] else 0.0 for row in rows], dtype=torch.float32),
        "baseline": torch.tensor([float(row["baseline"]) for row in rows], dtype=torch.float32),
        "target_cp": torch.tensor([float(row["target_cp"]) for row in rows], dtype=torch.float32),
    }


@torch.no_grad()
def evaluate_rows(model: HalfKPValueNet, rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    data = _batchify(rows)
    residual = model.residual_scaled(
        data["white_idx"], data["white_mask"], data["black_idx"], data["black_mask"], data["stm_white"]
    ) * VALUE_SCALE_CP
    pred_cp = data["baseline"] + residual
    pred_v = torch.tanh(pred_cp / VALUE_SCALE_CP)
    target_v = torch.tanh(data["target_cp"] / VALUE_SCALE_CP)
    loss = F.smooth_l1_loss(pred_v, target_v).item()
    corr = 0.0
    pred_np = pred_cp.numpy()
    target_np = data["target_cp"].numpy()
    if len(rows) > 1 and float(np.std(pred_np)) > 1e-6 and float(np.std(target_np)) > 1e-6:
        corr = float(np.corrcoef(pred_np, target_np)[0, 1])
    sign_acc = float(((pred_np > 0) == (target_np > 0)).mean())
    return {
        "n": len(rows),
        "smooth_l1_tanh": round(loss, 6),
        "cp_corr": round(corr, 4),
        "sign_acc": round(sign_acc, 4),
        "avg_abs_cp_err": round(float(np.abs(pred_np - target_np).mean()), 2),
    }


def train_model(
    train_rows: list[dict],
    dev_rows: list[dict],
    *,
    hidden: int,
    epochs: int,
    batch_size: int,
    lr: float,
) -> tuple[HalfKPValueNet, list[dict], dict]:
    model = HalfKPValueNet(hidden=hidden)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    data = _batchify(train_rows)
    n = int(data["target_cp"].shape[0])
    history: list[dict] = []
    best_state = None
    best_loss = float("inf")
    print(f"training HalfKP value: rows={n} dev={len(dev_rows)} hidden={hidden} epochs={epochs}", flush=True)
    for epoch in range(max(1, int(epochs))):
        perm = torch.randperm(n)
        total = 0.0
        seen = 0
        for start in range(0, n, batch_size):
            idx = perm[start:start + batch_size]
            residual = model.residual_scaled(
                data["white_idx"][idx],
                data["white_mask"][idx],
                data["black_idx"][idx],
                data["black_mask"][idx],
                data["stm_white"][idx],
            ) * VALUE_SCALE_CP
            pred_cp = data["baseline"][idx] + residual
            target_cp = data["target_cp"][idx]
            pred_v = torch.tanh(pred_cp / VALUE_SCALE_CP)
            target_v = torch.tanh(target_cp / VALUE_SCALE_CP)
            loss = F.smooth_l1_loss(pred_v, target_v) + 0.05 * F.smooth_l1_loss(pred_cp / CP_CLIP, target_cp / CP_CLIP)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            optimizer.step()
            total += float(loss.detach()) * int(idx.numel())
            seen += int(idx.numel())
        dev = evaluate_rows(model, dev_rows)
        metric = float(dev.get("smooth_l1_tanh") or 0.0)
        if metric < best_loss:
            best_loss = metric
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            tag = "best"
        else:
            tag = "stale"
        row = {"epoch": epoch + 1, "train_loss": round(total / max(1, seen), 6), "dev": dev, "tag": tag}
        history.append(row)
        print(
            f"  epoch {epoch + 1:02d}/{epochs}: train={row['train_loss']:.5f} "
            f"dev={metric:.5f} corr={dev.get('cp_corr', 0):.3f} sign={dev.get('sign_acc', 0):.3f} {tag}",
            flush=True,
        )
    if best_state is not None:
        model.load_state_dict(best_state)
    return model, history, evaluate_rows(model, dev_rows)


_MODEL_CACHE: dict[str, HalfKPValueNet] = {}


def _model_for(path: Path) -> HalfKPValueNet:
    key = str(path)
    model = _MODEL_CACHE.get(key)
    if model is None:
        model = load_model(path)
        _MODEL_CACHE[key] = model
    return model


@torch.no_grad()
def evaluate_halfkp_board(board: chess.Board, model: HalfKPValueNet) -> int:
    if board.is_game_over():
        if board.is_checkmate():
            return -1_000_000
        return 0
    white, black = halfkp_pair(board)
    widx, wmask = _pad_indices([white])
    bidx, bmask = _pad_indices([black])
    stm = torch.tensor([1.0 if board.turn == chess.WHITE else 0.0], dtype=torch.float32)
    residual = model.residual_scaled(
        torch.from_numpy(widx), torch.from_numpy(wmask), torch.from_numpy(bidx), torch.from_numpy(bmask), stm
    )[0].item() * VALUE_SCALE_CP
    cp_white = static_baseline_cp_white(board) + float(residual)
    return int(cp_white if board.turn == chess.WHITE else -cp_white)


def _state_to_board(board_state, side: str) -> chess.Board:
    board = to_chess_board(board_state, side)
    board.turn = chess.WHITE if str(side).lower() == "white" else chess.BLACK
    return board


def _variant(name: str) -> SearchVariant:
    return SEARCH_VARIANTS.get(name, SEARCH_VARIANTS["same_search"])


def choose_halfkp_move(board_state, side: str, *, model_path: Path, variant_name: str = "same_search") -> dict | None:
    board = _state_to_board(board_state, side)
    if board.is_game_over():
        return None
    for move in board.legal_moves:
        board.push(move)
        try:
            if board.is_checkmate():
                return _move_payload(move)
        finally:
            board.pop()

    model = _model_for(model_path)
    var = _variant(variant_name)
    evaluator = lambda current_board: evaluate_halfkp_board(current_board, model)
    if var.move_order == "principled":
        move_order_fn = _principled_move_order_score
    elif var.move_order == "basic":
        move_order_fn = _move_order_score
    else:
        move_order_fn = None
    result = search_best_move(
        board,
        max_depth=var.depth,
        evaluate=evaluator,
        move_order_fn=move_order_fn,
        quiescence_depth=var.qdepth,
        hasher=ZobristHasher(seed=20260601),
        time_budget_ms=var.time_budget_ms,
        enable_pvs=var.enable_pvs,
        enable_lmr=var.enable_lmr,
        enable_null_move=var.enable_null_move,
        enable_futility=var.enable_futility,
        lmr_min_move_index=var.lmr_min_move_index,
    )
    if result.best_move is None:
        return None
    best = opening_sanity_filter(
        board,
        result.best_move,
        score_move=lambda mv: int(move_order_fn(board, mv, 0) if move_order_fn else 0),
    )
    if var.opening_filter:
        best = _opening_principle_filter(board, best)
    if best is None:
        return None
    return _move_payload(best)


def _move_payload(move: chess.Move) -> dict:
    payload = {"from": chess.square_name(move.from_square), "to": chess.square_name(move.to_square)}
    if move.promotion is not None:
        payload["promotion"] = chess.piece_symbol(move.promotion).lower()
    return payload


def _payload_to_move(payload: dict | None, board: chess.Board) -> chess.Move | None:
    if not payload:
        return None
    promo = payload.get("promotion") or ""
    try:
        move = chess.Move.from_uci(f"{payload['from']}{payload['to']}{promo}")
    except Exception:
        return None
    return move if move in board.legal_moves else None


def _opponent_has_mate_in_one(board: chess.Board) -> bool:
    for reply in board.legal_moves:
        board.push(reply)
        try:
            if board.is_checkmate():
                return True
        finally:
            board.pop()
    return False


def deterministic_sanity(model_path: Path) -> dict:
    model = _model_for(model_path)
    rows = []
    ok = True
    for name, fen, _kind in TACTICAL_FENS + [(n, f, "blunder") for n, f in BLUNDER_FENS]:
        board = chess.Board(fen)
        a = evaluate_halfkp_board(board, model)
        b = evaluate_halfkp_board(board, model)
        after_ok = True
        if list(board.legal_moves):
            after = board.copy(stack=False)
            after.push(next(iter(board.legal_moves)))
            c = evaluate_halfkp_board(after, model)
            d = evaluate_halfkp_board(after, model)
            after_ok = c == d
        row_ok = (a == b) and after_ok
        ok = ok and row_ok
        rows.append({"name": name, "deterministic": row_ok, "eval": int(a)})
    return {"passed": bool(ok), "rows": rows}


def fixed_fen_suites(model_path: Path, variant_name: str) -> dict:
    tactical = []
    blunder = []
    passed = True
    for name, fen, kind in TACTICAL_FENS:
        board = chess.Board(fen)
        payload = choose_halfkp_move({FEN_KEY: fen}, "white" if board.turn == chess.WHITE else "black",
                                     model_path=model_path, variant_name=variant_name)
        move = _payload_to_move(payload, board)
        row = {"name": name, "legal": move is not None, "passed": False}
        if move is not None and kind == "mate_in_one":
            board.push(move)
            row["passed"] = bool(board.is_checkmate())
        elif move is not None:
            row["passed"] = True
        passed = passed and bool(row["passed"])
        tactical.append(row)
    for name, fen in BLUNDER_FENS:
        board = chess.Board(fen)
        payload = choose_halfkp_move({FEN_KEY: fen}, "white" if board.turn == chess.WHITE else "black",
                                     model_path=model_path, variant_name=variant_name)
        move = _payload_to_move(payload, board)
        row = {"name": name, "legal": move is not None, "permits_opponent_mate_in_one": None, "passed": False}
        if move is not None:
            board.push(move)
            mate_risk = _opponent_has_mate_in_one(board)
            row["permits_opponent_mate_in_one"] = bool(mate_risk)
            row["passed"] = not mate_risk
        passed = passed and bool(row["passed"])
        blunder.append(row)
    return {"passed": bool(passed), "tactical": tactical, "blunder": blunder}


def ablation_suite(model_path: Path) -> dict:
    rows = []
    for label, fen in enumerate(ABLATION_FENS, start=1):
        board = chess.Board(fen)
        side = "white" if board.turn == chess.WHITE else "black"
        baseline_t0 = time.perf_counter()
        baseline_payload = choose_experiment_neural_move({FEN_KEY: fen}, side, weights_path=DEFAULT_CHAMPION, search_profile="balanced")
        baseline_ms = (time.perf_counter() - baseline_t0) * 1000.0
        rows.append({
            "case": label,
            "engine": "baseline_current_evaluator_current_search",
            "legal": _payload_to_move(baseline_payload, board) is not None,
            "ms": round(baseline_ms, 1),
        })
        for variant_name in SEARCH_VARIANTS:
            t0 = time.perf_counter()
            payload = choose_halfkp_move({FEN_KEY: fen}, side, model_path=model_path, variant_name=variant_name)
            elapsed = (time.perf_counter() - t0) * 1000.0
            rows.append({
                "case": label,
                "engine": f"halfkp_{variant_name}",
                "legal": _payload_to_move(payload, board) is not None,
                "ms": round(elapsed, 1),
            })
    return {
        "passed": all(bool(row["legal"]) for row in rows),
        "rows": rows,
        "variants": list(SEARCH_VARIANTS),
    }


@contextlib.contextmanager
def patched_curriculum_selector(model_path: Path, variant_name: str) -> Iterator[None]:
    old = cc.choose_experiment_neural_move

    def _patched(board_state, side: str, *, search_profile: str = "balanced"):
        return choose_halfkp_move(board_state, side, model_path=model_path, variant_name=variant_name)

    cc.choose_experiment_neural_move = _patched
    try:
        yield
    finally:
        cc.choose_experiment_neural_move = old


def _play_candidate_game(model_path: Path, variant_name: str, opening_id: str, opening_moves: list[str],
                         exp6_color_name: str, stockfish_depth: int, engine: UciStockfish) -> dict:
    board = chess.Board()
    for uci in opening_moves:
        board.push_uci(uci)
    exp6_color = chess.WHITE if exp6_color_name == "white" else chess.BLACK
    invalid_actor = None
    exp6_times: list[float] = []
    wall0 = time.perf_counter()
    for _ply in range(400):
        if board.is_game_over(claim_draw=True):
            break
        if board.turn == exp6_color:
            side = "white" if board.turn == chess.WHITE else "black"
            state = {chess.square_name(sq): p.symbol() for sq, p in board.piece_map().items()}
            state[FEN_KEY] = board.fen()
            t0 = time.perf_counter()
            payload = choose_halfkp_move(state, side, model_path=model_path, variant_name=variant_name)
            exp6_times.append(time.perf_counter() - t0)
            move = _payload_to_move(payload, board)
            if move is None:
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
    return {
        "opening_id": opening_id,
        "stockfish_depth": int(stockfish_depth),
        "exp6_color": exp6_color_name,
        "result": result,
        "reason": reason,
        "plies": len(board.move_stack),
        "score_points": int(score),
        "elapsed_wall_s": round(time.perf_counter() - wall0, 3),
        "exp6_mean_ms": round((sum(exp6_times) / len(exp6_times) * 1000.0) if exp6_times else 0.0, 1),
        "exp6_max_ms": round((max(exp6_times) * 1000.0) if exp6_times else 0.0, 1),
    }


def staged_prefix_gate(model_path: Path, variant_name: str, *, games_limit: int = 4) -> dict:
    sf_path = resolve_stockfish_path()
    if not sf_path:
        raise SystemExit("Stockfish not found")
    schedule = []
    for depth in cc.STAGED_DEPTHS:
        for k in range(cc.STAGED_GAMES_PER_DEPTH):
            opening_id, opening_moves = cc.STAGED_OPENINGS[(depth + k - 1) % len(cc.STAGED_OPENINGS)]
            exp6_color = "white" if k % 2 == 0 else "black"
            schedule.append((opening_id, opening_moves, exp6_color, depth))
    rows = []
    with UciStockfish(sf_path) as engine:
        for idx, (opening_id, opening_moves, exp6_color, depth) in enumerate(schedule[:games_limit], start=1):
            row = _play_candidate_game(model_path, variant_name, opening_id, opening_moves, exp6_color, depth, engine)
            rows.append(row)
            print(
                f"    halfkp_prefix g{idx:02d} d{depth} {opening_id}/exp6={exp6_color}: "
                f"{row['result']:>14s} ({row['reason']}) {row['plies']:3d}p "
                f"mean={row['exp6_mean_ms']:.0f}ms max={row['exp6_max_ms']:.0f}ms score={row['score_points']:+d}",
                flush=True,
            )
    return summarize_staged(rows)


def summarize_staged(rows: list[dict]) -> dict:
    wins = sum(1 for item in rows if item["result"] == "exp6_win")
    draws = sum(1 for item in rows if item["result"] == "draw")
    losses = sum(1 for item in rows if item["result"] == "stockfish_win")
    total = sum(int(item["score_points"]) for item in rows)
    return {
        "W": wins,
        "D": draws,
        "L": losses,
        "score_total": total,
        "score_max": len(rows) * cc.SCORE_WIN,
        "games": len(rows),
        "games_redacted": rows,
    }


def official_full_gate(model_path: Path, variant_name: str) -> dict:
    with patched_curriculum_selector(model_path, variant_name):
        rows = cc.play_staged_test(model_path)
    summary = cc.score_summary(rows)
    return {"summary": summary, "games_redacted": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--train-limit", type=int, default=1600)
    parser.add_argument("--dev-size", type=int, default=320)
    parser.add_argument("--max-fullmove", type=int, default=40)
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--hidden", type=int, default=64)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--variant", choices=tuple(SEARCH_VARIANTS), default="same_search")
    parser.add_argument("--skip-train", action="store_true")
    parser.add_argument("--run-full-gate", action="store_true")
    args = parser.parse_args()

    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.set_num_threads(2)

    before_md5 = champion_md5()
    if before_md5 != EXPECTED_CHAMPION_MD5:
        raise SystemExit(f"champion md5 mismatch before run: {before_md5}")

    report: dict = {
        "candidate": "exp6_halfkp_v1",
        "commit_hash": current_commit_hash(),
        "champion_md5_before": before_md5,
        "changed_champion": False,
        "gate_function": "chess_exp6_curriculum.play_staged_test",
        "model_path": str(args.model_out),
        "variant": str(args.variant),
    }

    if not args.skip_train:
        train_rows, dev_rows = load_dataset(
            args.labels,
            train_limit=max(1, int(args.train_limit)),
            dev_size=max(1, int(args.dev_size)),
            max_fullmove=max(1, int(args.max_fullmove)),
        )
        model, history, dev = train_model(
            train_rows,
            dev_rows,
            hidden=max(8, int(args.hidden)),
            epochs=max(1, int(args.epochs)),
            batch_size=max(16, int(args.batch_size)),
            lr=float(args.lr),
        )
        save_model(
            args.model_out,
            model,
            config={
                "hidden": int(args.hidden),
                "train_limit": int(args.train_limit),
                "dev_size": int(args.dev_size),
                "max_fullmove": int(args.max_fullmove),
                "value_target": "tanh(clipped_stockfish_cp_white/600) + cp residual sanity",
                "policy_head": False,
            },
        )
        report["training"] = {"history": history, "final_dev": dev}
    elif not args.model_out.exists():
        raise SystemExit(f"missing model: {args.model_out}")

    det = deterministic_sanity(args.model_out)
    suites = fixed_fen_suites(args.model_out, args.variant)
    ablation = ablation_suite(args.model_out)
    report["deterministic_sanity"] = det
    report["fixed_fen_suites"] = suites
    report["search_ablation_fixed_fens"] = ablation

    stop_reason = ""
    if not det["passed"]:
        stop_reason = "deterministic sanity failed"
    elif not suites["passed"]:
        stop_reason = "fixed FEN tactical/blunder suite failed"
    elif not ablation["passed"]:
        stop_reason = "fixed FEN ablation legality failed"

    if not stop_reason:
        early = staged_prefix_gate(args.model_out, args.variant, games_limit=4)
        report["staged_4_prefix"] = early
        if early["score_total"] < -4:
            stop_reason = f"4-game prefix below stop line: {early['score_total']}/16"
        if early["L"] == 4:
            stop_reason = "4-game 0W/0D/4L early defence break"

    if not stop_reason and args.run_full_gate:
        full = official_full_gate(args.model_out, args.variant)
        report["staged_10_full_gate"] = full
        score = int(full["summary"]["total_score"])
        if score < -19:
            stop_reason = f"full gate below current champion: {score}/40"
    elif not args.run_full_gate:
        report["staged_10_full_gate"] = {"skipped": "run_full_gate not requested or early branch under evaluation"}

    after_md5 = champion_md5()
    report["champion_md5_after"] = after_md5
    report["champion_permissions_after"] = oct(DEFAULT_CHAMPION.stat().st_mode & 0o777)[2:]
    if after_md5 != before_md5:
        stop_reason = f"champion md5 changed: {before_md5} -> {after_md5}"
        report["changed_champion"] = True
    report["pass"] = not bool(stop_reason)
    report["fail_reason"] = stop_reason
    report["next_step"] = (
        "Stop this branch and inspect evaluator/search architecture."
        if stop_reason
        else "Candidate survived requested gates; run broader match-gate before any promotion discussion."
    )

    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, indent=2))
    print(f"report -> {args.report_json}", flush=True)
    if stop_reason:
        print(f"FAIL: {stop_reason}", flush=True)
    else:
        print("PASS: requested gates survived; no promotion performed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
