import json
import random
import secrets
from datetime import datetime, timedelta, timezone

from flask import request

from services.games.board_ai import (
    BOARD_AI_GAME_KEYS,
    KataGoUnavailable,
    board_ai_difficulties_for_game,
    choose_board_game_ai_move,
)
from services.games.chess import (
    board_rows,
    draw_claim_status,
    game_status,
    initial_board,
    king_square,
    legal_moves,
    opponent,
    validate_move,
)
from services.games.chess_engine import (
    EXPERIMENT_DIFFICULTY,
    choose_experiment_move,
    record_experiment_learning,
)
from services.games.chess_dl import (
    EXPERIMENT_DL_DIFFICULTY,
    choose_experiment_dl_move,
    record_experiment_dl_learning,
)
from services.games.chess_pv import (
    EXPERIMENT_PV_DIFFICULTY,
    choose_experiment_pv_move,
)
from services.games.chess_pv_guarded_overlay import (
    choose_experiment_pv_guarded_overlay_move,
    guarded_overlay_enabled,
)
from services.games.chess_nnue import (
    EXPERIMENT_NNUE_DIFFICULTY,
    EXP5_PRODUCTION_SEARCH_PROFILE,
    choose_experiment_nnue_move,
)
from services.games.chess_exp6 import (
    EXPERIMENT_NEURAL_DIFFICULTY,
    EXP6_DEFAULT_SEARCH_PROFILE,
    choose_experiment_neural_move,
)
from services.games.chess_nn import (
    EXPERIMENT_NN_DIFFICULTY,
    choose_experiment_nn_move,
)
from services.games.chess_opening_book import book_move as chess_book_move
from services.games.chess_stockfish_teacher import (
    DEFAULT_RUNTIME_DEPTH as DEFAULT_STOCKFISH_DEPTH,
    STOCKFISH_DIFFICULTY,
    choose_stockfish_move,
    stockfish_available,
)
from services.games.chess_dashboard import build_chess_engine_dashboard
from services.games.chess_pipeline import maybe_launch_chess_train_pipeline
from services.games.chess_promotion import (
    ensure_warm_start_chess_environment,
    promote_candidate_model,
    promotion_status_summary,
    stage_candidate_model,
)
from services.games.chess_replay_buffer import collect_match_replay
from services.games.rubiks_solver import (
    RubiksSolverUnavailable,
    solve_facelets as solve_rubiks_facelets,
)
from services.points_chain import DISPLAY_CURRENCY

GAME_KEY = "chess"
MIN_STOCKFISH_DEPTH = 1
MAX_STOCKFISH_DEPTH = 20
SOLO_GAME_KEYS = {
    "sudoku",
    "minesweeper",
    "1a2b",
    "tetris",
    "real_tetris",
    "space_shooter",
    "fps_arena",
    "open_world",
    "racing",
    "bullet_hell",
    "stickman_shooter",
    "snake",
    "game_2048",
    "brick_breaker",
    "rubiks_cube",
    "reversi",
    "go",
    "gomoku",
    "chinese_chess",
}
SCORE_RANKED_SOLO_GAMES = {
    "tetris",
    "real_tetris",
    "space_shooter",
    "fps_arena",
    "open_world",
    "racing",
    "bullet_hell",
    "stickman_shooter",
    "snake",
    "game_2048",
    "brick_breaker",
    "rubiks_cube",
    "reversi",
    "go",
    "gomoku",
    "chinese_chess",
}
MULTIPLAYER_GAME_KEYS = {"fps_arena", "stickman_shooter"}
MULTIPLAYER_MODES_BY_GAME = {
    "fps_arena": {"coop", "pvp"},
    "stickman_shooter": {"coop"},
}
SOLO_GAME_CHECK_SQL = "'sudoku', 'minesweeper', '1a2b', 'tetris', 'real_tetris', 'space_shooter', 'fps_arena', 'open_world', 'racing', 'bullet_hell', 'stickman_shooter', 'snake', 'game_2048', 'brick_breaker', 'rubiks_cube', 'reversi', 'go', 'gomoku', 'chinese_chess'"
WEEKLY_REWARDS = (300, 200, 100)
DAILY_CHALLENGE_REWARD_POINTS = 25
# New chess difficulty naming. ``experiment 0:minimax2ply`` is the
# renamed ``hard`` (2-ply material minimax); ``experiment 1:search``
# is the renamed ``experiment`` (engine search + learning store).
# The renames are pure relabelling — the dispatcher maps the new
# strings to the existing engine code, and legacy DB rows still
# selecting ``normal`` / ``hard`` / ``experiment`` keep playing.
EXP0_HEURISTIC_DIFFICULTY = "experiment 0:minimax2ply"
EXP1_SEARCH_DIFFICULTY = "experiment 1:search"
BASE_COMPUTER_DIFFICULTIES = {
    EXP0_HEURISTIC_DIFFICULTY,
    EXP1_SEARCH_DIFFICULTY,
    EXPERIMENT_NN_DIFFICULTY,
    EXPERIMENT_DL_DIFFICULTY,
    EXPERIMENT_PV_DIFFICULTY,
    EXPERIMENT_NNUE_DIFFICULTY,
    EXPERIMENT_NEURAL_DIFFICULTY,
}
COMPUTER_DIFFICULTIES = set(BASE_COMPUTER_DIFFICULTIES)
# Legacy strings kept in STORED so old game_matches rows continue to
# play and round-trip without a destructive migration.
LEGACY_CHESS_DIFFICULTIES = {"normal", "hard", EXPERIMENT_DIFFICULTY}
STORED_COMPUTER_DIFFICULTIES = (
    set(BASE_COMPUTER_DIFFICULTIES) | {STOCKFISH_DIFFICULTY} | LEGACY_CHESS_DIFFICULTIES
)
LEGACY_COMPUTER_DIFFICULTIES: set[str] = set()
MINESWEEPER_DIFFICULTIES = {"easy", "normal", "hard", "master"}
PIECE_VALUES = {
    "p": 100,
    "n": 320,
    "b": 330,
    "r": 500,
    "q": 900,
    "k": 20000,
}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def current_week_key(now=None):
    now = now or datetime.now(timezone.utc)
    year, week, _weekday = now.isocalendar()
    return f"{int(year)}-W{int(week):02d}"


def completed_week_start(week_key):
    year_part, week_part = str(week_key).split("-W", 1)
    return datetime.fromisocalendar(int(year_part), int(week_part), 1).replace(tzinfo=timezone.utc)


def default_board_json():
    return json.dumps(initial_board(), ensure_ascii=False, sort_keys=True)


def normalize_stockfish_depth(value, *, default=DEFAULT_STOCKFISH_DEPTH):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(MIN_STOCKFISH_DEPTH, min(MAX_STOCKFISH_DEPTH, parsed))


def computer_config_for_new_match(difficulty, payload):
    if difficulty != STOCKFISH_DIFFICULTY:
        return {}
    payload = payload or {}
    return {
        "stockfish_depth": normalize_stockfish_depth(
            payload.get("stockfish_depth", payload.get("depth", DEFAULT_STOCKFISH_DEPTH))
        )
    }


def load_computer_config(row):
    raw = "{}"
    try:
        if row is not None and "computer_config_json" in row.keys():
            raw = row["computer_config_json"] or "{}"
    except Exception:
        raw = "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


_LEGACY_DIFFICULTY_ALIASES = {
    # Legacy chess difficulty strings → canonical new strings. Only
    # applied on input from frontend; existing DB rows holding the
    # legacy strings (``normal`` / ``hard`` / ``experiment``) still
    # round-trip via STORED_COMPUTER_DIFFICULTIES + handled in dispatch.
    "hard": EXP0_HEURISTIC_DIFFICULTY,
    EXPERIMENT_DIFFICULTY: EXP1_SEARCH_DIFFICULTY,
}


def normalize_computer_difficulty(value):
    difficulty = str(value or "").strip().lower()
    if not difficulty:
        difficulty = "normal"
    if difficulty == "easy":
        difficulty = "normal"
    difficulty = _LEGACY_DIFFICULTY_ALIASES.get(difficulty, difficulty)
    allowed = set(BASE_COMPUTER_DIFFICULTIES) | {"normal"}
    if stockfish_available():
        allowed.add(STOCKFISH_DIFFICULTY)
    return difficulty if difficulty in allowed else None


def normalize_stored_computer_difficulty(value):
    difficulty = str(value or "normal").strip().lower()
    if difficulty == "easy":
        difficulty = "normal"
    return difficulty if difficulty in STORED_COMPUTER_DIFFICULTIES or difficulty in LEGACY_COMPUTER_DIFFICULTIES else "normal"


def computer_difficulty_options():
    rows = [
        {"key": EXP0_HEURISTIC_DIFFICULTY, "label": "實驗 0：2 層物質 minimax"},
        {"key": EXP1_SEARCH_DIFFICULTY, "label": "實驗 1：引擎搜尋 + 對局學習"},
        {"key": EXPERIMENT_NN_DIFFICULTY, "label": "實驗 2：NN 評估（autoretrain disconnected）"},
        {"key": EXPERIMENT_DL_DIFFICULTY, "label": "實驗 3：DL 語義平衡（autoretrain disconnected）"},
        {"key": EXPERIMENT_PV_DIFFICULTY, "label": "實驗 4：Policy/Value + MCTS（autoretrain disconnected）"},
        {"key": EXPERIMENT_NNUE_DIFFICULTY, "label": "實驗 5：NNUE + AlphaBeta/PVS"},
        {"key": EXPERIMENT_NEURAL_DIFFICULTY, "label": "實驗 6：Neural Network"},
    ]
    if stockfish_available():
        rows.append({
            "key": STOCKFISH_DIFFICULTY,
            "label": "Stockfish（本機）",
            "local_only": True,
        })
    return rows


def move_material_value(move):
    captured = str(move.get("captured") or "").lower()
    promotion = str(move.get("promotion") or "").lower()
    score = PIECE_VALUES.get(captured, 0)
    if promotion:
        score += max(0, PIECE_VALUES.get(promotion, 0) - PIECE_VALUES["p"])
    return score


def _choose_heuristic_move(board, side, difficulty="normal"):
    moves = legal_moves(board, side)
    if not moves:
        return None
    difficulty = normalize_computer_difficulty(difficulty) or "normal"

    scored = []
    for move in moves:
        try:
            applied = validate_move(board, side, move["from"], move["to"], move.get("promotion"))
        except ValueError:
            continue
        next_board = applied["board"]
        status = game_status(next_board, opponent(side))
        score = move_material_value(move)
        if status["status"] == "finished" and status.get("winner_color") == side:
            score += 100000
        elif status.get("reason") == "check":
            score += 60

        if difficulty == "hard" and status["status"] == "active":
            reply_scores = []
            for reply in legal_moves(next_board, opponent(side)):
                try:
                    reply_applied = validate_move(next_board, opponent(side), reply["from"], reply["to"], reply.get("promotion"))
                except ValueError:
                    continue
                reply_score = move_material_value(reply)
                reply_status = game_status(reply_applied["board"], side)
                if reply_status["status"] == "finished" and reply_status.get("winner_color") == opponent(side):
                    reply_score += 100000
                reply_scores.append(reply_score)
            if reply_scores:
                score -= max(reply_scores)
        scored.append((score, move))

    if not scored:
        return random.choice(moves)
    best_score = max(score for score, _move in scored)
    best_moves = [move for score, move in scored if score == best_score]
    return random.choice(best_moves)


def choose_computer_move(board, side, difficulty="normal", learning_store=None, move_history=None, computer_config=None):
    difficulty = normalize_computer_difficulty(difficulty) or "normal"
    if difficulty == STOCKFISH_DIFFICULTY:
        depth = normalize_stockfish_depth((computer_config or {}).get("stockfish_depth"))
        move = choose_stockfish_move(board, side, depth=depth)
        if move:
            return move
    if difficulty in (EXPERIMENT_DIFFICULTY, EXP1_SEARCH_DIFFICULTY) and learning_store is not None:
        try:
            learning_store.connect().close()
        except Exception:
            pass
    if difficulty == EXPERIMENT_NNUE_DIFFICULTY:
        board_payload = dict(board or {})
        if isinstance(move_history, list):
            board_payload["__move_history__"] = move_history
        move = choose_experiment_nnue_move(board_payload, side, search_profile=EXP5_PRODUCTION_SEARCH_PROFILE)
        if move:
            return move
    if difficulty == EXPERIMENT_NEURAL_DIFFICULTY:
        board_payload = dict(board or {})
        if isinstance(move_history, list):
            board_payload["__move_history__"] = move_history
        move = choose_experiment_neural_move(board_payload, side, search_profile=EXP6_DEFAULT_SEARCH_PROFILE)
        if move:
            return move
    # P2: try the static opening book first so engines without a built-in
    # opening repertoire stop playing offbeat first moves like ``a5``. The
    # book covers ~60 standard positions, returns ``None`` past book, and
    # never overrides difficulty-specific engines for mid- or end-game play.
    if difficulty != "easy":
        try:
            book_pick = chess_book_move(board, side)
        except Exception:
            book_pick = None
        if book_pick:
            return book_pick
    # exp1 = renamed legacy "experiment". Map back to the same engine.
    if difficulty in (EXPERIMENT_DIFFICULTY, EXP1_SEARCH_DIFFICULTY):
        move = choose_experiment_move(
            board, side, store=learning_store,
            difficulty=EXPERIMENT_DIFFICULTY,  # internal engine still uses "experiment"
            search_profile="fast",
        )
        if move:
            return move
    if difficulty == EXPERIMENT_NN_DIFFICULTY:
        move = choose_experiment_nn_move(board, side)
        if move:
            return move
    if difficulty == EXPERIMENT_DL_DIFFICULTY:
        move = choose_experiment_dl_move(board, side, search_profile="fast")
        if move:
            return move
    if difficulty == EXPERIMENT_PV_DIFFICULTY:
        if guarded_overlay_enabled():
            move = choose_experiment_pv_guarded_overlay_move(board, side, search_profile="fast", decision_mode="mcts")
        else:
            move = choose_experiment_pv_move(board, side, search_profile="fast", decision_mode="mcts")
        if move:
            return move
    # exp0 = renamed legacy "hard" (2-ply material minimax). The
    # heuristic dispatcher takes the legacy difficulty string.
    heuristic_difficulty = difficulty
    if difficulty == EXP0_HEURISTIC_DIFFICULTY:
        heuristic_difficulty = "hard"
    return _choose_heuristic_move(board, side, heuristic_difficulty)


