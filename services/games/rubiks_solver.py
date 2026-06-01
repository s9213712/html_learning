"""Rubik's cube solver integration."""

from __future__ import annotations

from collections import Counter

SOLVED_FACELETS = "UUUUUUUUURRRRRRRRRFFFFFFFFFDDDDDDDDDLLLLLLLLLBBBBBBBBB"
FACELET_ORDER = "URFDLB"
MAX_KOCIEMBA_DEPTH = 24


class RubiksSolverUnavailable(RuntimeError):
    """Raised when the optional kociemba dependency is not installed."""


def _kociemba():
    try:
        import kociemba  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RubiksSolverUnavailable("魔術方塊 solver 尚未安裝") from exc
    return kociemba


def validate_facelets(facelets: str) -> str:
    value = str(facelets or "").strip().upper()
    if len(value) != 54:
        raise ValueError("魔術方塊狀態必須是 54 格 facelet")
    invalid = sorted(set(value) - set(FACELET_ORDER))
    if invalid:
        raise ValueError(f"魔術方塊狀態含有不支援的顏色：{''.join(invalid)}")
    counts = Counter(value)
    for face in FACELET_ORDER:
        if counts[face] != 9:
            raise ValueError(f"{face} 面顏色數量必須是 9")
    for index, face in zip((4, 13, 22, 31, 40, 49), FACELET_ORDER):
        if value[index] != face:
            raise ValueError("中心色位置不符合 URFDLB solver 格式")
    return value


def expand_solver_moves(moves: list[str]) -> list[str]:
    expanded: list[str] = []
    for move in moves:
        token = str(move or "").strip()
        if not token or token == ".":
            continue
        if token.endswith("2") and len(token) == 2:
            expanded.extend([token[0], token[0]])
        else:
            expanded.append(token)
    return expanded


def solve_facelets(facelets: str, max_depth: int = MAX_KOCIEMBA_DEPTH) -> dict:
    normalized = validate_facelets(facelets)
    bounded_depth = max(1, min(int(max_depth or MAX_KOCIEMBA_DEPTH), MAX_KOCIEMBA_DEPTH))
    if normalized == SOLVED_FACELETS:
        return {
            "solver": "kociemba",
            "facelets": normalized,
            "solution": [],
            "expanded_solution": [],
            "length": 0,
            "quarter_turn_length": 0,
            "next_move": "",
            "solved": True,
        }

    raw_solution = str(_kociemba().solve(normalized, max_depth=bounded_depth) or "").strip()
    moves = [part for part in raw_solution.replace(".", " ").split() if part]
    expanded = expand_solver_moves(moves)
    return {
        "solver": "kociemba",
        "facelets": normalized,
        "solution": moves,
        "expanded_solution": expanded,
        "length": len(moves),
        "quarter_turn_length": len(expanded),
        "next_move": expanded[0] if expanded else "",
        "solved": False,
    }
