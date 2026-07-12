import hashlib
import json
import math
import os
import random
import shlex
import shutil
import subprocess
from pathlib import Path

from services.server.runtime import default_runtime_root_path


ROOT = Path(__file__).resolve().parents[2]
BOARD_AI_GAME_KEYS = {"reversi", "go", "gomoku"}
GO_KATAGO_DIFFICULTY = "katago"
BOARD_AI_DIFFICULTIES = {"easy", "normal", "hard", GO_KATAGO_DIFFICULTY}
BOARD_AI_DIFFICULTIES_BY_GAME = {
    "reversi": {"easy", "normal", "hard"},
    "go": {"easy", "normal", "hard", GO_KATAGO_DIFFICULTY},
    "gomoku": {"easy", "normal", "hard"},
}
BOARD_AI_SIZES = {
    "reversi": 8,
    "go": 19,
    "gomoku": 15,
}
COLORS = {"black", "white"}
EMPTY = ""
GO_KOMI = 6.5
GO_LIFE_DEATH_WEIGHTS = {
    "network_delta": 0.9,
    "atari_save": 980,
    "atari_attack": 1120,
    "two_liberty_attack": 620,
    "two_liberty_save": 340,
    "eye_fill_penalty": 430,
}
GO_STAR_POINTS = (60, 66, 72, 174, 180, 186, 288, 294, 300)
GO_GTP_COLUMNS = "ABCDEFGHJKLMNOPQRST"
KATAGO_SETUP_COMMAND = "python3 scripts/games/setup_katago.py"


class KataGoUnavailable(RuntimeError):
    pass


def opponent(color):
    return "white" if color == "black" else "black"


def choose_board_game_ai_move(game_key, board, turn, difficulty="normal"):
    game_key = str(game_key or "").strip().lower()
    if game_key not in BOARD_AI_GAME_KEYS:
        raise ValueError("不支援的棋類 AI")
    difficulty = str(difficulty or "normal").strip().lower()
    if difficulty not in BOARD_AI_DIFFICULTIES_BY_GAME[game_key]:
        raise ValueError("不支援的 AI 難度")
    turn = _normalize_color(turn)
    size = BOARD_AI_SIZES[game_key]
    board = _normalize_board(board, size)
    if game_key == "reversi":
        return _choose_reversi_move(board, turn, difficulty)
    if game_key == "gomoku":
        return _choose_gomoku_move(board, turn, difficulty)
    return _choose_go_move(board, turn, difficulty)


def board_ai_difficulties_for_game(game_key):
    return set(BOARD_AI_DIFFICULTIES_BY_GAME.get(str(game_key or "").strip().lower(), set()))


def _normalize_color(value):
    color = str(value or "").strip().lower()
    if color not in COLORS:
        raise ValueError("棋色格式錯誤")
    return color


def _normalize_board(board, size):
    if not isinstance(board, list) or len(board) != size * size:
        raise ValueError("棋盤格式錯誤")
    normalized = []
    for value in board:
        cell = "" if value is None else str(value).strip().lower()
        if cell not in {"", "black", "white"}:
            raise ValueError("棋盤內容格式錯誤")
        normalized.append(cell)
    return tuple(normalized)


def _idx(x, y, size):
    return y * size + x


def _xy(index, size):
    return index % size, index // size


def _in_bounds(x, y, size):
    return 0 <= x < size and 0 <= y < size


def _neighbors(index, size):
    x, y = _xy(index, size)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if _in_bounds(nx, ny, size):
            yield _idx(nx, ny, size)


def _diagonal_neighbors(index, size):
    x, y = _xy(index, size)
    for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
        nx, ny = x + dx, y + dy
        if _in_bounds(nx, ny, size):
            yield _idx(nx, ny, size)


def _decision(game_key, turn, difficulty, action, move=None, score=0, reason=""):
    payload = {
        "game_key": game_key,
        "turn": turn,
        "difficulty": difficulty,
        "action": action,
        "score": int(round(score)),
        "reason": reason,
    }
    if move is not None:
        size = BOARD_AI_SIZES[game_key]
        x, y = _xy(move, size)
        payload["move"] = {"index": int(move), "x": x, "y": y}
    return payload


def _go_vertex(index):
    size = BOARD_AI_SIZES["go"]
    x, y = _xy(index, size)
    return f"{GO_GTP_COLUMNS[x]}{size - y}"


def _go_index_from_vertex(vertex):
    value = str(vertex or "").strip().upper()
    if value in {"", "PASS", "RESIGN"}:
        return None
    column = value[0]
    if column not in GO_GTP_COLUMNS:
        raise ValueError("KataGo 回傳座標格式錯誤")
    row = int(value[1:])
    size = BOARD_AI_SIZES["go"]
    if row < 1 or row > size:
        raise ValueError("KataGo 回傳座標超出棋盤")
    return _idx(GO_GTP_COLUMNS.index(column), size - row, size)


def _katago_home():
    runtime_root = Path(os.getenv("HACKME_RUNTIME_DIR") or default_runtime_root_path())
    return Path(os.getenv("HACKME_KATAGO_HOME") or (runtime_root / "katago")).expanduser()


def _katago_default_binary(home):
    candidates = [home / "katago"]
    if home.exists():
        candidates.extend(sorted(home.rglob("katago")))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _katago_default_model(home):
    if not home.exists():
        return None
    models = [path for path in home.rglob("*.bin.gz") if path.is_file()]
    if not models:
        return None
    return max(models, key=lambda path: (path.stat().st_mtime, path.name))


def _katago_expand_path(value):
    return str(Path(value).expanduser()) if value else value