def load_board(row):
    try:
        data = json.loads(row["board_json"] or "{}")
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def load_history(row):
    try:
        data = json.loads(row["move_history_json"] or "[]")
    except Exception:
        data = []
    return data if isinstance(data, list) else []


def game_schema_sql():
    return """
    CREATE TABLE IF NOT EXISTS game_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_key TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'pvp',
        status TEXT NOT NULL DEFAULT 'active',
        white_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        black_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        human_side TEXT NOT NULL DEFAULT 'white',
        computer_difficulty TEXT NOT NULL DEFAULT 'normal',
        computer_config_json TEXT NOT NULL DEFAULT '{{}}',
        current_turn TEXT NOT NULL DEFAULT 'white',
        board_json TEXT NOT NULL,
        move_history_json TEXT NOT NULL DEFAULT '[]',
        winner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        result_reason TEXT,
        draw_offer_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        draw_offer_at TEXT,
        leaderboard_week TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        white_deleted_at TEXT,
        black_deleted_at TEXT,
        CHECK (game_key IN ('chess')),
        CHECK (mode IN ('pvp', 'computer')),
        CHECK (status IN ('active', 'finished', 'cancelled')),
        CHECK (current_turn IN ('white', 'black')),
        CHECK (human_side IN ('white', 'black')),
        CHECK (computer_difficulty IN ('easy', 'normal', 'hard', 'experiment', 'experiment 0:minimax2ply', 'experiment 1:search', 'experiment 2:nn', 'experiment 3:dl', 'experiment 4:pv', 'experiment 5:nnue', 'experiment 6:neuralnet', 'stockfish'))
    );
    CREATE TABLE IF NOT EXISTS game_invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_key TEXT NOT NULL,
        inviter_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        opponent_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        match_id INTEGER REFERENCES game_matches(id) ON DELETE SET NULL,
        message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        CHECK (game_key IN ('chess')),
        CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled', 'expired'))
    );
    CREATE TABLE IF NOT EXISTS game_leaderboard_rewards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_key TEXT NOT NULL,
        week_key TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        rank INTEGER NOT NULL,
        score INTEGER NOT NULL,
        reward_points INTEGER NOT NULL,
        ledger_uuid TEXT,
        awarded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        UNIQUE(game_key, week_key, user_id)
    );
    CREATE TABLE IF NOT EXISTS game_daily_challenge_rewards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_key TEXT NOT NULL,
        challenge_key TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        score_id INTEGER REFERENCES game_solo_scores(id) ON DELETE SET NULL,
        reward_points INTEGER NOT NULL,
        ledger_uuid TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(game_key, challenge_key, user_id)
    );
    CREATE TABLE IF NOT EXISTS game_solo_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_key TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        week_key TEXT NOT NULL,
        difficulty TEXT NOT NULL DEFAULT 'standard',
        puzzle_id TEXT,
        score INTEGER NOT NULL DEFAULT 0,
        guess_count INTEGER NOT NULL DEFAULT 0,
        raw_elapsed_ms INTEGER NOT NULL,
        penalty_seconds INTEGER NOT NULL DEFAULT 0,
        elapsed_ms INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        CHECK (game_key IN ({SOLO_GAME_CHECK_SQL})),
        CHECK (elapsed_ms > 0),
        CHECK (raw_elapsed_ms > 0),
        CHECK (penalty_seconds >= 0)
    );
    CREATE TABLE IF NOT EXISTS game_multiplayer_rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_code TEXT NOT NULL UNIQUE,
        game_key TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'lobby',
        host_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        guest_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        expires_at TEXT,
        CHECK (game_key IN ('fps_arena', 'stickman_shooter')),
        CHECK (mode IN ('coop', 'pvp')),
        CHECK (status IN ('lobby', 'active', 'finished', 'cancelled'))
    );
    CREATE TABLE IF NOT EXISTS game_multiplayer_invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL REFERENCES game_multiplayer_rooms(id) ON DELETE CASCADE,
        game_key TEXT NOT NULL,
        mode TEXT NOT NULL,
        inviter_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        invitee_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        CHECK (game_key IN ('fps_arena', 'stickman_shooter')),
        CHECK (mode IN ('coop', 'pvp')),
        CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled', 'expired'))
    );
    CREATE TABLE IF NOT EXISTS game_multiplayer_player_states (
        room_id INTEGER NOT NULL REFERENCES game_multiplayer_rooms(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        state_json TEXT NOT NULL DEFAULT '{{}}',
        sequence INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (room_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS game_multiplayer_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL REFERENCES game_multiplayer_rooms(id) ON DELETE CASCADE,
        sender_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        target_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL,
        CHECK (event_type IN ('gunshot', 'player_hit', 'friendly_fire', 'objective', 'down', 'finish'))
    );
    CREATE INDEX IF NOT EXISTS idx_game_matches_players ON game_matches(game_key, status, white_user_id, black_user_id);
    CREATE INDEX IF NOT EXISTS idx_game_matches_finished ON game_matches(game_key, mode, finished_at);
    CREATE INDEX IF NOT EXISTS idx_game_matches_leaderboard_week ON game_matches(game_key, mode, status, leaderboard_week, winner_user_id);
    CREATE INDEX IF NOT EXISTS idx_game_matches_white_visible ON game_matches(game_key, white_user_id, white_deleted_at, status, id);
    CREATE INDEX IF NOT EXISTS idx_game_matches_black_visible ON game_matches(game_key, black_user_id, black_deleted_at, status, id);
    CREATE INDEX IF NOT EXISTS idx_game_invites_user_status ON game_invites(game_key, opponent_user_id, status);
    CREATE INDEX IF NOT EXISTS idx_game_invites_inviter_status ON game_invites(game_key, inviter_user_id, status, id);
    CREATE INDEX IF NOT EXISTS idx_game_rewards_week ON game_leaderboard_rewards(game_key, week_key);
    CREATE INDEX IF NOT EXISTS idx_game_daily_rewards_user ON game_daily_challenge_rewards(user_id, challenge_key);
    CREATE INDEX IF NOT EXISTS idx_game_solo_scores_rank ON game_solo_scores(game_key, week_key, difficulty, elapsed_ms);
    CREATE INDEX IF NOT EXISTS idx_game_solo_scores_best_time ON game_solo_scores(game_key, week_key, difficulty, puzzle_id, user_id, elapsed_ms, created_at, id);
    CREATE INDEX IF NOT EXISTS idx_game_multiplayer_rooms_players ON game_multiplayer_rooms(game_key, status, host_user_id, guest_user_id);
    CREATE INDEX IF NOT EXISTS idx_game_multiplayer_invites_user ON game_multiplayer_invites(game_key, invitee_user_id, status);
    CREATE INDEX IF NOT EXISTS idx_game_multiplayer_invites_inviter ON game_multiplayer_invites(game_key, inviter_user_id, status, id);
    CREATE INDEX IF NOT EXISTS idx_game_multiplayer_events_room ON game_multiplayer_events(room_id, id);
    """.format(SOLO_GAME_CHECK_SQL=SOLO_GAME_CHECK_SQL)


def rebuild_solo_score_table(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_solo_scores)").fetchall()}
    if not cols:
        return
    score_expr = "score" if "score" in cols else "0"
    guess_expr = "guess_count" if "guess_count" in cols else "0"
    conn.execute("ALTER TABLE game_solo_scores RENAME TO game_solo_scores_old")
    conn.execute(
        """
        CREATE TABLE game_solo_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_key TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            week_key TEXT NOT NULL,
            difficulty TEXT NOT NULL DEFAULT 'standard',
            puzzle_id TEXT,
            score INTEGER NOT NULL DEFAULT 0,
            guess_count INTEGER NOT NULL DEFAULT 0,
            raw_elapsed_ms INTEGER NOT NULL,
            penalty_seconds INTEGER NOT NULL DEFAULT 0,
            elapsed_ms INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (game_key IN ({SOLO_GAME_CHECK_SQL})),
            CHECK (elapsed_ms > 0),
            CHECK (raw_elapsed_ms > 0),
            CHECK (penalty_seconds >= 0)
        )
        """.format(SOLO_GAME_CHECK_SQL=SOLO_GAME_CHECK_SQL)
    )
    conn.execute(
        f"""
        INSERT INTO game_solo_scores (
            id, game_key, user_id, week_key, difficulty, puzzle_id,
            score, guess_count, raw_elapsed_ms, penalty_seconds, elapsed_ms, created_at
        )
        SELECT id, game_key, user_id, week_key, difficulty, puzzle_id,
               {score_expr}, {guess_expr}, raw_elapsed_ms, penalty_seconds, elapsed_ms, created_at
        FROM game_solo_scores_old
        """
    )
    conn.execute("DROP TABLE game_solo_scores_old")


def rebuild_game_matches_table(conn):
    original_foreign_keys = int(conn.execute("PRAGMA foreign_keys").fetchone()[0] or 0)
    original_legacy_alter_table = int(conn.execute("PRAGMA legacy_alter_table").fetchone()[0] or 0)
    if original_foreign_keys:
        conn.execute("PRAGMA foreign_keys=OFF")
    if not original_legacy_alter_table:
        conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("ALTER TABLE game_matches RENAME TO game_matches_old")
        conn.execute(
            """
            CREATE TABLE game_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_key TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'pvp',
                status TEXT NOT NULL DEFAULT 'active',
                white_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                black_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                human_side TEXT NOT NULL DEFAULT 'white',
                computer_difficulty TEXT NOT NULL DEFAULT 'normal',
                computer_config_json TEXT NOT NULL DEFAULT '{}',
                current_turn TEXT NOT NULL DEFAULT 'white',
                board_json TEXT NOT NULL,
                move_history_json TEXT NOT NULL DEFAULT '[]',
                winner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                result_reason TEXT,
                draw_offer_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                draw_offer_at TEXT,
                leaderboard_week TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                white_deleted_at TEXT,
                black_deleted_at TEXT,
                CHECK (game_key IN ('chess')),
                CHECK (mode IN ('pvp', 'computer')),
                CHECK (status IN ('active', 'finished', 'cancelled')),
                CHECK (current_turn IN ('white', 'black')),
                CHECK (human_side IN ('white', 'black')),
                CHECK (computer_difficulty IN ('easy', 'normal', 'hard', 'experiment', 'experiment 0:minimax2ply', 'experiment 1:search', 'experiment 2:nn', 'experiment 3:dl', 'experiment 4:pv', 'experiment 5:nnue', 'experiment 6:neuralnet', 'stockfish'))
            )
            """
        )
        old_cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_matches_old)").fetchall()}

        def old_expr(name, fallback_sql):
            return name if name in old_cols else fallback_sql

        conn.execute(
            f"""
            INSERT INTO game_matches (
                id, game_key, mode, status, white_user_id, black_user_id, human_side,
                computer_difficulty, computer_config_json, current_turn, board_json, move_history_json,
                winner_user_id, result_reason, draw_offer_by_user_id, draw_offer_at, leaderboard_week, created_at, updated_at,
                finished_at, white_deleted_at, black_deleted_at
            )
            SELECT
                id,
                game_key,
                COALESCE({old_expr('mode', "'pvp'")}, 'pvp'),
                COALESCE({old_expr('status', "'active'")}, 'active'),
                white_user_id,
                {old_expr('black_user_id', 'NULL')},
                COALESCE({old_expr('human_side', "'white'")}, 'white'),
                COALESCE({old_expr('computer_difficulty', "'normal'")}, 'normal'),
                COALESCE({old_expr('computer_config_json', "'{{}}'")}, '{{}}'),
                COALESCE({old_expr('current_turn', "'white'")}, 'white'),
                board_json,
                COALESCE({old_expr('move_history_json', "'[]'")}, '[]'),
                {old_expr('winner_user_id', 'NULL')},
                {old_expr('result_reason', 'NULL')},
                {old_expr('draw_offer_by_user_id', 'NULL')},
                {old_expr('draw_offer_at', 'NULL')},
                {old_expr('leaderboard_week', 'NULL')},
                created_at,
                updated_at,
                {old_expr('finished_at', 'NULL')},
                {old_expr('white_deleted_at', 'NULL')},
                {old_expr('black_deleted_at', 'NULL')}
            FROM game_matches_old
            """
        )
        conn.execute("DROP TABLE game_matches_old")
    finally:
        if not original_legacy_alter_table:
            conn.execute("PRAGMA legacy_alter_table=OFF")
        if original_foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")


