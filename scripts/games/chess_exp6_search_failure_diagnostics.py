#!/usr/bin/env python3
"""Exp6 HalfKP v1 failure taxonomy and current-search ablation.

This diagnostic intentionally redacts exact staged FENs and moves in all
persisted outputs. It reconstructs positions in memory, queries Stockfish for
classification, then writes only stable digests and aggregate categories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.games.chess import FEN_KEY  # noqa: E402
from services.games.chess_exp6 import (  # noqa: E402
    _SEARCH_PROFILES,
    _move_order_score,
    _opening_principle_filter,
    _principled_move_order_score,
)
from services.games.chess_neural import NeuralEvaluator, load_weights  # noqa: E402
from services.games.chess_search import ZobristHasher, opening_sanity_filter, search_best_move  # noqa: E402
from services.games.chess_stockfish_teacher import UciStockfish, analysis_limit, resolve_stockfish_path  # noqa: E402
from scripts.games.common_paths import exp6_artifacts_root, exp6_private_dir  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts/games"))
import chess_exp6_curriculum as cc  # noqa: E402
from chess_exp6_halfkp_value_candidate import (  # noqa: E402
    DEFAULT_CHAMPION,
    EXPECTED_CHAMPION_MD5,
    _payload_to_move,
    champion_md5,
    choose_halfkp_move,
)


DEFAULT_MODEL = Path("/tmp/exp6_halfkp_v1_600.pt")
DEFAULT_JSONL = exp6_private_dir() / "halfkp_v1_failure_taxonomy.jsonl"
DEFAULT_REPORT_DIR = exp6_artifacts_root() / "search_failure_diagnostics"
DEFAULT_TAXONOMY_MD = DEFAULT_REPORT_DIR / "halfkp_v1_failure_taxonomy.md"
DEFAULT_SEARCH_TAXONOMY_MD = DEFAULT_REPORT_DIR / "search_failure_taxonomy.md"
DEFAULT_ABLATION_MD = DEFAULT_REPORT_DIR / "search_ablation_report.md"
DEFAULT_NEXT_PLAN_MD = DEFAULT_REPORT_DIR / "next_evaluator_candidate_plan.md"
DEFAULT_ABLATION_JSON = exp6_private_dir() / "search_failure_ablation.json"

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


@dataclass
class SearchPatch:
    name: str
    depth: int
    qdepth: int
    move_order: str = "principled"
    qmode: str = "captures"
    extension: str = "none"
    enable_pvs: bool = True
    enable_lmr: bool = True
    enable_null_move: bool = False
    enable_futility: bool = True
    time_budget_ms: int | None = None


PATCHES: list[SearchPatch] = [
    SearchPatch("current_eval_current_search", 2, 2, time_budget_ms=600),
    SearchPatch("current_eval_deeper_search_d3", 3, 3, time_budget_ms=1800),
    SearchPatch("current_eval_q_off", 2, 0, time_budget_ms=600),
    SearchPatch("current_eval_q_checks_promos", 2, 2, qmode="checks_promos", time_budget_ms=700),
    SearchPatch("current_eval_capture_check_promo_ext", 2, 2, extension="capture_check_promo", time_budget_ms=900),
    SearchPatch("current_eval_see_qfilter", 2, 2, qmode="see", time_budget_ms=700),
    SearchPatch("current_eval_king_danger_ext", 2, 2, extension="king_danger", time_budget_ms=900),
]


def _digest(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def _position_id(board: chess.Board) -> str:
    return _digest(board.fen(), 16)


def _move_id(board: chess.Board, move: chess.Move | None) -> str:
    if move is None:
        return ""
    return _digest(board.fen() + "|" + move.uci(), 12)


def _material_margin(board: chess.Board, color: chess.Color) -> int:
    total = 0
    for piece in board.piece_map().values():
        val = PIECE_VALUES[piece.piece_type]
        total += val if piece.color == color else -val
    return total


def _after(board: chess.Board, move: chess.Move) -> chess.Board:
    b = board.copy(stack=True)
    b.push(move)
    return b


def _opponent_has_mate_in_one(board: chess.Board) -> bool:
    for reply in board.legal_moves:
        board.push(reply)
        try:
            if board.is_checkmate():
                return True
        finally:
            board.pop()
    return False


def _move_category(board: chess.Board, move: chess.Move | None) -> str:
    if move is None:
        return "none"
    if board.is_capture(move):
        return "capture"
    if board.gives_check(move):
        return "check"
    if move.promotion:
        return "promotion"
    if board.is_castling(move):
        return "castle"
    return "quiet"


def _phase(board: chess.Board) -> str:
    pieces = len(board.piece_map())
    if board.fullmove_number <= 12:
        return "opening"
    if pieces <= 12:
        return "endgame"
    return "middlegame"


def _king_ring(color: chess.Color, square: int) -> set[int]:
    return set(chess.SquareSet(chess.BB_KING_ATTACKS[square])) | {square}


def _king_exposure_delta(board: chess.Board, chosen: chess.Move, best: chess.Move) -> int:
    color = board.turn
    def attacked_count(after: chess.Board) -> int:
        king = after.king(color)
        if king is None:
            return 8
        return sum(1 for sq in _king_ring(color, king) if after.is_attacked_by(not color, sq))
    return attacked_count(_after(board, chosen)) - attacked_count(_after(board, best))


def classify_position(board: chess.Board, chosen: chess.Move, best: chess.Move, delta_cp: float) -> list[str]:
    tags: list[str] = []
    after_chosen = _after(board, chosen)
    after_best = _after(board, best)
    if after_chosen.is_checkmate():
        tags.append("self_mate_terminal")
    if _opponent_has_mate_in_one(after_chosen):
        tags.append("tactical_miss")
    mat_delta = _material_margin(after_best, board.turn) - _material_margin(after_chosen, board.turn)
    if mat_delta >= 250:
        tags.append("hanging_piece")
    if board.is_capture(chosen) and mat_delta >= 150:
        tags.append("bad_exchange")
    if _king_exposure_delta(board, chosen, best) >= 2:
        tags.append("king_safety")
    if _phase(board) == "endgame" and abs(delta_cp) >= 120:
        tags.append("endgame_conversion")
    if int(delta_cp) >= 250 and not tags:
        tags.append("horizon_effect")
    if not tags:
        tags.append("positional_or_small_delta")
    return tags


def stockfish_top_and_eval(engine: UciStockfish, board: chess.Board, chosen: chess.Move, *, depth: int) -> tuple[list[dict], float, float]:
    rows = engine.analyse(board, limit=analysis_limit(depth=depth, movetime_ms=0), multipv=3)
    if not rows:
        return [], 0.0, 0.0
    best_eval = float(rows[0].get("teacher_eval_cp") or 0.0)
    chosen_eval = None
    if chosen.uci() in {str(row.get("move")) for row in rows}:
        for row in rows:
            if str(row.get("move")) == chosen.uci():
                chosen_eval = float(row.get("teacher_eval_cp") or 0.0)
                break
    if chosen_eval is None:
        chosen_rows = engine.analyse(
            board,
            limit=analysis_limit(depth=depth, movetime_ms=0),
            multipv=1,
            root_moves=[chosen],
        )
        chosen_eval = float(chosen_rows[0].get("teacher_eval_cp") or 0.0) if chosen_rows else -9999.0
    return rows, best_eval, chosen_eval


def replay_halfkp_prefix(model_path: Path, *, sf_depth: int, include_all: bool) -> tuple[list[dict], list[chess.Board]]:
    sf_path = resolve_stockfish_path()
    if not sf_path:
        raise SystemExit("Stockfish not found")
    schedule = []
    for depth in cc.STAGED_DEPTHS:
        for k in range(cc.STAGED_GAMES_PER_DEPTH):
            opening_id, opening_moves = cc.STAGED_OPENINGS[(depth + k - 1) % len(cc.STAGED_OPENINGS)]
            exp6_color = "white" if k % 2 == 0 else "black"
            schedule.append((opening_id, opening_moves, exp6_color, depth))
    taxonomy: list[dict] = []
    key_boards: list[chess.Board] = []
    with UciStockfish(sf_path) as engine:
        for game_idx, (opening_id, opening_moves, exp6_color_name, depth) in enumerate(schedule[:4], start=1):
            board = chess.Board()
            for uci in opening_moves:
                board.push_uci(uci)
            exp6_color = chess.WHITE if exp6_color_name == "white" else chess.BLACK
            result = "incomplete"
            reason = "max_plies"
            for ply in range(400):
                if board.is_game_over(claim_draw=True):
                    outcome = board.outcome(claim_draw=True)
                    if outcome is None:
                        break
                    if outcome.winner is None:
                        result = "draw"
                        reason = outcome.termination.name.lower()
                    else:
                        result = "exp6_win" if outcome.winner == exp6_color else "stockfish_win"
                        reason = outcome.termination.name.lower()
                    break
                if board.turn == exp6_color:
                    side = "white" if board.turn == chess.WHITE else "black"
                    state = {chess.square_name(sq): p.symbol() for sq, p in board.piece_map().items()}
                    state[FEN_KEY] = board.fen()
                    payload = choose_halfkp_move(state, side, model_path=model_path, variant_name="same_search")
                    chosen = _payload_to_move(payload, board)
                    if chosen is None:
                        result = "stockfish_win"
                        reason = "invalid_move"
                        break
                    top3, best_eval, chosen_eval = stockfish_top_and_eval(engine, board, chosen, depth=sf_depth)
                    best_move = chess.Move.from_uci(str(top3[0]["move"])) if top3 else chosen
                    delta = float(best_eval - chosen_eval)
                    is_key = include_all or delta >= 100.0 or chosen.uci() not in {str(row.get("move")) for row in top3}
                    if is_key:
                        record = {
                            "schema": "redacted_exp6_failure_taxonomy_v1",
                            "candidate": "exp6_halfkp_v1",
                            "position_id": _position_id(board),
                            "game_id": f"g{game_idx:02d}",
                            "opening_id": opening_id,
                            "stockfish_depth_game": int(depth),
                            "exp6_color": exp6_color_name,
                            "ply": int(ply),
                            "fullmove": int(board.fullmove_number),
                            "phase": _phase(board),
                            "side_to_move": "white" if board.turn == chess.WHITE else "black",
                            "engine_chosen_move_id": _move_id(board, chosen),
                            "engine_move_category": _move_category(board, chosen),
                            "stockfish_top3_move_ids": [_move_id(board, chess.Move.from_uci(str(row.get("move")))) for row in top3],
                            "stockfish_top3_eval_cp": [int(float(row.get("teacher_eval_cp") or 0.0)) for row in top3],
                            "chosen_eval_cp": int(chosen_eval),
                            "best_eval_cp": int(best_eval),
                            "eval_delta_cp": int(delta),
                            "chosen_in_stockfish_top3": chosen.uci() in {str(row.get("move")) for row in top3},
                            "taxonomy": classify_position(board, chosen, best_move, delta),
                            "fen_redacted": True,
                            "move_uci_redacted": True,
                        }
                        taxonomy.append(record)
                        key_boards.append(board.copy(stack=True))
                    board.push(chosen)
                else:
                    rows = engine.analyse(board, limit=analysis_limit(depth=depth, movetime_ms=0), multipv=1)
                    if not rows:
                        result = "exp6_win"
                        reason = "stockfish_invalid"
                        break
                    move = chess.Move.from_uci(str(rows[0]["move"]))
                    if move not in board.legal_moves:
                        result = "exp6_win"
                        reason = "stockfish_invalid"
                        break
                    board.push(move)
            for rec in taxonomy:
                if rec["game_id"] == f"g{game_idx:02d}" and "game_result" not in rec:
                    rec["game_result"] = result
                    rec["game_reason"] = reason
    return taxonomy, key_boards


def _q_checks_promos(board: chess.Board, move: chess.Move) -> bool:
    return bool(board.is_capture(move) or move.promotion or board.gives_check(move))


def _capture_value_delta(board: chess.Board, move: chess.Move) -> int:
    attacker = board.piece_at(move.from_square)
    captured = board.piece_at(move.to_square)
    if captured is None and board.is_en_passant(move):
        captured = chess.Piece(chess.PAWN, not board.turn)
    if captured is None:
        return 0
    return PIECE_VALUES[captured.piece_type] - (PIECE_VALUES[attacker.piece_type] if attacker else 100)


def _q_see_filter(board: chess.Board, move: chess.Move) -> bool:
    if move.promotion or board.gives_check(move):
        return True
    if not board.is_capture(move):
        return False
    return _capture_value_delta(board, move) >= -120


def _extension_for(kind: str):
    if kind == "none":
        return None, 0
    if kind == "capture_check_promo":
        def _ext(board: chess.Board, move: chess.Move, _ply: int, _depth: int) -> int:
            return 1 if (board.is_capture(move) or board.gives_check(move) or move.promotion) else 0
        return _ext, 1
    if kind == "king_danger":
        def _ext(board: chess.Board, move: chess.Move, _ply: int, _depth: int) -> int:
            if board.gives_check(move):
                return 1
            try:
                after = _after(board, move)
            except Exception:
                return 0
            king = after.king(board.turn)
            if king is None:
                return 0
            attacked = sum(1 for sq in _king_ring(board.turn, king) if after.is_attacked_by(not board.turn, sq))
            return 1 if attacked >= 3 else 0
        return _ext, 1
    return None, 0


def choose_current_search_variant(board: chess.Board, patch: SearchPatch) -> chess.Move | None:
    weights = load_weights(DEFAULT_CHAMPION)
    evaluator = NeuralEvaluator(weights)
    if patch.move_order == "principled":
        move_order_fn = _principled_move_order_score
    elif patch.move_order == "basic":
        move_order_fn = _move_order_score
    else:
        move_order_fn = None
    if patch.qmode == "checks_promos":
        qfilter = _q_checks_promos
    elif patch.qmode == "see":
        qfilter = _q_see_filter
    else:
        qfilter = None
    extension_fn, max_extensions = _extension_for(patch.extension)
    result = search_best_move(
        board,
        max_depth=patch.depth,
        evaluate=evaluator,
        move_order_fn=move_order_fn,
        qmove_filter=qfilter,
        extension_fn=extension_fn,
        max_extensions=max_extensions,
        quiescence_depth=patch.qdepth,
        hasher=ZobristHasher(seed=20260601),
        time_budget_ms=patch.time_budget_ms,
        enable_pvs=patch.enable_pvs,
        enable_lmr=patch.enable_lmr,
        enable_null_move=patch.enable_null_move,
        enable_futility=patch.enable_futility,
    )
    if result.best_move is None:
        return None
    best = opening_sanity_filter(
        board,
        result.best_move,
        score_move=lambda mv: int(move_order_fn(board, mv, 0) if move_order_fn else 0),
    )
    if patch.move_order == "principled":
        best = _opening_principle_filter(board, best)
    return best


def run_search_ablation(boards: list[chess.Board], *, sf_depth: int) -> dict:
    sf_path = resolve_stockfish_path()
    if not sf_path:
        raise SystemExit("Stockfish not found")
    rows: list[dict] = []
    with UciStockfish(sf_path) as engine:
        for board in boards:
            top3 = engine.analyse(board, limit=analysis_limit(depth=sf_depth, movetime_ms=0), multipv=3)
            if not top3:
                continue
            best_eval = float(top3[0].get("teacher_eval_cp") or 0.0)
            top3_uci = {str(row.get("move")) for row in top3}
            for patch in PATCHES:
                t0 = time.perf_counter()
                move = choose_current_search_variant(board, patch)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if move is None:
                    chosen_eval = -9999.0
                    delta = 9999.0
                    in_top3 = False
                    tags = ["illegal_or_no_move"]
                else:
                    _, _best_eval_unused, chosen_eval = stockfish_top_and_eval(engine, board, move, depth=sf_depth)
                    delta = float(best_eval - chosen_eval)
                    in_top3 = move.uci() in top3_uci
                    best_move = chess.Move.from_uci(str(top3[0]["move"]))
                    tags = classify_position(board, move, best_move, delta)
                rows.append({
                    "position_id": _position_id(board),
                    "patch": patch.name,
                    "chosen_move_id": _move_id(board, move),
                    "chosen_in_stockfish_top3": bool(in_top3),
                    "eval_delta_cp": int(delta),
                    "elapsed_ms": round(elapsed_ms, 1),
                    "taxonomy": tags,
                    "fen_redacted": True,
                    "move_uci_redacted": True,
                })
    by_patch: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["patch"]].append(row)
    for name, items in grouped.items():
        by_patch[name] = {
            "n": len(items),
            "top3_rate": round(sum(1 for item in items if item["chosen_in_stockfish_top3"]) / max(1, len(items)), 4),
            "mean_eval_delta_cp": round(sum(int(item["eval_delta_cp"]) for item in items) / max(1, len(items)), 2),
            "max_eval_delta_cp": max(int(item["eval_delta_cp"]) for item in items),
            "mean_elapsed_ms": round(sum(float(item["elapsed_ms"]) for item in items) / max(1, len(items)), 1),
        }
    return {"rows": rows, "summary_by_patch": by_patch}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def write_taxonomy_docs(taxonomy: list[dict], ablation: dict, *, taxonomy_md: Path, search_taxonomy_md: Path, ablation_md: Path, next_plan_md: Path, jsonl_path: Path, ablation_json: Path) -> None:
    counts = Counter(tag for row in taxonomy for tag in row["taxonomy"])
    by_game = Counter(row["game_id"] for row in taxonomy)
    taxonomy_md.parent.mkdir(parents=True, exist_ok=True)
    taxonomy_md.write_text(
        "# HalfKP v1 Failure Taxonomy\n\n"
        "Exact FENs and moves are redacted to avoid leaking staged content. "
        "Use `position_id` only to correlate private diagnostic rows.\n\n"
        f"- JSONL: `{jsonl_path}`\n"
        f"- Analysed positions: `{len(taxonomy)}`\n"
        f"- Games covered: `{dict(sorted(by_game.items()))}`\n\n"
        "## Category Counts\n\n"
        + "\n".join(f"- `{k}`: {v}" for k, v in counts.most_common())
        + "\n\n## Largest Deltas\n\n"
        + "\n".join(
            f"- `{row['position_id']}` {row['game_id']} ply {row['ply']}: "
            f"delta `{row['eval_delta_cp']}cp`, tags `{','.join(row['taxonomy'])}`"
            for row in sorted(taxonomy, key=lambda r: int(r["eval_delta_cp"]), reverse=True)[:12]
        )
        + "\n",
    )

    search_counts = Counter()
    for row in taxonomy:
        for tag in row["taxonomy"]:
            search_counts[tag] += 1
    search_taxonomy_md.write_text(
        "# Exp6 Search Failure Taxonomy\n\n"
        "This taxonomy is based on HalfKP v1 staged-prefix decision points, "
        "with exact FENs and moves redacted.\n\n"
        "Dominant failure modes:\n\n"
        + "\n".join(f"- `{k}`: {v}" for k, v in search_counts.most_common())
        + "\n\nInterpretation: these are consequence-evaluation failures, not policy top-N failures.\n",
    )

    ablation_json.parent.mkdir(parents=True, exist_ok=True)
    ablation_json.write_text(json.dumps(ablation, indent=2, sort_keys=True))
    summary = ablation["summary_by_patch"]
    baseline = summary.get("current_eval_current_search", {})
    lines = [
        "# Exp6 Search Ablation Report",
        "",
        "Exact FENs and moves are redacted. Ablations use the current locked evaluator on the same redacted failure-position set.",
        "",
        f"- JSON report: `{ablation_json}`",
        f"- Positions: `{baseline.get('n', 0)}`",
        "",
        "| Patch | Top3 Rate | Mean Delta CP | Max Delta CP | Mean ms |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, row in sorted(summary.items()):
        lines.append(
            f"| `{name}` | {row['top3_rate']:.4f} | {row['mean_eval_delta_cp']:.2f} | "
            f"{row['max_eval_delta_cp']} | {row['mean_elapsed_ms']:.1f} |"
        )
    best = min(summary.items(), key=lambda item: (item[1]["mean_eval_delta_cp"], item[1]["max_eval_delta_cp"])) if summary else None
    lines.extend([
        "",
        "## Result",
        "",
        (
            f"Best diagnostic patch by mean delta: `{best[0]}`. "
            "This is diagnostic only; no runtime patch was promoted."
            if best else "No ablation rows were produced."
        ),
        "",
        "A search patch must still pass fixed-FEN sanity and staged early gate before any full gate.",
        "",
    ])
    ablation_md.write_text("\n".join(lines))

    next_plan_md.write_text(
        "# Next Evaluator Candidate Plan\n\n"
        "Policy-rerank remains paused. The next candidate should improve value/search consequence judgement.\n\n"
        "Recommended order:\n\n"
        "1. Add a stronger private after-board regression suite using redacted position IDs, not exact staged FENs in docs.\n"
        "2. If a search-only patch is clearly best in ablation, isolate it as one minimal variant and run fixed-FEN sanity.\n"
        "3. Only if fixed-FEN sanity passes, run the staged 4-game early gate.\n"
        "4. If search-only ablations do not reduce failure deltas, do not train a new model from staged FENs. Instead train on generic curriculum/Stockfish-labelled positions with value-only targets.\n\n"
        "Allowed value-only additions:\n\n"
        "- mate-distance target\n"
        "- tactical swing target\n"
        "- SEE/material swing features\n"
        "- king exposure features\n"
        "- passed-pawn / promotion-race features\n\n"
        "Still forbidden:\n\n"
        "- policy target\n"
        "- root policy bonus\n"
        "- top-N rerank workaround\n"
        "- champion modification\n"
        "- storing exact staged FENs/moves in docs or handover\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--taxonomy-md", type=Path, default=DEFAULT_TAXONOMY_MD)
    parser.add_argument("--search-taxonomy-md", type=Path, default=DEFAULT_SEARCH_TAXONOMY_MD)
    parser.add_argument("--ablation-md", type=Path, default=DEFAULT_ABLATION_MD)
    parser.add_argument("--next-plan-md", type=Path, default=DEFAULT_NEXT_PLAN_MD)
    parser.add_argument("--ablation-json", type=Path, default=DEFAULT_ABLATION_JSON)
    parser.add_argument("--stockfish-depth", type=int, default=5)
    parser.add_argument("--include-all-decisions", action="store_true")
    args = parser.parse_args()

    before = champion_md5()
    if before != EXPECTED_CHAMPION_MD5:
        raise SystemExit(f"champion md5 mismatch before diagnostics: {before}")
    if not args.model.exists():
        raise SystemExit(f"missing HalfKP model: {args.model}")

    taxonomy, boards = replay_halfkp_prefix(
        args.model,
        sf_depth=max(1, int(args.stockfish_depth)),
        include_all=bool(args.include_all_decisions),
    )
    write_jsonl(args.jsonl, taxonomy)
    # Deduplicate boards by exact FEN in memory only.
    unique: dict[str, chess.Board] = {}
    for board in boards:
        unique.setdefault(board.fen(), board)
    ablation = run_search_ablation(list(unique.values()), sf_depth=max(1, int(args.stockfish_depth)))
    write_taxonomy_docs(
        taxonomy,
        ablation,
        taxonomy_md=args.taxonomy_md,
        search_taxonomy_md=args.search_taxonomy_md,
        ablation_md=args.ablation_md,
        next_plan_md=args.next_plan_md,
        jsonl_path=args.jsonl,
        ablation_json=args.ablation_json,
    )
    after = champion_md5()
    if after != before:
        raise SystemExit(f"champion md5 changed during diagnostics: {before} -> {after}")
    if (DEFAULT_CHAMPION.stat().st_mode & 0o777) != 0o444:
        raise SystemExit("champion permissions changed during diagnostics")

    print(f"taxonomy rows -> {args.jsonl} ({len(taxonomy)} rows)", flush=True)
    print(f"ablation rows -> {args.ablation_json} ({len(ablation['rows'])} rows)", flush=True)
    print(f"docs -> {args.taxonomy_md}, {args.search_taxonomy_md}, {args.ablation_md}, {args.next_plan_md}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