def _katago_command():
    command = os.getenv("HACKME_KATAGO_COMMAND") or os.getenv("KATAGO_COMMAND")
    if command:
        return shlex.split(command)

    home = _katago_home()
    default_binary = _katago_default_binary(home)
    default_config = home / "analysis.cfg"
    default_model = _katago_default_model(home)
    binary = (
        os.getenv("HACKME_KATAGO_BIN")
        or os.getenv("KATAGO_BIN")
        or (str(default_binary) if default_binary else "katago")
    )
    config = (
        os.getenv("HACKME_KATAGO_CONFIG")
        or os.getenv("KATAGO_CONFIG")
        or (str(default_config) if default_config.is_file() else "")
    )
    model = (
        os.getenv("HACKME_KATAGO_MODEL")
        or os.getenv("KATAGO_MODEL")
        or (str(default_model) if default_model else "")
    )

    binary = _katago_expand_path(binary)
    config = _katago_expand_path(config)
    model = _katago_expand_path(model)
    binary_path = binary if os.path.sep in binary else shutil.which(binary)
    if not binary_path:
        raise KataGoUnavailable(
            f"KataGo 尚未安裝；可執行 `{KATAGO_SETUP_COMMAND}` 自動下載與設定，"
            "或設定 HACKME_KATAGO_BIN / HACKME_KATAGO_CONFIG / HACKME_KATAGO_MODEL"
        )
    if not config or not model:
        raise KataGoUnavailable(
            f"KataGo 缺少 config 或模型；可執行 `{KATAGO_SETUP_COMMAND}` 建立 runtime/katago，"
            "或設定 HACKME_KATAGO_CONFIG 與 HACKME_KATAGO_MODEL"
        )
    return [binary_path, "analysis", "-config", config, "-model", model]


def _katago_timeout_seconds():
    try:
        return max(1.0, float(os.getenv("HACKME_KATAGO_TIMEOUT_SECONDS", "8")))
    except ValueError:
        return 8.0


def _katago_max_visits():
    try:
        return max(1, int(os.getenv("HACKME_KATAGO_MAX_VISITS", "64")))
    except ValueError:
        return 64