def ensure_game_schema(conn):
    conn.executescript(game_schema_sql())
    solo_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='game_solo_scores'"
    ).fetchone()
    solo_sql = str(solo_schema["sql"] or "") if solo_schema else ""
    if solo_schema and any(key not in solo_sql for key in SOLO_GAME_KEYS):
        rebuild_solo_score_table(conn)
    solo_cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_solo_scores)").fetchall()}
    if "score" not in solo_cols:
        conn.execute("ALTER TABLE game_solo_scores ADD COLUMN score INTEGER NOT NULL DEFAULT 0")
    if "guess_count" not in solo_cols:
        conn.execute("ALTER TABLE game_solo_scores ADD COLUMN guess_count INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_solo_scores_rank ON game_solo_scores(game_key, week_key, difficulty, elapsed_ms)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_solo_scores_guesses_rank ON game_solo_scores(game_key, week_key, difficulty, guess_count, elapsed_ms)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_solo_scores_score_rank ON game_solo_scores(game_key, week_key, difficulty, score DESC, elapsed_ms)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_solo_scores_best_time ON game_solo_scores(game_key, week_key, difficulty, puzzle_id, user_id, elapsed_ms, created_at, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_solo_scores_best_score ON game_solo_scores(game_key, week_key, difficulty, puzzle_id, user_id, score DESC, elapsed_ms, created_at, id)"
    )
    match_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='game_matches'"
    ).fetchone()
    match_sql = str(match_schema["sql"] or "") if match_schema else ""
    if match_schema and (
        "experiment" not in match_sql
        or "experiment 2:nn" not in match_sql
        or "experiment 3:dl" not in match_sql
        or "experiment 4:pv" not in match_sql
        or "experiment 5:nnue" not in match_sql
        or "experiment 6:neuralnet" not in match_sql
        or "experiment 0:minimax2ply" not in match_sql
        or "experiment 1:search" not in match_sql
        or "stockfish" not in match_sql
    ):
        rebuild_game_matches_table(conn)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_matches)").fetchall()}
    if "white_deleted_at" not in cols:
        conn.execute("ALTER TABLE game_matches ADD COLUMN white_deleted_at TEXT")
    if "black_deleted_at" not in cols:
        conn.execute("ALTER TABLE game_matches ADD COLUMN black_deleted_at TEXT")
    if "human_side" not in cols:
        conn.execute("ALTER TABLE game_matches ADD COLUMN human_side TEXT NOT NULL DEFAULT 'white'")
    if "computer_difficulty" not in cols:
        conn.execute("ALTER TABLE game_matches ADD COLUMN computer_difficulty TEXT NOT NULL DEFAULT 'easy'")
    if "computer_config_json" not in cols:
        conn.execute("ALTER TABLE game_matches ADD COLUMN computer_config_json TEXT NOT NULL DEFAULT '{}'")
    if "draw_offer_by_user_id" not in cols:
        conn.execute("ALTER TABLE game_matches ADD COLUMN draw_offer_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
    if "draw_offer_at" not in cols:
        conn.execute("ALTER TABLE game_matches ADD COLUMN draw_offer_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_matches_players ON game_matches(game_key, status, white_user_id, black_user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_matches_finished ON game_matches(game_key, mode, finished_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_matches_leaderboard_week ON game_matches(game_key, mode, status, leaderboard_week, winner_user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_matches_white_visible ON game_matches(game_key, white_user_id, white_deleted_at, status, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_matches_black_visible ON game_matches(game_key, black_user_id, black_deleted_at, status, id)"
    )
    conn.commit()


def user_filter_columns(conn):
    try:
        return {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    except Exception:
        return set()


def game_table_exists(conn, table_name):
    try:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table_name,)).fetchone())
    except Exception:
        return False