def _choose_go_katago_move(board, turn):
    command = _katago_command()
    query = {
        "id": f"hackme-go-{hashlib.sha256(('|'.join(board) + turn).encode('utf-8')).hexdigest()[:12]}",
        "rules": "chinese",
        "komi": GO_KOMI,
        "boardXSize": BOARD_AI_SIZES["go"],
        "boardYSize": BOARD_AI_SIZES["go"],
        "initialStones": [
            ["B" if value == "black" else "W", _go_vertex(index)]
            for index, value in enumerate(board)
            if value
        ],
        "analyzeTurns": ["B" if turn == "black" else "W"],
        "maxVisits": _katago_max_visits(),
    }
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(query, separators=(",", ":")) + "\n",
            text=True,
            capture_output=True,
            timeout=_katago_timeout_seconds(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise KataGoUnavailable("KataGo 可執行檔不存在") from exc
    except subprocess.TimeoutExpired as exc:
        raise KataGoUnavailable("KataGo 思考逾時") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        suffix = f"：{detail[-1]}" if detail else ""
        raise KataGoUnavailable(f"KataGo 執行失敗{suffix}")
    response = None
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue
    if not response:
        raise KataGoUnavailable("KataGo 沒有回傳分析結果")
    for info in response.get("moveInfos") or []:
        move_text = info.get("move")
        move_index = _go_index_from_vertex(move_text)
        if move_index is None:
            return _decision("go", turn, GO_KATAGO_DIFFICULTY, "pass", score=0, reason="katago-neural-network")
        if board[move_index]:
            continue
        next_board, _captured = go_apply_move(board, move_index, turn)
        if next_board is None:
            continue
        winrate = float(info.get("winrate") or 0)
        score_lead = float(info.get("scoreLead") or 0)
        score = winrate * 100000 + score_lead * 100
        return _decision("go", turn, GO_KATAGO_DIFFICULTY, "move", move_index, score, "katago-neural-network")
    raise KataGoUnavailable("KataGo 沒有提供合法著手")


REVERSI_DIRS = (
    (1, 0), (-1, 0), (0, 1), (0, -1),
    (1, 1), (1, -1), (-1, 1), (-1, -1),
)
REVERSI_POSITION_WEIGHTS = (
    120, -36, 24, 10, 10, 24, -36, 120,
    -36, -54, -8, -6, -6, -8, -54, -36,
    24, -8, 16, 6, 6, 16, -8, 24,
    10, -6, 6, 2, 2, 6, -6, 10,
    10, -6, 6, 2, 2, 6, -6, 10,
    24, -8, 16, 6, 6, 16, -8, 24,
    -36, -54, -8, -6, -6, -8, -54, -36,
    120, -36, 24, 10, 10, 24, -36, 120,
)
REVERSI_CORNERS = (0, 7, 56, 63)
REVERSI_CORNER_DANGER = {
    0: (1, 8, 9),
    7: (6, 14, 15),
    56: (48, 49, 57),
    63: (54, 55, 62),
}
REVERSI_EXACT_EMPTY_LIMIT = 12


def reversi_flips(board, index, color):
    size = BOARD_AI_SIZES["reversi"]
    if board[index]:
        return []
    other = opponent(color)
    x, y = _xy(index, size)
    flips = []
    for dx, dy in REVERSI_DIRS:
        line = []
        nx, ny = x + dx, y + dy
        while _in_bounds(nx, ny, size) and board[_idx(nx, ny, size)] == other:
            line.append(_idx(nx, ny, size))
            nx += dx
            ny += dy
        if line and _in_bounds(nx, ny, size) and board[_idx(nx, ny, size)] == color:
            flips.extend(line)
    return flips


def reversi_legal_moves(board, color):
    return [index for index, value in enumerate(board) if not value and reversi_flips(board, index, color)]


def reversi_apply_move(board, index, color):
    flips = reversi_flips(board, index, color)
    if not flips:
        return None
    next_board = list(board)
    next_board[index] = color
    for flip_index in flips:
        next_board[flip_index] = color
    return tuple(next_board)


def _reversi_terminal(board):
    return not any(not cell for cell in board) or (
        not reversi_legal_moves(board, "black") and not reversi_legal_moves(board, "white")
    )


def _reversi_frontier_count(board, color):
    size = BOARD_AI_SIZES["reversi"]
    frontier = 0
    for index, value in enumerate(board):
        if value != color:
            continue
        x, y = _xy(index, size)
        for dx, dy in REVERSI_DIRS:
            nx, ny = x + dx, y + dy
            if _in_bounds(nx, ny, size) and not board[_idx(nx, ny, size)]:
                frontier += 1
                break
    return frontier


def _reversi_stable_edge_indices(board, color):
    size = BOARD_AI_SIZES["reversi"]
    stable = set()
    corner_edges = (
        (0, ((1, 0), (0, 1))),
        (7, ((-1, 0), (0, 1))),
        (56, ((1, 0), (0, -1))),
        (63, ((-1, 0), (0, -1))),
    )
    for corner, directions in corner_edges:
        if board[corner] != color:
            continue
        stable.add(corner)
        cx, cy = _xy(corner, size)
        for dx, dy in directions:
            nx, ny = cx + dx, cy + dy
            while _in_bounds(nx, ny, size) and board[_idx(nx, ny, size)] == color:
                stable.add(_idx(nx, ny, size))
                nx += dx
                ny += dy
    return stable


def _reversi_corner_danger_score(board, color):
    other = opponent(color)
    score = 0
    for corner, danger_squares in REVERSI_CORNER_DANGER.items():
        if board[corner] == color:
            score += sum(1 for index in danger_squares if board[index] == color) * 10
        elif board[corner] == other:
            score -= sum(1 for index in danger_squares if board[index] == other) * 10
        else:
            score -= sum(1 for index in danger_squares if board[index] == color) * 34
            score += sum(1 for index in danger_squares if board[index] == other) * 34
    return score


def _reversi_eval(board, color):
    other = opponent(color)
    empties = board.count(EMPTY)
    disc_diff = board.count(color) - board.count(other)
    mobility = len(reversi_legal_moves(board, color)) - len(reversi_legal_moves(board, other))
    corner_score = sum(1 for index in REVERSI_CORNERS if board[index] == color) - sum(1 for index in REVERSI_CORNERS if board[index] == other)
    edge_indices = set(range(8)) | set(range(56, 64)) | {i * 8 for i in range(8)} | {i * 8 + 7 for i in range(8)}
    edge_score = sum(1 for index in edge_indices if board[index] == color) - sum(1 for index in edge_indices if board[index] == other)
    stable_score = len(_reversi_stable_edge_indices(board, color)) - len(_reversi_stable_edge_indices(board, other))
    frontier_score = _reversi_frontier_count(board, other) - _reversi_frontier_count(board, color)
    positional_score = sum(
        REVERSI_POSITION_WEIGHTS[index] * (1 if value == color else -1 if value == other else 0)
        for index, value in enumerate(board)
    )
    corner_danger = _reversi_corner_danger_score(board, color)
    parity_score = (1 if empties % 2 == 1 else -1) if empties <= 16 else 0
    if _reversi_terminal(board):
        return disc_diff * 1000
    if empties > 40:
        disc_weight, mobility_weight, frontier_weight = 1, 30, 18
    elif empties > 16:
        disc_weight, mobility_weight, frontier_weight = 4, 24, 14
    else:
        disc_weight, mobility_weight, frontier_weight = 12, 10, 7
    return (
        disc_diff * disc_weight
        + mobility * mobility_weight
        + corner_score * 420
        + edge_score * 10
        + stable_score * 58
        + frontier_score * frontier_weight
        + positional_score
        + corner_danger
        + parity_score * 24
    )


def _reversi_exact_search(board, turn, root_color, alpha, beta, cache):
    key = ("exact", board, turn, root_color)
    if key in cache:
        return cache[key]
    if _reversi_terminal(board):
        value = (board.count(root_color) - board.count(opponent(root_color))) * 10000
        cache[key] = value
        return value
    moves = reversi_legal_moves(board, turn)
    if not moves:
        value = _reversi_exact_search(board, opponent(turn), root_color, alpha, beta, cache)
        cache[key] = value
        return value
    maximizing = turn == root_color
    cutoff = False
    if maximizing:
        value = -math.inf
        for move in _ordered_reversi_moves(board, moves, turn):
            value = max(value, _reversi_exact_search(reversi_apply_move(board, move, turn), opponent(turn), root_color, alpha, beta, cache))
            alpha = max(alpha, value)
            if alpha >= beta:
                cutoff = True
                break
    else:
        value = math.inf
        for move in _ordered_reversi_moves(board, moves, turn):
            value = min(value, _reversi_exact_search(reversi_apply_move(board, move, turn), opponent(turn), root_color, alpha, beta, cache))
            beta = min(beta, value)
            if alpha >= beta:
                cutoff = True
                break
    if not cutoff:
        cache[key] = value
    return value


def _reversi_search(board, turn, depth, root_color, alpha, beta, cache):
    key = ("search", board, turn, depth, root_color)
    if key in cache:
        return cache[key]
    if depth <= 0 or _reversi_terminal(board):
        value = _reversi_eval(board, root_color)
        cache[key] = value
        return value
    if board.count(EMPTY) <= 8:
        return _reversi_exact_search(board, turn, root_color, alpha, beta, cache)
    moves = reversi_legal_moves(board, turn)
    if not moves:
        value = _reversi_search(board, opponent(turn), depth - 1, root_color, alpha, beta, cache)
        cache[key] = value
        return value
    maximizing = turn == root_color
    cutoff = False
    if maximizing:
        value = -math.inf
        for move in _ordered_reversi_moves(board, moves, turn):
            value = max(value, _reversi_search(reversi_apply_move(board, move, turn), opponent(turn), depth - 1, root_color, alpha, beta, cache))
            alpha = max(alpha, value)
            if alpha >= beta:
                cutoff = True
                break
        if not cutoff:
            cache[key] = value
        return value
    value = math.inf
    for move in _ordered_reversi_moves(board, moves, turn):
        value = min(value, _reversi_search(reversi_apply_move(board, move, turn), opponent(turn), depth - 1, root_color, alpha, beta, cache))
        beta = min(beta, value)
        if alpha >= beta:
            cutoff = True
            break
    if not cutoff:
        cache[key] = value
    return value


def _reversi_move_order_score(board, move, color):
    next_board = reversi_apply_move(board, move, color)
    if next_board is None:
        return -math.inf
    other = opponent(color)
    score = len(reversi_flips(board, move, color)) * 5
    if move in REVERSI_CORNERS:
        score += 10000
    for corner, danger_squares in REVERSI_CORNER_DANGER.items():
        if move in danger_squares and not board[corner]:
            score -= 650
    score += _reversi_eval(next_board, color) * 0.08
    score -= len(reversi_legal_moves(next_board, other)) * 12
    return score


def _ordered_reversi_moves(board, moves, color):
    return sorted(moves, key=lambda move: (-_reversi_move_order_score(board, move, color), move))


def _choose_reversi_move(board, turn, difficulty):
    moves = reversi_legal_moves(board, turn)
    if not moves:
        action = "finish" if not reversi_legal_moves(board, opponent(turn)) else "pass"
        return _decision("reversi", turn, difficulty, action, reason="no-legal-move")
    depth = {"easy": 1, "normal": 3, "hard": 4}[difficulty]
    best_move = None
    best_score = -math.inf
    cache = {}
    exact_endgame = difficulty == "hard" and board.count(EMPTY) <= REVERSI_EXACT_EMPTY_LIMIT
    for move in _ordered_reversi_moves(board, moves, turn):
        next_board = reversi_apply_move(board, move, turn)
        if exact_endgame:
            score = _reversi_exact_search(next_board, opponent(turn), turn, -math.inf, math.inf, cache)
        else:
            score = _reversi_search(next_board, opponent(turn), depth - 1, turn, -math.inf, math.inf, cache)
        if score > best_score or (score == best_score and (best_move is None or move < best_move)):
            best_move = move
            best_score = score
    reason = "exact-endgame-alpha-beta" if exact_endgame else "stability-alpha-beta"
    return _decision("reversi", turn, difficulty, "move", best_move, best_score, reason)


GOMOKU_DIRS = ((1, 0), (0, 1), (1, 1), (1, -1))
GOMOKU_WIN_SCORE = 1_000_000
GOMOKU_OPEN_FOUR_SCORE = 820_000
GOMOKU_CLOSED_FOUR_SCORE = 270_000
GOMOKU_DOUBLE_THREE_SCORE = 150_000


def gomoku_has_five(board, index, color):
    size = BOARD_AI_SIZES["gomoku"]
    x, y = _xy(index, size)
    for dx, dy in GOMOKU_DIRS:
        total = 1
        for sign in (1, -1):
            nx, ny = x + dx * sign, y + dy * sign
            while _in_bounds(nx, ny, size) and board[_idx(nx, ny, size)] == color:
                total += 1
                nx += dx * sign
                ny += dy * sign
        if total >= 5:
            return True
    return False


def gomoku_candidate_moves(board, difficulty="normal"):
    size = BOARD_AI_SIZES["gomoku"]
    stones = [index for index, value in enumerate(board) if value]
    if not stones:
        return [_idx(size // 2, size // 2, size)]
    radius = 1 if difficulty == "easy" else 2
    candidates = set()
    for stone in stones:
        sx, sy = _xy(stone, size)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                nx, ny = sx + dx, sy + dy
                if _in_bounds(nx, ny, size):
                    index = _idx(nx, ny, size)
                    if not board[index]:
                        candidates.add(index)
    center = (size - 1) / 2
    return sorted(candidates, key=lambda index: (abs(_xy(index, size)[0] - center) + abs(_xy(index, size)[1] - center), index))


def _gomoku_apply_move(board, index, color):
    if board[index]:
        return None
    next_board = list(board)
    next_board[index] = color
    return tuple(next_board)


def _gomoku_window_score(count):
    return (0, 4, 24, 180, 1400, 100000)[count]


def _gomoku_eval(board, color):
    other = opponent(color)
    size = BOARD_AI_SIZES["gomoku"]
    score = 0
    for y in range(size):
        for x in range(size):
            for dx, dy in GOMOKU_DIRS:
                end_x = x + dx * 4
                end_y = y + dy * 4
                if not _in_bounds(end_x, end_y, size):
                    continue
                window = [board[_idx(x + dx * step, y + dy * step, size)] for step in range(5)]
                own = window.count(color)
                theirs = window.count(other)
                if own and theirs:
                    continue
                if own:
                    score += _gomoku_window_score(own)
                elif theirs:
                    score -= _gomoku_window_score(theirs) * 1.2
    return score


def _gomoku_count_direction(board, move, color, dx, dy, sign):
    size = BOARD_AI_SIZES["gomoku"]
    x, y = _xy(move, size)
    count = 0
    nx, ny = x + dx * sign, y + dy * sign
    while _in_bounds(nx, ny, size) and board[_idx(nx, ny, size)] == color:
        count += 1
        nx += dx * sign
        ny += dy * sign
    open_end = _in_bounds(nx, ny, size) and not board[_idx(nx, ny, size)]
    return count, open_end


def _gomoku_line_shapes(board, move, color):
    shapes = []
    for dx, dy in GOMOKU_DIRS:
        forward, forward_open = _gomoku_count_direction(board, move, color, dx, dy, 1)
        backward, backward_open = _gomoku_count_direction(board, move, color, dx, dy, -1)
        total = 1 + forward + backward
        open_ends = int(forward_open) + int(backward_open)
        shapes.append({"total": total, "open_ends": open_ends})
    return shapes


def _gomoku_winning_moves(board, color, candidates=None):
    moves = gomoku_candidate_moves(board, "hard") if candidates is None else candidates
    winners = []
    for move in moves:
        next_board = _gomoku_apply_move(board, move, color)
        if next_board and gomoku_has_five(next_board, move, color):
            winners.append(move)
    return sorted(winners)


def _gomoku_threat_profile(board, move, color, cache=None):
    key = (board, move, color)
    if cache is not None and key in cache:
        return cache[key]
    next_board = _gomoku_apply_move(board, move, color)
    if next_board is None:
        profile = {
            "score": -math.inf,
            "winning_replies": 0,
            "open_fours": 0,
            "closed_fours": 0,
            "open_threes": 0,
            "forcing_score": -math.inf,
        }
        if cache is not None:
            cache[key] = profile
        return profile
    if gomoku_has_five(next_board, move, color):
        profile = {
            "score": GOMOKU_WIN_SCORE,
            "winning_replies": 0,
            "open_fours": 0,
            "closed_fours": 0,
            "open_threes": 0,
            "forcing_score": GOMOKU_WIN_SCORE,
        }
        if cache is not None:
            cache[key] = profile
        return profile
    shapes = _gomoku_line_shapes(next_board, move, color)
    open_fours = sum(1 for shape in shapes if shape["total"] == 4 and shape["open_ends"] == 2)
    closed_fours = sum(1 for shape in shapes if shape["total"] == 4 and shape["open_ends"] == 1)
    open_threes = sum(1 for shape in shapes if shape["total"] == 3 and shape["open_ends"] == 2)
    reply_candidates = gomoku_candidate_moves(next_board, "hard")
    winning_replies = len(_gomoku_winning_moves(next_board, color, reply_candidates))
    forcing_score = 0
    if winning_replies >= 2:
        forcing_score = GOMOKU_OPEN_FOUR_SCORE + min(winning_replies, 4) * 25000
    elif winning_replies == 1 or open_fours:
        forcing_score = GOMOKU_CLOSED_FOUR_SCORE + open_fours * 60000
    if open_threes >= 2:
        forcing_score = max(forcing_score, GOMOKU_DOUBLE_THREE_SCORE + open_threes * 18000)
    elif open_threes == 1:
        forcing_score = max(forcing_score, 32000)
    score = _gomoku_eval(next_board, color) + forcing_score + closed_fours * 45000
    profile = {
        "score": score,
        "winning_replies": winning_replies,
        "open_fours": open_fours,
        "closed_fours": closed_fours,
        "open_threes": open_threes,
        "forcing_score": forcing_score,
    }
    if cache is not None:
        cache[key] = profile
    return profile


def _gomoku_tactical_score(board, move, color, cache=None):
    own = _gomoku_threat_profile(board, move, color, cache)
    block = _gomoku_threat_profile(board, move, opponent(color), cache)
    return own["score"] + block["forcing_score"] * 0.92


def _ordered_gomoku_moves(board, candidates, color, difficulty, cache=None):
    limit = {"easy": 8, "normal": 16, "hard": 28}[difficulty]
    scored = [
        (move, _gomoku_tactical_score(board, move, color, cache))
        for move in candidates
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return [move for move, _score in scored[:limit]]


def _find_gomoku_threat_move(board, color, candidates, min_score, cache=None):
    best = None
    for move in candidates:
        profile = _gomoku_threat_profile(board, move, color, cache)
        score = profile["forcing_score"]
        if score < min_score:
            continue
        if best is None or score > best[1]["forcing_score"] or (
            score == best[1]["forcing_score"] and move < best[0]
        ):
            best = (move, profile)
    return best


def _find_gomoku_winning_move(board, color, candidates):
    for move in candidates:
        next_board = _gomoku_apply_move(board, move, color)
        if next_board and gomoku_has_five(next_board, move, color):
            return move
    return None


def _gomoku_search(board, turn, depth, root_color, alpha, beta, difficulty, cache):
    key = ("gomoku-search", board, turn, depth, root_color, difficulty)
    if key in cache:
        return cache[key]
    candidates = gomoku_candidate_moves(board, difficulty)
    if depth <= 0 or not candidates:
        value = _gomoku_eval(board, root_color)
        cache[key] = value
        return value
    own_win = _find_gomoku_winning_move(board, turn, candidates)
    if own_win is not None:
        value = GOMOKU_WIN_SCORE if turn == root_color else -GOMOKU_WIN_SCORE
        cache[key] = value
        return value
    candidates = _ordered_gomoku_moves(board, candidates, turn, difficulty, cache)
    maximizing = turn == root_color
    cutoff = False
    if maximizing:
        value = -math.inf
        for move in candidates:
            next_board = _gomoku_apply_move(board, move, turn)
            if gomoku_has_five(next_board, move, turn):
                return GOMOKU_WIN_SCORE
            value = max(value, _gomoku_search(next_board, opponent(turn), depth - 1, root_color, alpha, beta, difficulty, cache))
            alpha = max(alpha, value)
            if alpha >= beta:
                cutoff = True
                break
        if not cutoff:
            cache[key] = value
        return value
    value = math.inf
    for move in candidates:
        next_board = _gomoku_apply_move(board, move, turn)
        if gomoku_has_five(next_board, move, turn):
            return -GOMOKU_WIN_SCORE
        value = min(value, _gomoku_search(next_board, opponent(turn), depth - 1, root_color, alpha, beta, difficulty, cache))
        beta = min(beta, value)
        if alpha >= beta:
            cutoff = True
            break
    if not cutoff:
        cache[key] = value
    return value


def _choose_gomoku_move(board, turn, difficulty):
    candidates = gomoku_candidate_moves(board, difficulty)
    if not candidates:
        return _decision("gomoku", turn, difficulty, "finish", reason="board-full")
    own_win = _find_gomoku_winning_move(board, turn, candidates)
    if own_win is not None:
        return _decision("gomoku", turn, difficulty, "move", own_win, 1000000, "win-now")
    block = _find_gomoku_winning_move(board, opponent(turn), candidates)
    if block is not None:
        return _decision("gomoku", turn, difficulty, "move", block, 900000, "block-five")
    cache = {}
    if difficulty in {"normal", "hard"}:
        own_threshold = GOMOKU_OPEN_FOUR_SCORE if difficulty == "normal" else GOMOKU_DOUBLE_THREE_SCORE
        own_threat = _find_gomoku_threat_move(board, turn, candidates, own_threshold, cache)
        if own_threat is not None:
            move, profile = own_threat
            return _decision("gomoku", turn, difficulty, "move", move, profile["score"], "threat-space")
        block_threshold = GOMOKU_OPEN_FOUR_SCORE if difficulty == "normal" else GOMOKU_DOUBLE_THREE_SCORE
        opponent_threat = _find_gomoku_threat_move(board, opponent(turn), candidates, block_threshold, cache)
        if opponent_threat is not None:
            move, profile = opponent_threat
            return _decision("gomoku", turn, difficulty, "move", move, profile["forcing_score"], "threat-block")
    depth = {"easy": 1, "normal": 2, "hard": 2}[difficulty]
    best_move = None
    best_score = -math.inf
    ordered = _ordered_gomoku_moves(board, candidates, turn, difficulty, cache)
    for move in ordered:
        next_board = _gomoku_apply_move(board, move, turn)
        score = _gomoku_search(next_board, opponent(turn), depth - 1, turn, -math.inf, math.inf, difficulty, cache)
        if score > best_score or (score == best_score and (best_move is None or move < best_move)):
            best_move = move
            best_score = score
    return _decision("gomoku", turn, difficulty, "move", best_move, best_score, "threat-alpha-beta")


def go_group_and_liberties(board, start):
    size = BOARD_AI_SIZES["go"]
    color = board[start]
    group = set()
    liberties = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in group:
            continue
        group.add(current)
        for neighbor in _neighbors(current, size):
            value = board[neighbor]
            if not value:
                liberties.add(neighbor)
            elif value == color and neighbor not in group:
                stack.append(neighbor)
    return group, liberties


def go_apply_move(board, index, color):
    size = BOARD_AI_SIZES["go"]
    if board[index]:
        return None, 0
    next_board = list(board)
    next_board[index] = color
    captured = 0
    other = opponent(color)
    for neighbor in _neighbors(index, size):
        if next_board[neighbor] != other:
            continue
        group, liberties = go_group_and_liberties(tuple(next_board), neighbor)
        if not liberties:
            captured += len(group)
            for stone in group:
                next_board[stone] = EMPTY
    own_group, own_liberties = go_group_and_liberties(tuple(next_board), index)
    if not own_liberties and captured == 0:
        return None, 0
    return tuple(next_board), captured


def go_legal_moves(board, color):
    moves = []
    for index, value in enumerate(board):
        if value:
            continue
        next_board, _captured = go_apply_move(board, index, color)
        if next_board is not None:
            moves.append(index)
    return moves


def go_candidate_moves(board, color, difficulty="normal", legal_moves=None):
    size = BOARD_AI_SIZES["go"]
    legal = set(go_legal_moves(board, color) if legal_moves is None else legal_moves)
    if not legal:
        return []
    stones = [index for index, value in enumerate(board) if value]
    center = _idx(size // 2, size // 2, size)
    if not stones:
        return [center] if center in legal else [min(legal)]
    radius = {"easy": 1, "normal": 2, "hard": 3}.get(difficulty, 2)
    candidates = set()
    for stone in stones:
        sx, sy = _xy(stone, size)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                nx, ny = sx + dx, sy + dy
                if _in_bounds(nx, ny, size):
                    index = _idx(nx, ny, size)
                    if index in legal:
                        candidates.add(index)
    if len(stones) < 14:
        candidates.update(index for index in GO_STAR_POINTS if index in legal)
        if center in legal:
            candidates.add(center)
    if difficulty == "hard":
        candidates.update(_go_life_death_candidate_moves(board, color))
    return sorted(candidates or legal)


def go_life_death_network(board, color):
    normalized = _normalize_board(list(board), BOARD_AI_SIZES["go"])
    return _go_life_death_network(normalized, _normalize_color(color))


def _go_eye_quality(board, point, color):
    if board[point]:
        return 0.0
    size = BOARD_AI_SIZES["go"]
    other = opponent(color)
    orthogonal = list(_neighbors(point, size))
    if any(board[neighbor] == other for neighbor in orthogonal):
        return 0.0
    own_neighbors = sum(1 for neighbor in orthogonal if board[neighbor] == color)
    empty_neighbors = sum(1 for neighbor in orthogonal if not board[neighbor])
    diagonal_values = [board[neighbor] for neighbor in _diagonal_neighbors(point, size)]
    diagonal_enemies = sum(1 for value in diagonal_values if value == other)
    diagonal_empty = sum(1 for value in diagonal_values if not value)
    x, y = _xy(point, size)
    edge_count = int(x in {0, size - 1}) + int(y in {0, size - 1})
    if own_neighbors == len(orthogonal):
        if edge_count >= 1:
            return 1.0 if diagonal_enemies == 0 else 0.18
        if diagonal_enemies == 0:
            return 1.0 if diagonal_empty == 0 else 0.82
        if diagonal_enemies == 1:
            return 0.48
        return 0.12
    if own_neighbors >= max(2, len(orthogonal) - 1) and empty_neighbors:
        return 0.45 if diagonal_enemies == 0 else 0.16
    return 0.0


def _go_group_infos(board):
    visited = set()
    infos = []
    for index, color in enumerate(board):
        if not color or index in visited:
            continue
        group, liberties = go_group_and_liberties(board, index)
        visited.update(group)
        eye_quality = [_go_eye_quality(board, liberty, color) for liberty in liberties]
        true_eyes = sum(1 for quality in eye_quality if quality >= 0.75)
        potential_eyes = sum(quality for quality in eye_quality if 0 < quality < 0.75)
        false_eye_risk = sum(1 for quality in eye_quality if 0 < quality < 0.3)
        connection_points = 0
        for liberty in liberties:
            friendly_neighbors = sum(1 for neighbor in _neighbors(liberty, BOARD_AI_SIZES["go"]) if board[neighbor] == color)
            if friendly_neighbors >= 2:
                connection_points += 1
        infos.append({
            "color": color,
            "stones": group,
            "liberties": liberties,
            "true_eyes": true_eyes,
            "potential_eyes": potential_eyes,
            "false_eye_risk": false_eye_risk,
            "connection_points": connection_points,
        })
    return infos


def _go_life_death_group_value(info):
    stones = len(info["stones"])
    liberties = len(info["liberties"])
    true_eyes = info["true_eyes"]
    eye_space = true_eyes + info["potential_eyes"]
    value = stones * 4 + liberties * 12 + true_eyes * 180 + info["potential_eyes"] * 70
    value += min(info["connection_points"], 4) * 26
    value -= info["false_eye_risk"] * 48
    if true_eyes >= 2:
        value += 520 + stones * 5
    elif eye_space >= 1.65 and liberties >= 3:
        value += 240 + stones * 3
    elif liberties == 1:
        value -= 280 + stones * 32
    elif liberties == 2:
        value -= 110 + stones * 14
    elif liberties == 3 and eye_space < 0.75:
        value -= 38 + stones * 4
    return value


def _go_life_death_network(board, color):
    score = 0.0
    other = opponent(color)
    for info in _go_group_infos(board):
        value = _go_life_death_group_value(info)
        if info["color"] == color:
            score += value
        elif info["color"] == other:
            score -= value * 1.06
    return score


def _go_adjacent_group_infos(board, move, color):
    infos = []
    seen = set()
    for neighbor in _neighbors(move, BOARD_AI_SIZES["go"]):
        if board[neighbor] != color:
            continue
        group, liberties = go_group_and_liberties(board, neighbor)
        key = frozenset(group)
        if key in seen:
            continue
        seen.add(key)
        eye_quality = [_go_eye_quality(board, liberty, color) for liberty in liberties]
        infos.append({
            "stones": group,
            "liberties": liberties,
            "true_eyes": sum(1 for quality in eye_quality if quality >= 0.75),
        })
    return infos


def _go_life_death_move_score(board, next_board, move, color, captured, life_before=None):
    if life_before is None:
        life_before = _go_life_death_network(board, color)
    life_after = _go_life_death_network(next_board, color)
    score = (life_after - life_before) * GO_LIFE_DEATH_WEIGHTS["network_delta"]
    other = opponent(color)
    if captured:
        score += captured * 92
    if captured == 0 and _go_eye_quality(board, move, color) >= 0.75:
        score -= GO_LIFE_DEATH_WEIGHTS["eye_fill_penalty"]
    for info in _go_adjacent_group_infos(board, move, color):
        liberties = len(info["liberties"])
        if move not in info["liberties"]:
            continue
        if liberties == 1:
            score += GO_LIFE_DEATH_WEIGHTS["atari_save"] + len(info["stones"]) * 36
        elif liberties == 2:
            score += GO_LIFE_DEATH_WEIGHTS["two_liberty_save"] + len(info["stones"]) * 16
    for info in _go_adjacent_group_infos(board, move, other):
        liberties = len(info["liberties"])
        if move not in info["liberties"]:
            continue
        if liberties == 1:
            score += GO_LIFE_DEATH_WEIGHTS["atari_attack"] + len(info["stones"]) * 34
        elif liberties == 2:
            score += GO_LIFE_DEATH_WEIGHTS["two_liberty_attack"] + len(info["stones"]) * 22
        elif liberties == 3 and info["true_eyes"] == 0:
            score += 140 + len(info["stones"]) * 8
    return score


def _go_life_death_candidate_moves(board, color):
    candidates = set()
    other = opponent(color)
    for info in _go_group_infos(board):
        liberties = set(info["liberties"])
        liberty_count = len(liberties)
        if not liberties:
            continue
        if info["color"] == color:
            if liberty_count <= 2 or (info["true_eyes"] < 2 and liberty_count <= 3):
                candidates.update(liberties)
        elif info["color"] == other:
            if liberty_count <= 3:
                candidates.update(liberties)
        for liberty in liberties:
            if _go_eye_quality(board, liberty, info["color"]) > 0.35:
                candidates.add(liberty)
    return {move for move in candidates if not board[move] and go_apply_move(board, move, color)[0] is not None}


def _go_area_eval(board, color):
    size = BOARD_AI_SIZES["go"]
    visited = set()
    territory = {"black": 0.0, "white": GO_KOMI}
    for index, value in enumerate(board):
        if value or index in visited:
            continue
        queue = [index]
        region = set()
        borders = set()
        visited.add(index)
        while queue:
            current = queue.pop()
            region.add(current)
            for neighbor in _neighbors(current, size):
                neighbor_value = board[neighbor]
                if neighbor_value:
                    borders.add(neighbor_value)
                elif neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if len(borders) == 1:
            owner = next(iter(borders))
            territory[owner] += len(region)
    other = opponent(color)
    return territory[color] - territory[other]


def _go_move_heuristic(board, move, color, difficulty="normal", life_before=None):
    next_board, captured = go_apply_move(board, move, color)
    if next_board is None:
        return -math.inf
    group, liberties = go_group_and_liberties(next_board, move)
    adjacent_allies = sum(1 for neighbor in _neighbors(move, BOARD_AI_SIZES["go"]) if board[neighbor] == color)
    adjacent_enemies = sum(1 for neighbor in _neighbors(move, BOARD_AI_SIZES["go"]) if board[neighbor] == opponent(color))
    x, y = _xy(move, BOARD_AI_SIZES["go"])
    center = (BOARD_AI_SIZES["go"] - 1) / 2
    center_bias = 18 - abs(x - center) - abs(y - center)
    score = captured * 36 + len(liberties) * 8 + adjacent_allies * 6 + adjacent_enemies * 3 + center_bias + _go_area_eval(next_board, color) * 5
    if difficulty == "hard":
        score += _go_life_death_move_score(board, next_board, move, color, captured, life_before=life_before)
    return score


def _rank_go_moves(board, moves, color, difficulty):
    if difficulty != "hard":
        ranked = [(move, _go_move_heuristic(board, move, color, difficulty)) for move in moves]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked
    base_ranked = [(move, _go_move_heuristic(board, move, color, "normal")) for move in moves]
    base_ranked.sort(key=lambda item: (-item[1], item[0]))
    urgent_moves = _go_life_death_candidate_moves(board, color)
    if not urgent_moves and sum(1 for value in board if value) < 10:
        return base_ranked
    full_network_moves = {move for move, _score in base_ranked[:32]}
    full_network_moves.update(urgent_moves)
    life_before = _go_life_death_network(board, color)
    ranked = []
    for move, base_score in base_ranked:
        if move in full_network_moves:
            ranked.append((move, _go_move_heuristic(board, move, color, "hard", life_before)))
        else:
            ranked.append((move, base_score))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked


def _seed_for_go(board, color, difficulty):
    payload = "|".join(board) + f"|{color}|{difficulty}"
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12], 16)


def _go_rollout_score(board, color, rng, depth):
    current = board
    turn = opponent(color)
    passes = 0
    for _step in range(depth):
        legal_moves = go_legal_moves(current, turn)
        if not legal_moves:
            passes += 1
            if passes >= 2:
                break
            turn = opponent(turn)
            continue
        passes = 0
        moves = go_candidate_moves(current, turn, "normal", legal_moves=legal_moves)
        ranked = _rank_go_moves(current, moves, turn, "normal")
        move = rng.choice([move for move, _score in ranked[: min(6, len(ranked))]])
        current, _captured = go_apply_move(current, move, turn)
        turn = opponent(turn)
    return _go_area_eval(current, color)


def _choose_go_move(board, turn, difficulty):
    if difficulty == GO_KATAGO_DIFFICULTY:
        return _choose_go_katago_move(board, turn)
    legal_moves = go_legal_moves(board, turn)
    if not legal_moves:
        return _decision("go", turn, difficulty, "pass", reason="no-legal-move")
    moves = go_candidate_moves(board, turn, difficulty, legal_moves=legal_moves)
    if difficulty == "hard":
        urgent_moves = sorted(_go_life_death_candidate_moves(board, turn))
        if urgent_moves:
            life_before = _go_life_death_network(board, turn)
            urgent_scores = [
                (move, _go_move_heuristic(board, move, turn, "hard", life_before))
                for move in urgent_moves
            ]
            urgent_scores.sort(key=lambda item: (-item[1], item[0]))
            move, score = urgent_scores[0]
            if score >= 250:
                return _decision("go", turn, difficulty, "move", move, score, "life-death-network")
    base_scores = _rank_go_moves(board, moves, turn, difficulty)
    if difficulty == "easy":
        move, score = base_scores[0]
        return _decision("go", turn, difficulty, "move", move, score, "capture-heuristic")
    candidate_limit = 8
    rollout_count = 1
    rollout_depth = 6 if difficulty == "normal" else 7
    rng = random.Random(_seed_for_go(board, turn, difficulty))
    best_move = None
    best_score = -math.inf
    for move, heuristic_score in base_scores[:candidate_limit]:
        next_board, _captured = go_apply_move(board, move, turn)
        rollout_score = sum(_go_rollout_score(next_board, turn, rng, rollout_depth) for _ in range(rollout_count)) / rollout_count
        score = heuristic_score + rollout_score
        if score > best_score or (score == best_score and (best_move is None or move < best_move)):
            best_move = move
            best_score = score
    reason = "life-death-network-rollout" if difficulty == "hard" else "mcts-rollout"
    return _decision("go", turn, difficulty, "move", best_move, best_score, reason)