def users_are_game_friends(conn, user_id, other_user_id):
    if not game_table_exists(conn, "user_friends"):
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM user_friends
        WHERE status='accepted'
          AND (
            (user_id=? AND friend_user_id=?)
            OR (friend_user_id=? AND user_id=?)
          )
        LIMIT 1
        """,
        (int(user_id), int(other_user_id), int(user_id), int(other_user_id)),
    ).fetchone()
    return bool(row)


def active_user_where(conn, alias=""):
    cols = user_filter_columns(conn)
    prefix = f"{alias}." if alias else ""
    clauses = []
    if "status" in cols:
        clauses.append(f"COALESCE({prefix}status, 'active')='active'")
    if "deleted_at" in cols:
        clauses.append(f"COALESCE({prefix}deleted_at, '')=''")
    return " AND ".join(clauses) if clauses else "1=1"


def serialize_match(row, actor_id=None):
    board = load_board(row)
    history = load_history(row)
    status_info = game_status(board, row["current_turn"])
    current_king_square = king_square(board, row["current_turn"])
    draw_status = draw_claim_status(board, row["current_turn"], move_history=history)
    side = None
    human_side = row["human_side"] if "human_side" in row.keys() else "white"
    computer_difficulty = row["computer_difficulty"] if "computer_difficulty" in row.keys() else "normal"
    computer_difficulty = normalize_stored_computer_difficulty(computer_difficulty)
    computer_config = load_computer_config(row)
    stockfish_depth = (
        normalize_stockfish_depth(computer_config.get("stockfish_depth"))
        if computer_difficulty == STOCKFISH_DIFFICULTY
        else None
    )
    if actor_id:
        if row["mode"] == "computer" and int(row["white_user_id"]) == int(actor_id):
            side = human_side
        elif int(row["white_user_id"]) == int(actor_id):
            side = "white"
        elif row["black_user_id"] and int(row["black_user_id"]) == int(actor_id):
            side = "black"
    white_username = row["white_username"]
    black_username = row["black_username"] or ("電腦" if row["mode"] == "computer" else "-")
    if row["mode"] == "computer" and human_side == "black":
        white_username = "電腦"
        black_username = row["white_username"]
    draw_offer_by_user_id = row["draw_offer_by_user_id"] if "draw_offer_by_user_id" in row.keys() else None
    draw_offer_pending = row["status"] == "active" and row["mode"] == "pvp" and bool(draw_offer_by_user_id)
    draw_offer_by_side = None
    if draw_offer_pending and draw_offer_by_user_id:
        if int(draw_offer_by_user_id) == int(row["white_user_id"]):
            draw_offer_by_side = "white"
        elif row["black_user_id"] and int(draw_offer_by_user_id) == int(row["black_user_id"]):
            draw_offer_by_side = "black"
    can_offer_draw = row["status"] == "active" and row["mode"] == "pvp" and bool(side) and not draw_offer_pending
    can_respond_draw_offer = draw_offer_pending and bool(side) and int(draw_offer_by_user_id or 0) != int(actor_id or 0)
    return {
        "id": row["id"],
        "game_key": row["game_key"],
        "mode": row["mode"],
        "status": row["status"],
        "white_user_id": row["white_user_id"],
        "white_username": white_username,
        "black_user_id": row["black_user_id"],
        "black_username": black_username,
        "human_side": human_side,
        "computer_difficulty": computer_difficulty,
        "computer_config": computer_config,
        "stockfish_depth": stockfish_depth,
        "current_turn": row["current_turn"],
        "board": board,
        "board_rows": board_rows(board),
        "current_check": row["status"] == "active" and status_info.get("reason") == "check",
        "current_king_square": current_king_square or "",
        "move_history": history,
        "winner_user_id": row["winner_user_id"],
        "winner_username": row["winner_username"],
        "result_reason": row["result_reason"] or "",
        "my_side": side,
        "draw_offer_pending": draw_offer_pending,
        "draw_offer_by_user_id": draw_offer_by_user_id,
        "draw_offer_by_username": row["draw_offer_by_username"] if "draw_offer_by_username" in row.keys() else None,
        "draw_offer_by_side": draw_offer_by_side,
        "draw_offer_at": row["draw_offer_at"] if "draw_offer_at" in row.keys() else None,
        "can_offer_draw": can_offer_draw,
        "can_accept_draw_offer": bool(can_respond_draw_offer),
        "can_reject_draw_offer": bool(can_respond_draw_offer),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
        "legal_moves": legal_moves(board, row["current_turn"]) if row["status"] == "active" else [],
        "can_claim_draw": bool(draw_status["can_claim"]) if row["status"] == "active" else False,
        "draw_claim_reasons": list(draw_status["reasons"]) if row["status"] == "active" else [],
    }


def match_select_sql(where):
    return f"""
        SELECT m.*,
               wu.username AS white_username,
               bu.username AS black_username,
               win.username AS winner_username,
               offerer.username AS draw_offer_by_username
        FROM game_matches m
        JOIN users wu ON wu.id=m.white_user_id
        LEFT JOIN users bu ON bu.id=m.black_user_id
        LEFT JOIN users win ON win.id=m.winner_user_id
        LEFT JOIN users offerer ON offerer.id=m.draw_offer_by_user_id
        WHERE {where}
    """


def serialize_invite(row):
    return {
        "id": row["id"],
        "game_key": row["game_key"],
        "status": row["status"],
        "inviter_user_id": row["inviter_user_id"],
        "inviter_username": row["inviter_username"],
        "opponent_user_id": row["opponent_user_id"],
        "opponent_username": row["opponent_username"],
        "match_id": row["match_id"],
        "message": row["message"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
    }


def normalize_multiplayer_game_key(game_key):
    normalized = str(game_key or "").strip().lower()
    return normalized if normalized in MULTIPLAYER_GAME_KEYS else None


def normalize_multiplayer_mode(game_key, mode):
    normalized = str(mode or "coop").strip().lower()
    allowed = MULTIPLAYER_MODES_BY_GAME.get(game_key) or set()
    return normalized if normalized in allowed else None


def multiplayer_room_select_sql(where):
    return f"""
        SELECT r.*,
               host.username AS host_username,
               guest.username AS guest_username
        FROM game_multiplayer_rooms r
        JOIN users host ON host.id=r.host_user_id
        LEFT JOIN users guest ON guest.id=r.guest_user_id
        WHERE {where}
    """


def serialize_multiplayer_room(row, actor_id=None):
    my_role = ""
    if actor_id:
        if int(row["host_user_id"]) == int(actor_id):
            my_role = "host"
        elif row["guest_user_id"] and int(row["guest_user_id"]) == int(actor_id):
            my_role = "guest"
    return {
        "id": row["id"],
        "room_code": row["room_code"],
        "game_key": row["game_key"],
        "mode": row["mode"],
        "status": row["status"],
        "host_user_id": row["host_user_id"],
        "host_username": row["host_username"],
        "guest_user_id": row["guest_user_id"],
        "guest_username": row["guest_username"],
        "my_role": my_role,
        "can_start": bool(my_role and row["guest_user_id"] and row["status"] in {"lobby", "active"}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "expires_at": row["expires_at"],
    }


def serialize_multiplayer_invite(row):
    return {
        "id": row["id"],
        "room_id": row["room_id"],
        "game_key": row["game_key"],
        "mode": row["mode"],
        "status": row["status"],
        "inviter_user_id": row["inviter_user_id"],
        "inviter_username": row["inviter_username"],
        "invitee_user_id": row["invitee_user_id"],
        "invitee_username": row["invitee_username"],
        "message": row["message"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
    }


def multiplayer_json_obj(value, fallback=None):
    try:
        data = json.loads(value or "{}")
    except Exception:
        data = fallback if fallback is not None else {}
    return data if isinstance(data, dict) else (fallback if fallback is not None else {})


def serialize_multiplayer_player_state(row):
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "sequence": row["sequence"],
        "updated_at": row["updated_at"],
        "state": multiplayer_json_obj(row["state_json"]),
    }


def serialize_multiplayer_event(row):
    return {
        "id": row["id"],
        "room_id": row["room_id"],
        "sender_user_id": row["sender_user_id"],
        "sender_username": row["sender_username"],
        "target_user_id": row["target_user_id"],
        "event_type": row["event_type"],
        "payload": multiplayer_json_obj(row["payload_json"]),
        "created_at": row["created_at"],
    }


def finish_match(conn, row, status_info, now):
    winner_color = status_info.get("winner_color")
    winner_user_id = None
    human_side = row["human_side"] if "human_side" in row.keys() else "white"
    if row["mode"] == "computer":
        if winner_color == human_side:
            winner_user_id = row["white_user_id"]
    elif winner_color == "white":
        winner_user_id = row["white_user_id"]
    elif winner_color == "black":
        winner_user_id = row["black_user_id"]
    week = current_week_key(datetime.fromisoformat(now.replace("Z", "+00:00")))
    conn.execute(
        """
        UPDATE game_matches
        SET status='finished', winner_user_id=?, result_reason=?, draw_offer_by_user_id=NULL, draw_offer_at=NULL, leaderboard_week=?, finished_at=?, updated_at=?
        WHERE id=?
        """,
        (winner_user_id, status_info.get("reason") or "draw", week, now, now, row["id"]),
    )


def register_games_routes(app, deps):
    ensure_warm_start_chess_environment()
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_db = deps["get_db"]
    json_resp = deps["json_resp"]
    require_csrf = deps["require_csrf"]
    require_csrf_safe = deps["require_csrf_safe"]
    points_service = deps.get("points_service")
    audit = deps.get("audit", lambda *args, **kwargs: None)
    get_client_ip = deps.get("get_client_ip", lambda: "")
    get_ua = deps.get("get_ua", lambda: "")
    chess_engine_store = deps.get("chess_engine_store")

    def actor_or_401():
        actor = get_current_user_ctx()
        if not actor:
            return None, json_resp({"ok": False, "msg": "未登入"}), 401
        return actor, None, None

    def parse_json_body():
        try:
            data = request.get_json(force=True)
        except Exception:
            return None, json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return None, json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        return data, None, None

    def compact_response_requested(data=None):
        raw = request.args.get("compact")
        if raw is None and isinstance(data, dict):
            raw = data.get("compact")
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "compact"}

    def user_row(conn, user_id):
        return conn.execute("SELECT id, username, role, status FROM users WHERE id=?", (int(user_id),)).fetchone()

    def match_row(conn, match_id):
        return conn.execute(match_select_sql("m.id=?"), (int(match_id),)).fetchone()

    def actor_can_play(actor, row):
        return int(row["white_user_id"]) == int(actor["id"]) or (row["black_user_id"] and int(row["black_user_id"]) == int(actor["id"]))

    def actor_color(actor, row):
        if row["mode"] == "computer" and int(row["white_user_id"]) == int(actor["id"]):
            return row["human_side"] if "human_side" in row.keys() else "white"
        if int(row["white_user_id"]) == int(actor["id"]):
            return "white"
        if row["black_user_id"] and int(row["black_user_id"]) == int(actor["id"]):
            return "black"
        return None

    def actor_delete_column(actor, row):
        if int(row["white_user_id"]) == int(actor["id"]):
            return "white_deleted_at"
        if row["black_user_id"] and int(row["black_user_id"]) == int(actor["id"]):
            return "black_deleted_at"
        return None

    def multiplayer_room_row(conn, room_id):
        return conn.execute(multiplayer_room_select_sql("r.id=?"), (int(room_id),)).fetchone()

    def actor_can_access_multiplayer_room(actor, row):
        return bool(
            row
            and (
                int(row["host_user_id"]) == int(actor["id"])
                or (row["guest_user_id"] and int(row["guest_user_id"]) == int(actor["id"]))
            )
        )

    def multiplayer_room_states(conn, room_id):
        rows = conn.execute(
            """
            SELECT s.*, u.username
            FROM game_multiplayer_player_states s
            JOIN users u ON u.id=s.user_id
            WHERE s.room_id=?
            ORDER BY s.updated_at DESC
            """,
            (int(room_id),),
        ).fetchall()
        return [serialize_multiplayer_player_state(row) for row in rows]

    def multiplayer_room_events(conn, room_id, actor_id, after_event_id=0, limit=80):
        rows = conn.execute(
            """
            SELECT e.*, sender.username AS sender_username
            FROM game_multiplayer_events e
            JOIN users sender ON sender.id=e.sender_user_id
            WHERE e.room_id=?
              AND e.id>?
              AND (e.target_user_id IS NULL OR e.target_user_id=? OR e.sender_user_id=?)
            ORDER BY e.id ASC
            LIMIT ?
            """,
            (int(room_id), int(after_event_id or 0), int(actor_id), int(actor_id), int(limit)),
        ).fetchall()
        return [serialize_multiplayer_event(row) for row in rows]

    def multiplayer_room_payload(conn, row, actor_id, after_event_id=0):
        return {
            "room": serialize_multiplayer_room(row, actor_id),
            "players": multiplayer_room_states(conn, row["id"]),
            "events": multiplayer_room_events(conn, row["id"], actor_id, after_event_id=after_event_id),
        }

    def make_multiplayer_room_code(conn):
        for _attempt in range(12):
            code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()
            exists = conn.execute("SELECT id FROM game_multiplayer_rooms WHERE room_code=?", (code,)).fetchone()
            if not exists:
                return code
        return secrets.token_hex(5).upper()

    def clamp_multiplayer_payload_json(value, label, max_bytes=12000):
        try:
            payload = json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False, sort_keys=True)
        except Exception:
            raise ValueError(f"{label} 格式錯誤")
        if len(payload.encode("utf-8")) > max_bytes:
            raise ValueError(f"{label} 太大")
        return payload

    def award_weekly_rewards(week, actor=None):
        conn = get_db()
        try:
            ensure_game_schema(conn)
            rows = leaderboard_rows(conn, week)[:3]
        finally:
            conn.close()
        awarded = []
        for index, row in enumerate(rows):
            reward_points = WEEKLY_REWARDS[index]
            ledger_uuid = None
            if points_service:
                result = points_service.rc1_facade().grant_reward(
                    user_id=row["user_id"],
                    amount=reward_points,
                    action_type="game_weekly_leaderboard_reward",
                    reference_type="game_weekly_leaderboard",
                    reference_id=f"{GAME_KEY}:{week}:{index + 1}",
                    idempotency_key=f"game_weekly_reward:{GAME_KEY}:{week}:{row['user_id']}",
                    reason=f"西洋棋週排行榜第 {index + 1} 名獎勵",
                    currency_type=DISPLAY_CURRENCY,
                    public_metadata={"game_key": GAME_KEY, "week": week, "rank": index + 1, "score": row["score"]},
                    actor=actor,
                )
                ledger_uuid = result["ledger"]["ledger_uuid"]
            conn = get_db()
            try:
                ensure_game_schema(conn)
                now = utc_now()
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO game_leaderboard_rewards (
                        game_key, week_key, user_id, rank, score, reward_points, ledger_uuid, awarded_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        GAME_KEY,
                        week,
                        row["user_id"],
                        index + 1,
                        row["score"],
                        reward_points,
                        ledger_uuid,
                        int(actor["id"]) if actor and "id" in actor.keys() else None,
                        now,
                    ),
                )
                conn.commit()
                inserted = cur.rowcount > 0
            finally:
                conn.close()
            if inserted:
                awarded.append({"user_id": row["user_id"], "username": row["username"], "rank": index + 1, "reward_points": reward_points})
        return awarded

    def daily_challenge_reward_status(conn, game_key, challenge_key, user_id):
        row = conn.execute(
            """
            SELECT reward_points, ledger_uuid, created_at
            FROM game_daily_challenge_rewards
            WHERE game_key=? AND challenge_key=? AND user_id=?
            """,
            (game_key, challenge_key, int(user_id)),
        ).fetchone()
        if not row:
            return None
        return {
            "eligible": True,
            "awarded": False,
            "already_claimed": True,
            "reward_points": int(row["reward_points"] or DAILY_CHALLENGE_REWARD_POINTS),
            "ledger_uuid": row["ledger_uuid"],
            "created_at": row["created_at"],
        }

    def award_daily_challenge_reward(conn, *, actor, game_key, difficulty, puzzle_id, score_id, score):
        challenge_key = str(puzzle_id or "").strip()[:64]
        if not challenge_key or not challenge_key.startswith(f"{game_key}-daily-"):
            return None
        existing = daily_challenge_reward_status(conn, game_key, challenge_key, actor["id"])
        if existing:
            return existing
        reward_points = DAILY_CHALLENGE_REWARD_POINTS
        ledger_uuid = None
        wallet = None
        if points_service:
            try:
                result = points_service.rc1_facade().grant_reward(
                    user_id=int(actor["id"]),
                    amount=reward_points,
                    action_type="game_daily_challenge_reward",
                    reference_type="game_daily_challenge",
                    reference_id=f"{game_key}:{challenge_key}",
                    idempotency_key=f"game_daily_reward:{game_key}:{challenge_key}:{actor['id']}",
                    reason=f"{game_key} 每日任務完成獎勵",
                    currency_type=DISPLAY_CURRENCY,
                    public_metadata={
                        "game_key": game_key,
                        "challenge_key": challenge_key,
                        "difficulty": difficulty,
                        "score": int(score or 0),
                    },
                    actor=actor,
                )
                ledger_uuid = result["ledger"]["ledger_uuid"]
                wallet = result.get("wallet")
            except Exception as exc:
                return {
                    "eligible": True,
                    "awarded": False,
                    "reward_points": reward_points,
                    "error": str(exc)[:180],
                }
        now = utc_now()
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO game_daily_challenge_rewards (
                game_key, challenge_key, user_id, score_id, reward_points, ledger_uuid, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (game_key, challenge_key, int(actor["id"]), int(score_id), reward_points, ledger_uuid, now),
        )
        if cur.rowcount <= 0:
            existing = daily_challenge_reward_status(conn, game_key, challenge_key, actor["id"])
            if existing:
                return existing
        return {
            "eligible": True,
            "awarded": True,
            "already_claimed": False,
            "reward_points": reward_points,
            "ledger_uuid": ledger_uuid,
            "wallet": wallet,
            "created_at": now,
        }

    def collect_computer_replay(row, *, winner_color, actor_username):
        try:
            replay = collect_match_replay(
                row,
                winner_color=winner_color,
                source="user_games",
                actor_username=actor_username,
            )
            audit(
                "GAME_CHESS_REPLAY_COLLECTED",
                get_client_ip(),
                user=actor_username,
                success=True,
                ua=get_ua(),
                detail=(
                    f"match_id={row['id']}, replay_id={replay.get('replay_id')}, "
                    f"stored={replay.get('stored')}, suspicious={replay.get('suspicious_flag')}, "
                    f"confidence={replay.get('confidence_score')}"
                ),
            )
            if replay.get("stored"):
                try:
                    maybe_launch_chess_train_pipeline(trigger="live_replay", actor_username=actor_username)
                except Exception as exc:
                    audit(
                        "GAME_CHESS_PIPELINE_AUTORUN_FAILED",
                        get_client_ip(),
                        user=actor_username,
                        success=False,
                        ua=get_ua(),
                        detail=f"match_id={row['id']}, error={type(exc).__name__}: {exc}",
                    )
            return replay
        except Exception as exc:
            audit(
                "GAME_CHESS_REPLAY_COLLECTION_FAILED",
                get_client_ip(),
                user=actor_username,
                success=False,
                ua=get_ua(),
                detail=f"match_id={row['id']}, error={type(exc).__name__}: {exc}",
            )
            return None

    @app.route("/api/games/catalog", methods=["GET"])
    @require_csrf_safe
    def games_catalog():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        return json_resp({
            "ok": True,
            "games": [{
                "key": GAME_KEY,
                "title": "西洋棋",
                "status": "available",
                "supports_invites": True,
                "supports_computer": True,
                "computer_difficulties": computer_difficulty_options(),
            }, {
                "key": "sudoku",
                "title": "數獨",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "minesweeper",
                "title": "踩地雷",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "1a2b",
                "title": "1A2B",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "tetris",
                "title": "俄羅斯方塊",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "real_tetris",
                "title": "真實版俄羅斯方塊",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "space_shooter",
                "title": "宇宙戰機",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "fps_arena",
                "title": "3D 射擊場",
                "status": "available",
                "supports_invites": True,
                "supports_computer": False,
                "multiplayer_modes": [
                    {"key": "coop", "label": "合作破關"},
                    {"key": "pvp", "label": "PvP 對戰"},
                ],
            }, {
                "key": "open_world",
                "title": "都市開放世界",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "racing",
                "title": "街頭賽車",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "bullet_hell",
                "title": "彈幕遊戲",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "stickman_shooter",
                "title": "火柴人橫向射擊",
                "status": "available",
                "supports_invites": True,
                "supports_computer": False,
                "multiplayer_modes": [
                    {"key": "coop", "label": "合作破關"},
                ],
            }, {
                "key": "snake",
                "title": "貪食蛇",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "game_2048",
                "title": "2048",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "brick_breaker",
                "title": "打磚塊",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "rubiks_cube",
                "title": "3D 魔術方塊",
                "status": "available",
                "supports_invites": False,
                "supports_computer": False,
            }, {
                "key": "reversi",
                "title": "黑白棋",
                "status": "available",
                "supports_invites": False,
                "supports_computer": True,
                "computer_difficulties": [
                    {"key": "easy", "label": "簡單"},
                    {"key": "normal", "label": "普通"},
                    {"key": "hard", "label": "困難"},
                ],
            }, {
                "key": "go",
                "title": "圍棋",
                "status": "available",
                "supports_invites": False,
                "supports_computer": True,
                "computer_difficulties": [
                    {"key": "easy", "label": "簡單"},
                    {"key": "normal", "label": "普通"},
                    {"key": "hard", "label": "困難"},
                    {"key": "katago", "label": "KataGo 神經網路"},
                ],
            }, {
                "key": "gomoku",
                "title": "五子棋",
                "status": "available",
                "supports_invites": False,
                "supports_computer": True,
                "computer_difficulties": [
                    {"key": "easy", "label": "簡單"},
                    {"key": "normal", "label": "普通"},
                    {"key": "hard", "label": "困難"},
                ],
            }, {
                "key": "chinese_chess",
                "title": "中國象棋",
                "status": "available",
                "supports_invites": False,
                "supports_computer": True,
                "computer_difficulties": [
                    {"key": "easy", "label": "簡單"},
                    {"key": "normal", "label": "普通"},
                    {"key": "hard", "label": "困難"},
                ],
            }],
        })

    @app.route("/api/games/rubiks_cube/solve", methods=["POST"])
    @require_csrf
    def rubiks_cube_solve():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        data, err, status = parse_json_body()
        if err:
            return err, status
        try:
            max_depth = int(data.get("max_depth") or 24)
        except (TypeError, ValueError):
            max_depth = 24
        try:
            result = solve_rubiks_facelets(str(data.get("facelets") or ""), max_depth=max_depth)
        except RubiksSolverUnavailable as exc:
            return json_resp({"ok": False, "msg": str(exc)}), 503
        except ValueError as exc:
            return json_resp({"ok": False, "msg": str(exc)}), 400
        return json_resp({"ok": True, **result})

    @app.route("/api/games/users", methods=["GET"])
    @require_csrf_safe
    def games_users():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            active_filter = active_user_where(conn, "u")
            actor_id = int(actor["id"])
            friend_expr = "0 AS is_friend"
            params = [actor_id]
            if game_table_exists(conn, "user_friends"):
                friend_expr = """
                    CASE WHEN EXISTS (
                        SELECT 1
                        FROM user_friends f
                        WHERE f.status='accepted'
                          AND (
                            (f.user_id=? AND f.friend_user_id=u.id)
                            OR (f.friend_user_id=? AND f.user_id=u.id)
                          )
                    ) THEN 1 ELSE 0 END AS is_friend
                """
                params = [actor_id, actor_id, actor_id]
            rows = conn.execute(
                f"""
                SELECT
                    u.id,
                    u.username,
                    u.role,
                    {friend_expr}
                FROM users u
                WHERE u.id<>?
                  AND COALESCE(u.role, 'user') NOT IN ('root', 'super_admin')
                  AND {active_filter}
                ORDER BY u.username COLLATE NOCASE
                LIMIT 200
                """,
                tuple(params),
            ).fetchall()
            users = []
            for row in rows:
                item = dict(row)
                item["is_friend"] = bool(item.get("is_friend"))
                users.append(item)
            return json_resp({"ok": True, "users": users})
        finally:
            conn.close()

    @app.route("/api/games/<game_key>/multiplayer", methods=["GET"])
    @require_csrf_safe
    def game_multiplayer_lobby(game_key):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        game_key = normalize_multiplayer_game_key(game_key)
        if not game_key:
            return json_resp({"ok": False, "msg": "這個遊戲不支援多人房間"}), 404
        conn = get_db()
        try:
            ensure_game_schema(conn)
            rooms = conn.execute(
                multiplayer_room_select_sql(
                    """
                    r.game_key=?
                    AND r.status IN ('lobby', 'active')
                    AND (r.host_user_id=? OR r.guest_user_id=?)
                    ORDER BY CASE WHEN r.status='active' THEN 0 ELSE 1 END, r.id DESC
                    LIMIT 40
                    """
                ),
                (game_key, int(actor["id"]), int(actor["id"])),
            ).fetchall()
            invites = conn.execute(
                """
                SELECT i.*, inviter.username AS inviter_username, invitee.username AS invitee_username
                FROM game_multiplayer_invites i
                JOIN users inviter ON inviter.id=i.inviter_user_id
                JOIN users invitee ON invitee.id=i.invitee_user_id
                WHERE i.game_key=?
                  AND (i.inviter_user_id=? OR i.invitee_user_id=?)
                  AND i.status IN ('pending', 'accepted')
                ORDER BY i.id DESC
                LIMIT 60
                """,
                (game_key, int(actor["id"]), int(actor["id"])),
            ).fetchall()
            return json_resp({
                "ok": True,
                "game_key": game_key,
                "modes": [
                    {"key": mode, "label": "合作破關" if mode == "coop" else "PvP 對戰"}
                    for mode in sorted(MULTIPLAYER_MODES_BY_GAME.get(game_key) or set())
                ],
                "rooms": [serialize_multiplayer_room(row, actor["id"]) for row in rooms],
                "invites": [serialize_multiplayer_invite(row) for row in invites],
            })
        finally:
            conn.close()

    @app.route("/api/games/multiplayer/invites/pending", methods=["GET"])
    @require_csrf_safe
    def pending_game_multiplayer_invites():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            rows = conn.execute(
                """
                SELECT
                    i.*,
                    inviter.username AS inviter_username,
                    invitee.username AS invitee_username,
                    r.room_code AS room_code,
                    r.status AS room_status,
                    r.host_user_id AS host_user_id,
                    r.guest_user_id AS guest_user_id
                FROM game_multiplayer_invites i
                JOIN game_multiplayer_rooms r ON r.id=i.room_id
                JOIN users inviter ON inviter.id=i.inviter_user_id
                JOIN users invitee ON invitee.id=i.invitee_user_id
                WHERE i.invitee_user_id=?
                  AND i.status='pending'
                  AND r.status IN ('lobby', 'active')
                ORDER BY i.id DESC
                LIMIT 20
                """,
                (int(actor["id"]),),
            ).fetchall()
            invites = []
            for row in rows:
                invite = serialize_multiplayer_invite(row)
                invite["room"] = {
                    "id": row["room_id"],
                    "room_code": row["room_code"],
                    "status": row["room_status"],
                    "host_user_id": row["host_user_id"],
                    "guest_user_id": row["guest_user_id"],
                }
                invites.append(invite)
            return json_resp({"ok": True, "invites": invites})
        finally:
            conn.close()

    @app.route("/api/games/<game_key>/multiplayer/invites", methods=["POST"])
    @require_csrf
    def create_game_multiplayer_invite(game_key):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        game_key = normalize_multiplayer_game_key(game_key)
        if not game_key:
            return json_resp({"ok": False, "msg": "這個遊戲不支援多人房間"}), 404
        data, err, status = parse_json_body()
        if err:
            return err, status
        mode = normalize_multiplayer_mode(game_key, data.get("mode"))
        if not mode:
            return json_resp({"ok": False, "msg": "這個遊戲不支援所選多人模式"}), 400
        username = str(data.get("opponent_username") or data.get("username") or "").strip()
        if not username:
            return json_resp({"ok": False, "msg": "請選擇要邀請的玩家"}), 400
        conn = get_db()
        try:
            ensure_game_schema(conn)
            active_filter = active_user_where(conn)
            opponent_row = conn.execute(
                f"SELECT id, username FROM users WHERE username=? AND {active_filter}",
                (username,),
            ).fetchone()
            if not opponent_row:
                return json_resp({"ok": False, "msg": "找不到可邀請的玩家"}), 404
            if int(opponent_row["id"]) == int(actor["id"]):
                return json_resp({"ok": False, "msg": "不能邀請自己"}), 400
            if not users_are_game_friends(conn, actor["id"], opponent_row["id"]):
                return json_resp({"ok": False, "msg": "只能邀請已成為好友的玩家"}), 403
            pending = conn.execute(
                """
                SELECT i.id
                FROM game_multiplayer_invites i
                JOIN game_multiplayer_rooms r ON r.id=i.room_id
                WHERE i.game_key=? AND i.mode=? AND i.status='pending'
                  AND r.status IN ('lobby', 'active')
                  AND i.inviter_user_id=? AND i.invitee_user_id=?
                """,
                (game_key, mode, int(actor["id"]), int(opponent_row["id"])),
            ).fetchone()
            if pending:
                return json_resp({"ok": False, "msg": "已經有等待中的多人邀請"}), 409
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            expires = (datetime.now(timezone.utc) + timedelta(hours=8)).replace(microsecond=0).isoformat()
            room_cur = conn.execute(
                """
                INSERT INTO game_multiplayer_rooms (
                    room_code, game_key, mode, status, host_user_id, guest_user_id,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, 'lobby', ?, NULL, ?, ?, ?)
                """,
                (make_multiplayer_room_code(conn), game_key, mode, int(actor["id"]), now, now, expires),
            )
            room_id = room_cur.lastrowid
            invite_cur = conn.execute(
                """
                INSERT INTO game_multiplayer_invites (
                    room_id, game_key, mode, inviter_user_id, invitee_user_id, status,
                    message, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    room_id,
                    game_key,
                    mode,
                    int(actor["id"]),
                    int(opponent_row["id"]),
                    str(data.get("message") or "")[:160],
                    now,
                    now,
                    expires,
                ),
            )
            conn.commit()
            room = multiplayer_room_row(conn, room_id)
            audit(
                "GAME_MULTIPLAYER_INVITE_CREATED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"game={game_key}, mode={mode}, room_id={room_id}, invite_id={invite_cur.lastrowid}",
            )
            return json_resp({
                "ok": True,
                "invite_id": invite_cur.lastrowid,
                "room": serialize_multiplayer_room(room, actor["id"]) if room else None,
            })
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.route("/api/games/multiplayer/invites/<int:invite_id>/<action>", methods=["POST"])
    @require_csrf
    def review_game_multiplayer_invite(invite_id, action):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        if action not in {"accept", "reject", "cancel"}:
            return json_resp({"ok": False, "msg": "不支援的邀請操作"}), 400
        conn = get_db()
        try:
            ensure_game_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            invite = conn.execute("SELECT * FROM game_multiplayer_invites WHERE id=?", (int(invite_id),)).fetchone()
            if not invite:
                conn.rollback()
                return json_resp({"ok": False, "msg": "找不到多人邀請"}), 404
            if invite["status"] != "pending":
                conn.rollback()
                return json_resp({"ok": False, "msg": "邀請已處理"}), 409
            actor_id = int(actor["id"])
            if action == "cancel" and actor_id != int(invite["inviter_user_id"]):
                conn.rollback()
                return json_resp({"ok": False, "msg": "只有邀請者能取消邀請"}), 403
            if action in {"accept", "reject"} and actor_id != int(invite["invitee_user_id"]):
                conn.rollback()
                return json_resp({"ok": False, "msg": "只有受邀者能回覆邀請"}), 403
            room = conn.execute("SELECT * FROM game_multiplayer_rooms WHERE id=?", (int(invite["room_id"]),)).fetchone()
            if not room or room["status"] not in {"lobby", "active"}:
                conn.rollback()
                return json_resp({"ok": False, "msg": "多人房間已失效"}), 409
            now = utc_now()
            new_status = {"accept": "accepted", "reject": "rejected", "cancel": "cancelled"}[action]
            if action == "accept":
                if room["guest_user_id"] and int(room["guest_user_id"]) != actor_id:
                    conn.rollback()
                    return json_resp({"ok": False, "msg": "房間已被其他玩家加入"}), 409
                conn.execute(
                    "UPDATE game_multiplayer_rooms SET guest_user_id=?, updated_at=? WHERE id=?",
                    (actor_id, now, int(invite["room_id"])),
                )
            elif action in {"reject", "cancel"}:
                conn.execute(
                    "UPDATE game_multiplayer_rooms SET status='cancelled', updated_at=?, finished_at=? WHERE id=?",
                    (now, now, int(invite["room_id"])),
                )
            conn.execute(
                "UPDATE game_multiplayer_invites SET status=?, updated_at=? WHERE id=?",
                (new_status, now, int(invite_id)),
            )
            conn.commit()
            refreshed = multiplayer_room_row(conn, invite["room_id"])
            return json_resp({
                "ok": True,
                "room": serialize_multiplayer_room(refreshed, actor["id"]) if refreshed else None,
            })
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.route("/api/games/multiplayer/rooms/<int:room_id>", methods=["GET"])
    @require_csrf_safe
    def game_multiplayer_room_detail(room_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        after_event_id = int(request.args.get("after_event_id") or 0)
        conn = get_db()
        try:
            ensure_game_schema(conn)
            room = multiplayer_room_row(conn, room_id)
            if not room:
                return json_resp({"ok": False, "msg": "找不到多人房間"}), 404
            if not actor_can_access_multiplayer_room(actor, room):
                return json_resp({"ok": False, "msg": "不是這個房間的玩家"}), 403
            return json_resp({"ok": True, **multiplayer_room_payload(conn, room, actor["id"], after_event_id)})
        finally:
            conn.close()

    @app.route("/api/games/multiplayer/rooms/<int:room_id>/start", methods=["POST"])
    @require_csrf
    def start_game_multiplayer_room(room_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            room = multiplayer_room_row(conn, room_id)
            if not room:
                conn.rollback()
                return json_resp({"ok": False, "msg": "找不到多人房間"}), 404
            if not actor_can_access_multiplayer_room(actor, room):
                conn.rollback()
                return json_resp({"ok": False, "msg": "不是這個房間的玩家"}), 403
            if not room["guest_user_id"]:
                conn.rollback()
                return json_resp({"ok": False, "msg": "等待另一位玩家加入"}), 409
            now = utc_now()
            conn.execute(
                """
                UPDATE game_multiplayer_rooms
                SET status='active', started_at=COALESCE(started_at, ?), updated_at=?
                WHERE id=? AND status IN ('lobby', 'active')
                """,
                (now, now, int(room_id)),
            )
            conn.commit()
            refreshed = multiplayer_room_row(conn, room_id)
            return json_resp({"ok": True, **multiplayer_room_payload(conn, refreshed, actor["id"], 0)})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.route("/api/games/multiplayer/rooms/<int:room_id>/state", methods=["POST"])
    @require_csrf
    def update_game_multiplayer_room_state(room_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        data, err, status = parse_json_body()
        if err:
            return err, status
        state_body = data.get("state") if isinstance(data.get("state"), dict) else {}
        after_event_id = int(data.get("after_event_id") or 0)
        raw_events = data.get("events") if isinstance(data.get("events"), list) else []
        allowed_events = {"gunshot", "player_hit", "friendly_fire", "objective", "down", "finish"}
        conn = get_db()
        try:
            ensure_game_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            room = multiplayer_room_row(conn, room_id)
            if not room:
                conn.rollback()
                return json_resp({"ok": False, "msg": "找不到多人房間"}), 404
            if not actor_can_access_multiplayer_room(actor, room):
                conn.rollback()
                return json_resp({"ok": False, "msg": "不是這個房間的玩家"}), 403
            if room["status"] == "finished" or room["status"] == "cancelled":
                conn.rollback()
                return json_resp({"ok": False, "msg": "多人房間已結束"}), 409
            state_json = clamp_multiplayer_payload_json(state_body, "玩家狀態")
            now = utc_now()
            existing_state = conn.execute(
                "SELECT sequence FROM game_multiplayer_player_states WHERE room_id=? AND user_id=?",
                (int(room_id), int(actor["id"])),
            ).fetchone()
            sequence = int(existing_state["sequence"] or 0) + 1 if existing_state else 1
            conn.execute(
                """
                INSERT INTO game_multiplayer_player_states (room_id, user_id, state_json, sequence, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(room_id, user_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    sequence=excluded.sequence,
                    updated_at=excluded.updated_at
                """,
                (int(room_id), int(actor["id"]), state_json, sequence, now),
            )
            if room["guest_user_id"] and room["status"] == "lobby":
                conn.execute(
                    "UPDATE game_multiplayer_rooms SET status='active', started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?",
                    (now, now, int(room_id)),
                )
            else:
                conn.execute("UPDATE game_multiplayer_rooms SET updated_at=? WHERE id=?", (now, int(room_id)))
            participant_ids = {int(room["host_user_id"])}
            if room["guest_user_id"]:
                participant_ids.add(int(room["guest_user_id"]))
            for event in raw_events[:12]:
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type") or event.get("event_type") or "").strip().lower()
                if event_type not in allowed_events:
                    continue
                target_user_id = event.get("target_user_id")
                if target_user_id is not None:
                    try:
                        target_user_id = int(target_user_id)
                    except Exception:
                        continue
                    if target_user_id not in participant_ids:
                        continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                payload_json = clamp_multiplayer_payload_json(payload, "事件資料", max_bytes=2400)
                conn.execute(
                    """
                    INSERT INTO game_multiplayer_events (room_id, sender_user_id, target_user_id, event_type, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (int(room_id), int(actor["id"]), target_user_id, event_type, payload_json, now),
                )
            conn.commit()
            refreshed = multiplayer_room_row(conn, room_id)
            return json_resp({"ok": True, **multiplayer_room_payload(conn, refreshed, actor["id"], after_event_id)})
        except ValueError as exc:
            conn.rollback()
            return json_resp({"ok": False, "msg": str(exc)}), 400
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.route("/api/games/<game_key>/ai-move", methods=["POST"])
    @require_csrf
    def board_game_ai_move(game_key):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        game_key = str(game_key or "").strip().lower()
        if game_key not in BOARD_AI_GAME_KEYS:
            return json_resp({"ok": False, "msg": "不支援的棋類 AI"}), 404
        data, err, status = parse_json_body()
        if err:
            return err, status
        difficulty = str(data.get("difficulty") or "normal").strip().lower()
        if difficulty not in board_ai_difficulties_for_game(game_key):
            return json_resp({"ok": False, "msg": "不支援的 AI 難度"}), 400
        try:
            decision = choose_board_game_ai_move(
                game_key,
                data.get("board"),
                data.get("turn"),
                difficulty,
            )
        except KataGoUnavailable as exc:
            return json_resp({"ok": False, "msg": str(exc)}), 503
        except ValueError as exc:
            return json_resp({"ok": False, "msg": str(exc)}), 400
        return json_resp({"ok": True, **decision})

    @app.route("/api/games/chess/invites", methods=["GET"])
    @require_csrf_safe
    def chess_invites():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            sql = """
                SELECT i.*, inviter.username AS inviter_username, opponent.username AS opponent_username
                FROM game_invites i
                JOIN users inviter ON inviter.id=i.inviter_user_id
                JOIN users opponent ON opponent.id=i.opponent_user_id
                WHERE i.game_key=? AND (i.inviter_user_id=? OR i.opponent_user_id=?)
                ORDER BY i.id DESC
                LIMIT 80
            """
            rows = conn.execute(sql, (GAME_KEY, int(actor["id"]), int(actor["id"]))).fetchall()
            return json_resp({"ok": True, "invites": [serialize_invite(row) for row in rows]})
        finally:
            conn.close()

    @app.route("/api/games/chess/invites", methods=["POST"])
    @require_csrf
    def create_chess_invite():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        data, err, status = parse_json_body()
        if err:
            return err, status
        username = str(data.get("opponent_username") or data.get("username") or "").strip()
        if not username:
            return json_resp({"ok": False, "msg": "請選擇要邀請的玩家"}), 400
        conn = get_db()
        try:
            ensure_game_schema(conn)
            active_filter = active_user_where(conn)
            opponent_row = conn.execute(
                f"SELECT id, username FROM users WHERE username=? AND {active_filter}",
                (username,),
            ).fetchone()
            if not opponent_row:
                return json_resp({"ok": False, "msg": "找不到可邀請的玩家"}), 404
            if int(opponent_row["id"]) == int(actor["id"]):
                return json_resp({"ok": False, "msg": "不能邀請自己"}), 400
            if not users_are_game_friends(conn, actor["id"], opponent_row["id"]):
                return json_resp({"ok": False, "msg": "只能邀請已成為好友的玩家"}), 403
            pending = conn.execute(
                """
                SELECT id FROM game_invites
                WHERE game_key=? AND status='pending' AND inviter_user_id=? AND opponent_user_id=?
                """,
                (GAME_KEY, int(actor["id"]), int(opponent_row["id"])),
            ).fetchone()
            if pending:
                return json_resp({"ok": False, "msg": "已經有等待中的邀請"}), 409
            now = utc_now()
            expires = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat()
            cur = conn.execute(
                """
                INSERT INTO game_invites (game_key, inviter_user_id, opponent_user_id, status, message, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (GAME_KEY, int(actor["id"]), int(opponent_row["id"]), str(data.get("message") or "")[:160], now, now, expires),
            )
            conn.commit()
            audit("GAME_INVITE_CREATED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"invite_id={cur.lastrowid}")
            return json_resp({"ok": True, "invite_id": cur.lastrowid})
        finally:
            conn.close()

    @app.route("/api/games/chess/invites/<int:invite_id>/<action>", methods=["POST"])
    @require_csrf
    def review_chess_invite(invite_id, action):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        if action not in {"accept", "reject", "cancel"}:
            return json_resp({"ok": False, "msg": "不支援的邀請操作"}), 400
        conn = get_db()
        try:
            ensure_game_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            invite = conn.execute("SELECT * FROM game_invites WHERE id=? AND game_key=?", (invite_id, GAME_KEY)).fetchone()
            if not invite:
                conn.rollback()
                return json_resp({"ok": False, "msg": "找不到邀請"}), 404
            if invite["status"] != "pending":
                conn.rollback()
                return json_resp({"ok": False, "msg": "邀請已處理"}), 409
            actor_id = int(actor["id"])
            if action == "cancel" and actor_id != int(invite["inviter_user_id"]):
                conn.rollback()
                return json_resp({"ok": False, "msg": "只有邀請者能取消邀請"}), 403
            if action in {"accept", "reject"} and actor_id != int(invite["opponent_user_id"]):
                conn.rollback()
                return json_resp({"ok": False, "msg": "只有受邀者能回覆邀請"}), 403
            now = utc_now()
            match_id = None
            new_status = {"accept": "accepted", "reject": "rejected", "cancel": "cancelled"}[action]
            if action == "accept":
                inviter_is_white = random.choice([True, False])
                white_user_id = int(invite["inviter_user_id"]) if inviter_is_white else int(invite["opponent_user_id"])
                black_user_id = int(invite["opponent_user_id"]) if inviter_is_white else int(invite["inviter_user_id"])
                cur = conn.execute(
                    """
                    INSERT INTO game_matches (
                        game_key, mode, status, white_user_id, black_user_id, human_side, current_turn,
                        board_json, move_history_json, created_at, updated_at
                    ) VALUES (?, 'pvp', 'active', ?, ?, 'white', 'white', ?, '[]', ?, ?)
                    """,
                    (GAME_KEY, white_user_id, black_user_id, default_board_json(), now, now),
                )
                match_id = cur.lastrowid
            conn.execute(
                "UPDATE game_invites SET status=?, match_id=?, updated_at=? WHERE id=?",
                (new_status, match_id, now, invite_id),
            )
            conn.commit()
            return json_resp({"ok": True, "match_id": match_id})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.route("/api/games/chess/practice", methods=["POST"])
    @require_csrf
    def create_chess_practice():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        data, err, status = parse_json_body()
        if err:
            return err, status
        human_side = str(data.get("side") or data.get("human_side") or "white").strip().lower()
        if human_side not in {"white", "black"}:
            return json_resp({"ok": False, "msg": "請選擇白方或黑方"}), 400
        difficulty = normalize_computer_difficulty(data.get("difficulty") or data.get("computer_difficulty"))
        if difficulty is None:
            return json_resp({"ok": False, "msg": "請選擇有效的電腦難度"}), 400
        computer_config = computer_config_for_new_match(difficulty, data)
        conn = get_db()
        try:
            ensure_game_schema(conn)
            now = utc_now()
            board = initial_board()
            history = []
            current_turn = "white"
            if human_side == "black":
                opening_moves = legal_moves(board, "white")
                if opening_moves:
                    computer_move = choose_computer_move(
                        board,
                        "white",
                        difficulty,
                        learning_store=chess_engine_store,
                        computer_config=computer_config,
                    )
                    applied = validate_move(board, "white", computer_move["from"], computer_move["to"], computer_move.get("promotion"))
                    board = applied["board"]
                    history.append({
                        "by": "white",
                        "from": computer_move["from"],
                        "to": computer_move["to"],
                        "piece": computer_move["piece"],
                        "captured": applied.get("captured"),
                        "promotion": computer_move.get("promotion"),
                        "computer": True,
                        "at": now,
                    })
                    current_turn = "black"
            cur = conn.execute(
                """
                INSERT INTO game_matches (
                    game_key, mode, status, white_user_id, black_user_id, human_side, computer_difficulty, computer_config_json, current_turn,
                    board_json, move_history_json, created_at, updated_at
                ) VALUES (?, 'computer', 'active', ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    GAME_KEY,
                    int(actor["id"]),
                    human_side,
                    difficulty,
                    json.dumps(computer_config, ensure_ascii=False, sort_keys=True),
                    current_turn,
                    json.dumps(board, ensure_ascii=False, sort_keys=True),
                    json.dumps(history, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
            return json_resp({"ok": True, "match_id": cur.lastrowid})
        finally:
            conn.close()

    @app.route("/api/games/chess/challenge", methods=["POST"])
    @require_csrf
    def create_chess_challenge():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        data, err, status = parse_json_body()
        if err:
            return err, status
        preset_key = str(data.get("preset") or "rook_endgame").strip().lower()
        presets = {
            "rook_endgame": {
                "title": "車王殘局",
                "board": {"e1": "K", "a1": "R", "e8": "k"},
                "turn": "white",
                "hint": "用車切斷黑王，再用白王靠近逼到邊線。",
            },
            "queen_mate": {
                "title": "后翼將殺網",
                "board": {"g1": "K", "h5": "Q", "e8": "k", "f7": "p", "g7": "p", "h7": "p"},
                "turn": "white",
                "hint": "先找王旁弱點，避免只貪吃子。",
            },
            "knight_fork": {
                "title": "馬叉戰術",
                "board": {"e1": "K", "f3": "N", "g8": "k", "d4": "q", "h4": "r", "e7": "p"},
                "turn": "white",
                "hint": "尋找帶將軍或攻擊重子的馬跳點。",
            },
        }
        preset = presets.get(preset_key) or presets["rook_endgame"]
        conn = get_db()
        try:
            ensure_game_schema(conn)
            now = utc_now()
            cur = conn.execute(
                """
                INSERT INTO game_matches (
                    game_key, mode, status, white_user_id, black_user_id, human_side, computer_difficulty, current_turn,
                    board_json, move_history_json, created_at, updated_at
                ) VALUES (?, 'computer', 'active', ?, NULL, 'white', 'hard', ?, ?, ?, ?, ?)
                """,
                (
                    GAME_KEY,
                    int(actor["id"]),
                    preset["turn"],
                    json.dumps(preset["board"], ensure_ascii=False, sort_keys=True),
                    "[]",
                    now,
                    now,
                ),
            )
            conn.commit()
            return json_resp({"ok": True, "match_id": cur.lastrowid, "preset": preset_key, "title": preset["title"], "hint": preset["hint"]})
        finally:
            conn.close()

    @app.route("/api/games/chess/matches", methods=["GET"])
    @require_csrf_safe
    def chess_matches():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            rows = conn.execute(
                match_select_sql(
                    """
                    m.game_key=?
                    AND (
                        (m.white_user_id=? AND m.white_deleted_at IS NULL)
                        OR (m.black_user_id=? AND m.black_deleted_at IS NULL)
                    )
                    ORDER BY CASE WHEN m.status='active' THEN 0 ELSE 1 END, m.id DESC
                    LIMIT 80
                    """
                ),
                (GAME_KEY, int(actor["id"]), int(actor["id"])),
            ).fetchall()
            return json_resp({"ok": True, "matches": [serialize_match(row, actor["id"]) for row in rows]})
        finally:
            conn.close()

    @app.route("/api/games/chess/matches/<int:match_id>", methods=["GET"])
    @require_csrf_safe
    def chess_match_detail(match_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            row = match_row(conn, match_id)
            if not row:
                return json_resp({"ok": False, "msg": "找不到棋局"}), 404
            if not actor_can_play(actor, row):
                return json_resp({"ok": False, "msg": "不是這局的玩家"}), 403
            return json_resp({"ok": True, "match": serialize_match(row, actor["id"])})
        finally:
            conn.close()

    @app.route("/api/games/chess/matches/<int:match_id>", methods=["DELETE"])
    @require_csrf
    def chess_match_delete(match_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            row = match_row(conn, match_id)
            if not row:
                return json_resp({"ok": False, "msg": "找不到棋局"}), 404
            side = actor_color(actor, row)
            column = actor_delete_column(actor, row)
            if not side or not column:
                return json_resp({"ok": False, "msg": "不是這局的玩家"}), 403
            if row["status"] == "active":
                return json_resp({"ok": False, "msg": "進行中的棋局不能刪除，請先認輸或完成棋局"}), 409
            now = utc_now()
            conn.execute(f"UPDATE game_matches SET {column}=?, updated_at=? WHERE id=?", (now, now, match_id))
            conn.commit()
            audit(
                "GAME_MATCH_DELETED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"match_id={match_id}, side={side}, status={row['status']}",
            )
            return json_resp({"ok": True, "msg": "棋局已從你的列表移除"})
        finally:
            conn.close()

    @app.route("/api/games/chess/matches/<int:match_id>/move", methods=["POST"])
    @require_csrf
    def chess_match_move(match_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        data, err, status = parse_json_body()
        if err:
            return err, status
        from_square = str(data.get("from") or "").strip().lower()
        to_square = str(data.get("to") or "").strip().lower()
        conn = get_db()
        try:
            ensure_game_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = match_row(conn, match_id)
            if not row:
                conn.rollback()
                return json_resp({"ok": False, "msg": "找不到棋局"}), 404
            if row["status"] != "active":
                conn.rollback()
                return json_resp({"ok": False, "msg": "棋局已結束"}), 409
            side = actor_color(actor, row)
            if not side:
                conn.rollback()
                return json_resp({"ok": False, "msg": "不是這局的玩家"}), 403
            if side != row["current_turn"]:
                conn.rollback()
                return json_resp({"ok": False, "msg": "還沒輪到你"}), 409
            board = load_board(row)
            try:
                move = validate_move(board, side, from_square, to_square, data.get("promotion"))
            except ValueError as exc:
                conn.rollback()
                return json_resp({"ok": False, "msg": str(exc)}), 400
            history = load_history(row)
            now = utc_now()
            history.append({
                "by": side,
                "from": from_square,
                "to": to_square,
                "piece": move["piece"],
                "captured": move.get("captured"),
                "promotion": move.get("promotion"),
                "at": now,
            })
            board = move["board"]
            next_turn = opponent(side)
            status_info = game_status(board, next_turn)
            human_side = row["human_side"] if "human_side" in row.keys() else "white"
            computer_difficulty = row["computer_difficulty"] if "computer_difficulty" in row.keys() else "normal"
            computer_config = load_computer_config(row)
            if row["mode"] == "computer" and status_info["status"] == "active" and next_turn != human_side:
                computer_side = next_turn
                computer_move = choose_computer_move(
                    board,
                    computer_side,
                    computer_difficulty,
                    learning_store=chess_engine_store,
                    move_history=history,
                    computer_config=computer_config,
                )
                if computer_move:
                    computer_applied = validate_move(board, computer_side, computer_move["from"], computer_move["to"], computer_move.get("promotion"))
                    board = computer_applied["board"]
                    history.append({
                        "by": computer_side,
                        "from": computer_move["from"],
                        "to": computer_move["to"],
                        "piece": computer_move["piece"],
                        "captured": computer_applied.get("captured"),
                        "promotion": computer_move.get("promotion"),
                        "computer": True,
                        "at": utc_now(),
                    })
                    next_turn = human_side
                    status_info = game_status(board, next_turn)
            conn.execute(
                """
                UPDATE game_matches
                SET board_json=?, move_history_json=?, current_turn=?, draw_offer_by_user_id=NULL, draw_offer_at=NULL, updated_at=?
                WHERE id=?
                """,
                (json.dumps(board, ensure_ascii=False, sort_keys=True), json.dumps(history, ensure_ascii=False), next_turn, now, match_id),
            )
            if status_info["status"] == "finished":
                match_before_finish = conn.execute("SELECT * FROM game_matches WHERE id=?", (match_id,)).fetchone()
                finish_match(conn, match_before_finish, status_info, utc_now())
                final_row = conn.execute("SELECT * FROM game_matches WHERE id=?", (match_id,)).fetchone()
                collect_computer_replay(
                    final_row,
                    winner_color=status_info.get("winner_color"),
                    actor_username=actor["username"],
                )
            conn.commit()
            refreshed = match_row(conn, match_id)
            return json_resp({"ok": True, "match": serialize_match(refreshed, actor["id"])})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.route("/api/games/chess/matches/<int:match_id>/offer-draw", methods=["POST"])
    @require_csrf
    def chess_match_offer_draw(match_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = match_row(conn, match_id)
            if not row:
                conn.rollback()
                return json_resp({"ok": False, "msg": "找不到棋局"}), 404
            if row["status"] != "active":
                conn.rollback()
                return json_resp({"ok": False, "msg": "棋局已結束"}), 409
            if row["mode"] != "pvp":
                conn.rollback()
                return json_resp({"ok": False, "msg": "只有玩家對戰可以提和"}), 409
            side = actor_color(actor, row)
            if not side:
                conn.rollback()
                return json_resp({"ok": False, "msg": "不是這局的玩家"}), 403
            existing_offer = row["draw_offer_by_user_id"] if "draw_offer_by_user_id" in row.keys() else None
            if existing_offer:
                conn.rollback()
                if int(existing_offer) == int(actor["id"]):
                    return json_resp({"ok": False, "msg": "你已經提和，等待對方回覆"}), 409
                return json_resp({"ok": False, "msg": "對方已提和，請先接受或拒絕"}), 409
            now = utc_now()
            conn.execute(
                """
                UPDATE game_matches
                SET draw_offer_by_user_id=?, draw_offer_at=?, updated_at=?
                WHERE id=? AND status='active'
                """,
                (int(actor["id"]), now, now, match_id),
            )
            conn.commit()
            refreshed = match_row(conn, match_id)
            return json_resp({"ok": True, "match": serialize_match(refreshed, actor["id"])})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.route("/api/games/chess/matches/<int:match_id>/respond-draw", methods=["POST"])
    @require_csrf
    def chess_match_respond_draw(match_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        data, err, status = parse_json_body()
        if err:
            return err, status
        action = str(data.get("action") or "").strip().lower()
        if action not in {"accept", "reject"}:
            return json_resp({"ok": False, "msg": "不支援的和棋操作"}), 400
        conn = get_db()
        try:
            ensure_game_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = match_row(conn, match_id)
            if not row:
                conn.rollback()
                return json_resp({"ok": False, "msg": "找不到棋局"}), 404
            if row["status"] != "active":
                conn.rollback()
                return json_resp({"ok": False, "msg": "棋局已結束"}), 409
            side = actor_color(actor, row)
            if not side:
                conn.rollback()
                return json_resp({"ok": False, "msg": "不是這局的玩家"}), 403
            offerer_id = row["draw_offer_by_user_id"] if "draw_offer_by_user_id" in row.keys() else None
            if not offerer_id:
                conn.rollback()
                return json_resp({"ok": False, "msg": "目前沒有待回覆的提和"}), 409
            if int(offerer_id) == int(actor["id"]):
                conn.rollback()
                return json_resp({"ok": False, "msg": "不能回覆自己提出的和棋"}), 409
            now = utc_now()
            if action == "accept":
                finish_match(conn, row, {"status": "finished", "winner_color": None, "reason": "agreed_draw"}, now)
            else:
                conn.execute(
                    """
                    UPDATE game_matches
                    SET draw_offer_by_user_id=NULL, draw_offer_at=NULL, updated_at=?
                    WHERE id=? AND status='active'
                    """,
                    (now, match_id),
                )
            conn.commit()
            refreshed = match_row(conn, match_id)
            return json_resp({"ok": True, "match": serialize_match(refreshed, actor["id"])})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.route("/api/games/chess/matches/<int:match_id>/resign", methods=["POST"])
    @require_csrf
    def chess_match_resign(match_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            row = match_row(conn, match_id)
            if not row:
                return json_resp({"ok": False, "msg": "找不到棋局"}), 404
            if row["status"] != "active":
                return json_resp({"ok": False, "msg": "這局已經結束"}), 409
            side = actor_color(actor, row)
            if not side:
                return json_resp({"ok": False, "msg": "不是這局的玩家"}), 403
            winner = row["black_user_id"] if side == "white" else row["white_user_id"]
            if row["mode"] == "computer":
                winner = None
            now = utc_now()
            conn.execute(
                """
                UPDATE game_matches
                SET status='finished', winner_user_id=?, result_reason='resign', leaderboard_week=?, finished_at=?, updated_at=?
                WHERE id=? AND status='active'
                """,
                (winner, current_week_key(), now, now, match_id),
            )
            final_row = conn.execute("SELECT * FROM game_matches WHERE id=?", (match_id,)).fetchone()
            collect_computer_replay(
                final_row,
                winner_color=opponent(side),
                actor_username=actor["username"],
            )
            conn.commit()
            refreshed = match_row(conn, match_id)
            return json_resp({"ok": True, "match": serialize_match(refreshed, actor["id"])})
        finally:
            conn.close()

    @app.route("/api/games/chess/matches/<int:match_id>/claim-draw", methods=["POST"])
    @require_csrf
    def chess_match_claim_draw(match_id):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        conn = get_db()
        try:
            ensure_game_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = match_row(conn, match_id)
            if not row:
                conn.rollback()
                return json_resp({"ok": False, "msg": "找不到棋局"}), 404
            if row["status"] != "active":
                conn.rollback()
                return json_resp({"ok": False, "msg": "棋局已結束"}), 409
            side = actor_color(actor, row)
            if not side:
                conn.rollback()
                return json_resp({"ok": False, "msg": "不是這局的玩家"}), 403
            if side != row["current_turn"]:
                conn.rollback()
                return json_resp({"ok": False, "msg": "還沒輪到你"}), 409
            board = load_board(row)
            history = load_history(row)
            claim = draw_claim_status(board, row["current_turn"], move_history=history)
            if not claim["can_claim"]:
                conn.rollback()
                return json_resp({"ok": False, "msg": "目前不能申請和棋"}), 409
            reason = "threefold_repetition" if "threefold_repetition" in claim["reasons"] else "fifty_moves"
            finish_match(conn, row, {"status": "finished", "winner_color": None, "reason": reason}, utc_now())
            conn.commit()
            refreshed = match_row(conn, match_id)
            return json_resp({"ok": True, "match": serialize_match(refreshed, actor["id"])})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.route("/api/games/chess/leaderboard", methods=["GET"])
    @require_csrf_safe
    def chess_leaderboard():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        week = str(request.args.get("week") or current_week_key()).strip()
        now = datetime.now(timezone.utc)
        auto_awarded = []
        if not request.args.get("week") and now.weekday() >= 5:
            auto_awarded = award_weekly_rewards(week, actor=None)
        conn = get_db()
        try:
            ensure_game_schema(conn)
            rows = leaderboard_rows(conn, week)
            rewards = conn.execute(
                """
                SELECT r.*, u.username
                FROM game_leaderboard_rewards r
                JOIN users u ON u.id=r.user_id
                WHERE r.game_key=? AND r.week_key=?
                ORDER BY r.rank ASC
                """,
                (GAME_KEY, week),
            ).fetchall()
            return json_resp({
                "ok": True,
                "week": week,
                "reward_points": list(WEEKLY_REWARDS),
                "leaderboard": rows,
                "rewards": [dict(row) for row in rewards],
                "auto_awarded": auto_awarded,
            })
        finally:
            conn.close()

    @app.route("/api/games/<game_key>/solo-leaderboard", methods=["GET"])
    @require_csrf_safe
    def solo_game_leaderboard(game_key):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        game_key = str(game_key or "").strip().lower()
        if game_key not in SOLO_GAME_KEYS:
            return json_resp({"ok": False, "msg": "不支援的單人遊戲"}), 404
        week = str(request.args.get("week") or current_week_key()).strip()
        puzzle_id = str(request.args.get("puzzle_id") or "").strip()[:64]
        raw_difficulty = request.args.get("difficulty")
        difficulty = str(raw_difficulty or ("easy" if game_key == "minesweeper" else "standard")).strip().lower()
        if puzzle_id and raw_difficulty is None:
            difficulty = ""
        if difficulty and game_key == "minesweeper" and difficulty not in MINESWEEPER_DIFFICULTIES and not difficulty.startswith("daily-"):
            return json_resp({"ok": False, "msg": "不支援的難度"}), 400
        if difficulty and game_key in {"sudoku", "1a2b"} and not difficulty.startswith("daily-"):
            difficulty = "standard"
        conn = get_db()
        try:
            ensure_game_schema(conn)
            if game_key in SCORE_RANKED_SOLO_GAMES:
                rank_mode = "score_desc"
            else:
                rank_mode = "guesses_then_time" if game_key == "1a2b" else "time_asc"
            return json_resp({
                "ok": True,
                "game_key": game_key,
                "week": week,
                "difficulty": difficulty or "any",
                "puzzle_id": puzzle_id,
                "rank_mode": rank_mode,
                "leaderboard": solo_leaderboard_rows(conn, game_key, week, difficulty or None, puzzle_id=puzzle_id or None),
            })
        finally:
            conn.close()

    @app.route("/api/games/<game_key>/solo-scores", methods=["POST"])
    @require_csrf
    def submit_solo_game_score(game_key):
        actor, err, status = actor_or_401()
        if err:
            return err, status
        game_key = str(game_key or "").strip().lower()
        if game_key not in SOLO_GAME_KEYS:
            return json_resp({"ok": False, "msg": "不支援的單人遊戲"}), 404
        data, err, status = parse_json_body()
        if err:
            return err, status
        compact_response = compact_response_requested(data)
        try:
            raw_elapsed_ms = int(data.get("raw_elapsed_ms") or data.get("elapsed_ms") or 0)
            penalty_seconds = int(data.get("penalty_seconds") or 0)
            elapsed_ms = int(data.get("elapsed_ms") or 0)
        except Exception:
            return json_resp({"ok": False, "msg": "成績格式錯誤"}), 400
        if raw_elapsed_ms <= 0 or elapsed_ms <= 0 or penalty_seconds < 0:
            return json_resp({"ok": False, "msg": "成績時間不可小於等於 0"}), 400
        if elapsed_ms < raw_elapsed_ms or elapsed_ms != raw_elapsed_ms + penalty_seconds * 1000:
            return json_resp({"ok": False, "msg": "成績時間與加時不一致"}), 400
        if elapsed_ms > 24 * 60 * 60 * 1000 or penalty_seconds > 3600:
            return json_resp({"ok": False, "msg": "成績時間超出合理範圍"}), 400
        difficulty = str(data.get("difficulty") or ("easy" if game_key == "minesweeper" else "standard")).strip().lower()
        if game_key == "minesweeper" and difficulty not in MINESWEEPER_DIFFICULTIES and not difficulty.startswith("daily-"):
            return json_resp({"ok": False, "msg": "不支援的難度"}), 400
        if game_key in {"sudoku", "1a2b"} and not difficulty.startswith("daily-"):
            difficulty = "standard"
        try:
            guess_count = int(data.get("guess_count") or 0)
            score = int(data.get("score") or 0)
        except Exception:
            return json_resp({"ok": False, "msg": "成績格式錯誤"}), 400
        if game_key == "1a2b":
            if guess_count <= 0 or guess_count > 1000:
                return json_resp({"ok": False, "msg": "猜測次數超出合理範圍"}), 400
            if elapsed_ms > 5 * 60 * 1000:
                return json_resp({
                    "ok": True,
                    "ranked": False,
                    "msg": "1A2B 超過 5 分鐘完成，不列入排行榜",
                    "leaderboard": [],
                })
        elif guess_count < 0:
            return json_resp({"ok": False, "msg": "猜測次數格式錯誤"}), 400
        if game_key in SCORE_RANKED_SOLO_GAMES:
            if score <= 0 or score > 1_000_000_000:
                return json_resp({"ok": False, "msg": "分數超出合理範圍"}), 400
        elif score < 0:
            return json_resp({"ok": False, "msg": "分數格式錯誤"}), 400
        puzzle_id = str(data.get("puzzle_id") or "")[:64]
        week = current_week_key()
        now = utc_now()
        conn = get_db()
        try:
            ensure_game_schema(conn)
            cur = conn.execute(
                """
                INSERT INTO game_solo_scores (
                    game_key, user_id, week_key, difficulty, puzzle_id,
                    score, guess_count, raw_elapsed_ms, penalty_seconds, elapsed_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (game_key, int(actor["id"]), week, difficulty, puzzle_id, score, guess_count, raw_elapsed_ms, penalty_seconds, elapsed_ms, now),
            )
            score_id = cur.lastrowid
            conn.commit()
            daily_reward = award_daily_challenge_reward(
                conn,
                actor=actor,
                game_key=game_key,
                difficulty=difficulty,
                puzzle_id=puzzle_id,
                score_id=score_id,
                score=score,
            )
            conn.commit()
            audit(
                "GAME_SOLO_SCORE_SUBMITTED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"game_key={game_key},score_id={score_id},score={score},elapsed_ms={elapsed_ms},penalty_seconds={penalty_seconds},guess_count={guess_count}",
            )
            return json_resp({
                "ok": True,
                "score_id": score_id,
                "week": week,
                "daily_reward": daily_reward,
                "leaderboard": [] if compact_response else solo_leaderboard_rows(conn, game_key, week, difficulty),
                "compact": compact_response,
            })
        finally:
            conn.close()

    @app.route("/api/root/games/chess/weekly-rewards/award", methods=["POST"])
    @require_csrf
    def award_chess_weekly_rewards():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        if actor["username"] != "root":
            return json_resp({"ok": False, "msg": "只有 root 可執行此操作"}), 403
        data, err, status = parse_json_body()
        if err:
            return err, status
        week = str(data.get("week") or current_week_key()).strip()
        awarded = award_weekly_rewards(week, actor=actor)
        audit("GAME_WEEKLY_REWARDS_AWARDED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"week={week}")
        return json_resp({"ok": True, "week": week, "awarded": awarded})

    @app.route("/api/root/games/chess/warm-start", methods=["POST"])
    @require_csrf
    def chess_warm_start():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        if actor["username"] != "root":
            return json_resp({"ok": False, "msg": "只有 root 可執行此操作"}), 403
        return json_resp(ensure_warm_start_chess_environment())

    @app.route("/api/root/games/chess/engines/dashboard", methods=["GET"])
    @require_csrf_safe
    def chess_engines_dashboard():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        if actor["username"] != "root":
            return json_resp({"ok": False, "msg": "只有 root 可查看此資訊"}), 403
        return json_resp(build_chess_engine_dashboard())

    @app.route("/api/root/games/chess/promotion/status", methods=["GET"])
    @require_csrf_safe
    def chess_promotion_status():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        if actor["username"] != "root":
            return json_resp({"ok": False, "msg": "只有 root 可查看此資訊"}), 403
        return json_resp({"ok": True, "promotion": promotion_status_summary()})

    @app.route("/api/root/games/chess/promotion/stage", methods=["POST"])
    @require_csrf
    def chess_promotion_stage():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        if actor["username"] != "root":
            return json_resp({"ok": False, "msg": "只有 root 可執行此操作"}), 403
        data, err, status = parse_json_body()
        if err:
            return err, status
        try:
            result = stage_candidate_model(
                engine=str(data.get("engine") or "").strip(),
                source_path=data.get("source_path"),
                benchmark_report_path=data.get("benchmark_report_path"),
            )
        except Exception as exc:
            return json_resp({"ok": False, "msg": str(exc)}), 400
        return json_resp(result)

    @app.route("/api/root/games/chess/promotion/promote", methods=["POST"])
    @require_csrf
    def chess_promotion_promote():
        actor, err, status = actor_or_401()
        if err:
            return err, status
        if actor["username"] != "root":
            return json_resp({"ok": False, "msg": "只有 root 可執行此操作"}), 403
        data, err, status = parse_json_body()
        if err:
            return err, status
        try:
            result = promote_candidate_model(
                engine=str(data.get("engine") or "").strip(),
                benchmark_report_path=data.get("benchmark_report_path"),
            )
        except Exception as exc:
            return json_resp({"ok": False, "msg": str(exc)}), 400
        return json_resp(result)


def leaderboard_rows(conn, week):
    rows = conn.execute(
        """
        SELECT u.id AS user_id,
               u.username AS username,
               SUM(CASE
                    WHEN m.winner_user_id=u.id THEN 3
                    WHEN m.winner_user_id IS NULL THEN 1
                    ELSE 0
               END) AS score,
               SUM(CASE WHEN m.winner_user_id=u.id THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN m.winner_user_id IS NULL THEN 1 ELSE 0 END) AS draws,
               SUM(CASE WHEN m.winner_user_id IS NOT NULL AND m.winner_user_id<>u.id THEN 1 ELSE 0 END) AS losses,
               COUNT(*) AS games
        FROM game_matches m
        JOIN users u ON u.id IN (m.white_user_id, m.black_user_id)
        WHERE m.game_key=? AND m.mode='pvp' AND m.status='finished' AND m.leaderboard_week=?
        GROUP BY u.id, u.username
        ORDER BY score DESC, wins DESC, games ASC, u.username COLLATE NOCASE ASC
        LIMIT 50
        """,
        (GAME_KEY, week),
    ).fetchall()
    return [{**dict(row), "rank": index + 1} for index, row in enumerate(rows)]


def solo_leaderboard_rows(conn, game_key, week, difficulty, puzzle_id=None):
    best_order = "s2.elapsed_ms ASC, s2.created_at ASC, s2.id ASC"
    final_order = "best.elapsed_ms ASC, attempts.attempts ASC, u.username COLLATE NOCASE ASC"
    if game_key == "1a2b":
        best_order = "CASE WHEN s2.guess_count > 0 THEN s2.guess_count ELSE 999999 END ASC, s2.elapsed_ms ASC, s2.created_at ASC, s2.id ASC"
        final_order = "CASE WHEN best.guess_count > 0 THEN best.guess_count ELSE 999999 END ASC, best.elapsed_ms ASC, u.username COLLATE NOCASE ASC"
    elif game_key in SCORE_RANKED_SOLO_GAMES:
        best_order = "s2.score DESC, s2.elapsed_ms ASC, s2.created_at ASC, s2.id ASC"
        final_order = "best.score DESC, best.elapsed_ms ASC, u.username COLLATE NOCASE ASC"
    puzzle_filter = " AND s2.puzzle_id=?" if puzzle_id else ""
    attempt_puzzle_filter = " AND puzzle_id=?" if puzzle_id else ""
    outer_puzzle_filter = " AND s.puzzle_id=?" if puzzle_id else ""
    best_difficulty_filter = " AND s2.difficulty=s.difficulty" if difficulty else ""
    attempt_difficulty_filter = " AND difficulty=?" if difficulty else ""
    outer_difficulty_filter = " AND s.difficulty=?" if difficulty else ""
    params = []
    if puzzle_id:
        params.append(puzzle_id)
    params.extend([game_key, week])
    if difficulty:
        params.append(difficulty)
    if puzzle_id:
        params.append(puzzle_id)
    params.extend([game_key, week])
    if difficulty:
        params.append(difficulty)
    if puzzle_id:
        params.append(puzzle_id)
    rows = conn.execute(
        f"""
        SELECT best.user_id,
               u.username,
               best.score,
               best.elapsed_ms,
               best.raw_elapsed_ms,
               best.penalty_seconds,
               best.guess_count,
               attempts.attempts,
               best.created_at AS latest_at
        FROM game_solo_scores s
        JOIN game_solo_scores best ON best.id=(
            SELECT s2.id
            FROM game_solo_scores s2
            WHERE s2.game_key=s.game_key
              AND s2.week_key=s.week_key
              AND s2.user_id=s.user_id
              {best_difficulty_filter}
              {puzzle_filter}
            ORDER BY {best_order}
            LIMIT 1
        )
        JOIN (
            SELECT user_id, COUNT(*) AS attempts
            FROM game_solo_scores
            WHERE game_key=? AND week_key=?{attempt_difficulty_filter}{attempt_puzzle_filter}
            GROUP BY user_id
        ) attempts ON attempts.user_id=best.user_id
        JOIN users u ON u.id=best.user_id
        WHERE s.game_key=? AND s.week_key=?{outer_difficulty_filter}{outer_puzzle_filter}
        GROUP BY best.id, best.user_id, u.username, best.score, best.elapsed_ms, best.raw_elapsed_ms, best.penalty_seconds, best.guess_count, attempts.attempts, best.created_at
        ORDER BY {final_order}
        LIMIT 50
        """,
        tuple(params),
    ).fetchall()
    return [{**dict(row), "rank": index + 1} for index, row in enumerate(rows)]
