import json
from pathlib import Path

import chess

from services.games import chess_dl as chess_dl_service
from services.games import chess_pv as chess_pv_service
import services.games.self_play_training as self_play_training_service
from services.games.chess_search import TranspositionTable, ZobristHasher, search_best_move
from services.games.chess_dl import bundled_chess_dl_model_path, default_chess_dl_model_path
from services.games.chess_engine import ChessExperimentStore, bundled_chess_engine_db_path, default_chess_engine_db_path
from services.games.chess_nn import bundled_chess_nn_model_path, default_chess_nn_model_path
from services.games.chess_pv import bundled_chess_pv_model_path, default_chess_pv_model_path
from services.games.self_play_training import (
    TEACHER_DIFFICULTY,
    choose_teacher_move,
    default_training_report_dir,
    run_post_training_smoke_evaluation,
    run_round_robin_benchmark,
    run_training_session,
    write_training_report,
)
from services.server.runtime import default_runtime_root_path


def _fast_legal_training_move(_board_state, side):
    board = chess.Board(str(_board_state.get("__fen__") or chess.STARTING_FEN))
    if board.turn != (chess.WHITE if side == "white" else chess.BLACK):
        board.turn = chess.WHITE if side == "white" else chess.BLACK
    legal = sorted(board.legal_moves, key=lambda move: move.uci())
    if not legal:
        return None
    move = legal[0]
    piece = board.piece_at(move.from_square)
    captured = board.piece_at(move.to_square)
    return {
        "from": chess.square_name(move.from_square),
        "to": chess.square_name(move.to_square),
        "piece": piece.symbol() if piece else "",
        "captured": captured.symbol() if captured else None,
        "promotion": chess.piece_symbol(move.promotion) if move.promotion else None,
    }


def test_teacher_engine_finds_mate_in_one():
    board = {"__fen__": "6k1/5Q2/6K1/8/8/8/8/8 w - - 0 1"}
    move = choose_teacher_move({"__fen__": "6k1/5Q2/6K1/8/8/8/8/8 w - - 0 1"}, "white", depth=2)
    board_obj = chess.Board(board["__fen__"])
    mating_moves = set()
    for legal in board_obj.legal_moves:
        board_after = board_obj.copy(stack=False)
        board_after.push(legal)
        if board_after.is_checkmate():
            mating_moves.add(legal.uci())

    assert move is not None
    chosen_uci = f"{move['from']}{move['to']}{move.get('promotion') or ''}"
    assert chosen_uci in mating_moves


def test_teacher_engine_avoids_early_rook_shuffle_after_edge_pawn():
    board = chess.Board()
    for uci in ("a2a4", "g8f6"):
        board.push(chess.Move.from_uci(uci))

    move = choose_teacher_move({"__fen__": board.fen()}, "white", depth=2)

    assert move is not None
    assert move["from"] != "a1"


def test_shared_search_iterative_deepening_uses_tt_and_finds_mate():
    board = chess.Board("6k1/5Q2/6K1/8/8/8/8/8 w - - 0 1")
    table = TranspositionTable(max_entries=256)

    result = search_best_move(
        board,
        max_depth=2,
        evaluate=lambda current: 0 if not current.is_checkmate() else (-10**7 if current.turn == chess.WHITE else 10**7),
        hasher=ZobristHasher(seed=20260519),
        transposition=table,
    )

    assert result.best_move is not None
    assert result.best_move in board.legal_moves
    assert result.score > 0
    assert result.depth == 2
    assert result.stats.tt_hits >= 1
    assert result.stats.tt_stores >= 1
    assert result.stats.history_updates >= 1


def test_training_session_updates_runtime_db_and_nn_model(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    experiment_db = runtime_dir / "database" / "chess_experiment.db"
    experiment_nn = runtime_dir / "games" / "models" / "chess_experiment_2_nn.json"
    experiment_dl = runtime_dir / "games" / "models" / "chess_experiment_3_dl.json"
    experiment_pv = runtime_dir / "games" / "models" / "chess_experiment_4_pv.json"
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_DB_PATH", str(experiment_db))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_NN_MODEL_PATH", str(experiment_nn))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_DL_MODEL_PATH", str(experiment_dl))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_PV_MODEL_PATH", str(experiment_pv))
    monkeypatch.setattr(self_play_training_service, "_choose_training_move", lambda board_state, side, *_args, **_kwargs: _fast_legal_training_move(board_state, side))

    summary = run_training_session(
        exp1_teacher_games=1,
        exp2_teacher_games=1,
        exp3_teacher_games=1,
        exp4_teacher_games=1,
        hard_exp1_games=1,
        hard_exp2_games=1,
        hard_exp3_games=1,
        hard_exp4_games=1,
        cross_games=1,
        cross_exp1_exp3_games=1,
        cross_exp2_exp3_games=1,
        cross_exp1_exp4_games=1,
        cross_exp2_exp4_games=1,
        cross_exp3_exp4_games=1,
        teacher_depth=1,
        max_plies=12,
        student_exploration_rate=1.0,
        seed=7,
        store=ChessExperimentStore(experiment_db),
        nn_model_path=experiment_nn,
        dl_model_path=experiment_dl,
        pv_model_path=experiment_pv,
    )

    assert summary["games_played"] == 14
    assert summary["experiment_db_path"] == str(experiment_db)
    assert summary["experiment_2_nn_model_path"] == str(experiment_nn)
    assert summary["experiment_3_dl_model_path"] == str(experiment_dl)
    assert summary["experiment_4_pv_model_path"] == str(experiment_pv)
    assert experiment_db.exists()
    assert experiment_nn.exists()
    assert experiment_dl.exists()
    assert experiment_pv.exists()
    assert any(match["white_engine"] == TEACHER_DIFFICULTY or match["black_engine"] == TEACHER_DIFFICULTY for match in summary["matches"])
    assert any(match["white_engine"] == "hard" or match["black_engine"] == "hard" for match in summary["matches"])
    assert summary["requested_games"]["hard_vs_exp1"] == 1
    assert summary["requested_games"]["hard_vs_exp2"] == 1
    assert summary["requested_games"]["hard_vs_exp3"] == 1
    assert summary["requested_games"]["hard_vs_exp4"] == 1
    assert summary["updates"]["teacher_distillation_exp3"] >= 1

    model = json.loads(experiment_nn.read_text(encoding="utf-8"))
    assert model["sample_count"] >= 1
    dl_model = json.loads(experiment_dl.read_text(encoding="utf-8"))
    assert dl_model["sample_count"] >= 1
    assert dl_model["replay_size"] >= summary["updates"]["teacher_distillation_exp3"]
    pv_model = json.loads(experiment_pv.read_text(encoding="utf-8"))
    assert pv_model["sample_count"] >= 1
    reports = write_training_report(summary, report_dir=runtime_dir / "reports" / "games")
    assert Path(reports["json_report"]).exists()
    assert Path(reports["md_report"]).exists()


def test_games_runtime_defaults_share_external_runtime(monkeypatch):
    monkeypatch.delenv("HACKME_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("HTML_LEARNING_DB_DIR", raising=False)
    monkeypatch.delenv("HTML_LEARNING_CHESS_ENGINE_DB_PATH", raising=False)
    monkeypatch.delenv("HTML_LEARNING_CHESS_ENGINE_NN_MODEL_PATH", raising=False)
    monkeypatch.delenv("HTML_LEARNING_REPORTS_DIR", raising=False)

    runtime_root = default_runtime_root_path().resolve()
    assert default_chess_engine_db_path() == runtime_root / "games" / "models" / "chess_experiment.db"
    assert bundled_chess_engine_db_path().name == "chess_experiment.db"
    assert default_chess_nn_model_path() == runtime_root / "games" / "models" / "chess_experiment_2_nn.json"
    assert default_chess_dl_model_path() == runtime_root / "games" / "models" / "chess_experiment_3_dl.json"
    assert default_chess_pv_model_path() == runtime_root / "games" / "models" / "chess_experiment_4_pv.json"
    assert bundled_chess_nn_model_path().name == "chess_experiment_2_nn.json"
    assert bundled_chess_dl_model_path().name == "chess_experiment_3_dl.json"
    assert bundled_chess_pv_model_path().name == "chess_experiment_4_pv.json"
    assert default_training_report_dir() == runtime_root / "reports" / "games"


def test_training_session_can_run_hard_vs_experiment_only(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    experiment_db = runtime_dir / "database" / "chess_experiment.db"
    experiment_nn = runtime_dir / "games" / "models" / "chess_experiment_2_nn.json"
    experiment_dl = runtime_dir / "games" / "models" / "chess_experiment_3_dl.json"
    experiment_pv = runtime_dir / "games" / "models" / "chess_experiment_4_pv.json"
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_DB_PATH", str(experiment_db))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_NN_MODEL_PATH", str(experiment_nn))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_DL_MODEL_PATH", str(experiment_dl))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_PV_MODEL_PATH", str(experiment_pv))
    monkeypatch.setattr(chess_dl_service, "_SEARCH_DEPTH", 1)
    monkeypatch.setattr(chess_dl_service, "_SEARCH_QUIESCENCE_DEPTH", 1)
    monkeypatch.setattr(chess_pv_service, "_SEARCH_DEPTH", 1)
    monkeypatch.setattr(chess_pv_service, "_SEARCH_QUIESCENCE_DEPTH", 1)
    monkeypatch.setattr(self_play_training_service, "_choose_training_move", lambda board_state, side, *_args, **_kwargs: _fast_legal_training_move(board_state, side))

    summary = run_training_session(
        exp1_teacher_games=0,
        exp2_teacher_games=0,
        exp3_teacher_games=0,
        hard_exp1_games=1,
        hard_exp2_games=1,
        hard_exp3_games=1,
        hard_exp4_games=1,
        cross_games=0,
        cross_exp1_exp3_games=0,
        cross_exp2_exp3_games=0,
        cross_exp1_exp4_games=0,
        cross_exp2_exp4_games=0,
        cross_exp3_exp4_games=0,
        teacher_depth=1,
        max_plies=10,
        student_exploration_rate=0.0,
        seed=17,
        store=ChessExperimentStore(experiment_db),
        nn_model_path=experiment_nn,
        dl_model_path=experiment_dl,
        pv_model_path=experiment_pv,
    )

    assert summary["games_played"] == 4
    engines = {(match["white_engine"], match["black_engine"]) for match in summary["matches"]}
    assert any("hard" in pairing for pairing in engines)
    assert summary["requested_games"]["hard_vs_exp1"] == 1
    assert summary["requested_games"]["hard_vs_exp2"] == 1
    assert summary["requested_games"]["hard_vs_exp3"] == 1
    assert summary["requested_games"]["hard_vs_exp4"] == 1


def test_human_probe_case_rejects_illegal_engine_move(tmp_path, monkeypatch):
    monkeypatch.setattr(
        self_play_training_service,
        "_engine_move_for_benchmark",
        lambda *args, **kwargs: {"from": "a1", "to": "a8"},
    )
    case = dict(self_play_training_service._HUMAN_PROBE_CASES[1])
    result = self_play_training_service._run_human_probe_case(
        "experiment",
        case,
        store=ChessExperimentStore(tmp_path / "bench.db"),
        nn_model_path=tmp_path / "exp2.json",
        dl_model_path=tmp_path / "exp3.json",
        pv_model_path=tmp_path / "exp4.json",
        nnue_model_path=tmp_path / "exp5.json",
        teacher_depth=1,
    )

    assert result["pass"] is False
    assert result["engine_illegal_move"] is True
    assert "illegal_uci" in result["reason"]
    assert result["final_fen"] == case["initial_fen"]


def test_endgame_case_requires_queen_promotion(tmp_path, monkeypatch):
    monkeypatch.setattr(
        self_play_training_service,
        "_engine_move_for_benchmark",
        lambda *args, **kwargs: {"from": "e7", "to": "e8", "promotion": "n"},
    )
    case = dict(self_play_training_service._ENDGAME_SUITE_CASES[2])
    result = self_play_training_service._evaluate_endgame_case(
        "experiment 3:dl",
        case,
        store=ChessExperimentStore(tmp_path / "bench.db"),
        nn_model_path=tmp_path / "exp2.json",
        dl_model_path=tmp_path / "exp3.json",
        pv_model_path=tmp_path / "exp4.json",
        nnue_model_path=tmp_path / "exp5.json",
        teacher_depth=1,
    )

    assert result["pass"] is False
    assert result["is_promotion"] is True
    assert result["promotion"] == "n"
    assert "unexpected_promotion_piece" in result["reason"]


def test_human_probe_case_records_mate_window_and_material_gain(tmp_path, monkeypatch):
    monkeypatch.setattr(
        self_play_training_service,
        "_engine_move_for_benchmark",
        lambda *args, **kwargs: {"from": "e2", "to": "f1"},
    )
    case = dict(self_play_training_service._HUMAN_PROBE_CASES[1])
    result = self_play_training_service._run_human_probe_case(
        "experiment 2:nn",
        case,
        store=ChessExperimentStore(tmp_path / "bench.db"),
        nn_model_path=tmp_path / "exp2.json",
        dl_model_path=tmp_path / "exp3.json",
        pv_model_path=tmp_path / "exp4.json",
        nnue_model_path=tmp_path / "exp5.json",
        teacher_depth=1,
    )

    assert result["pass"] is True
    assert result["engine_moves"] == ["e2f1"]
    assert result["material_gain"] >= 800
    assert result["human_has_mate_in_one"] is False
    assert result["engine_illegal_move"] is False


def test_round_robin_benchmark_and_smoke_reports_use_all_engines(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    experiment_db = runtime_dir / "database" / "chess_experiment.db"
    experiment_nn = runtime_dir / "games" / "models" / "chess_experiment_2_nn.json"
    experiment_dl = runtime_dir / "games" / "models" / "chess_experiment_3_dl.json"
    experiment_pv = runtime_dir / "games" / "models" / "chess_experiment_4_pv.json"
    experiment_nnue = runtime_dir / "games" / "models" / "chess_experiment_5_nnue_experience.json"
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_DB_PATH", str(experiment_db))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_NN_MODEL_PATH", str(experiment_nn))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_DL_MODEL_PATH", str(experiment_dl))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_PV_MODEL_PATH", str(experiment_pv))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_NNUE_MODEL_PATH", str(experiment_nnue))
    monkeypatch.setattr(chess_dl_service, "_SEARCH_DEPTH", 1)
    monkeypatch.setattr(chess_dl_service, "_SEARCH_QUIESCENCE_DEPTH", 1)
    monkeypatch.setattr(chess_pv_service, "_SEARCH_DEPTH", 1)
    monkeypatch.setattr(chess_pv_service, "_SEARCH_QUIESCENCE_DEPTH", 1)

    def fast_training_match(white_engine, black_engine, **kwargs):
        opening_label = str(kwargs.get("opening_label") or "fixture_opening")
        return self_play_training_service.TrainingMatch(
            white_engine=white_engine,
            black_engine=black_engine,
            winner_color="white",
            reason="fixture_fast_match",
            move_count=4,
            final_fen=chess.STARTING_FEN,
            uci_moves=["e2e4", "e7e5", "g1f3", "b8c6"],
            opening_label=opening_label,
            student_updates={},
            teacher_guidance_updates={},
            teacher_distillation_updates=0,
        )

    def fast_benchmark_move(_difficulty, board_state, side, **_kwargs):
        board = chess.Board(str(board_state.get("__fen__")))
        for legal in sorted(board.legal_moves, key=lambda move: move.uci()):
            if board.turn == (chess.WHITE if side == "white" else chess.BLACK):
                return {
                    "from": chess.square_name(legal.from_square),
                    "to": chess.square_name(legal.to_square),
                    "promotion": chess.piece_symbol(legal.promotion) if legal.promotion else None,
                }
        return None

    monkeypatch.setattr(self_play_training_service, "play_training_match", fast_training_match)
    monkeypatch.setattr(self_play_training_service, "_engine_move_for_benchmark", fast_benchmark_move)

    smoke = run_post_training_smoke_evaluation(
        store=ChessExperimentStore(experiment_db),
        nn_model_path=experiment_nn,
        dl_model_path=experiment_dl,
        pv_model_path=experiment_pv,
        nnue_model_path=experiment_nnue,
        teacher_depth=1,
        max_plies=2,
        games_per_pair=1,
        seed=32,
    )
    benchmark = run_round_robin_benchmark(
        store=ChessExperimentStore(experiment_db),
        nn_model_path=experiment_nn,
        dl_model_path=experiment_dl,
        pv_model_path=experiment_pv,
        nnue_model_path=experiment_nnue,
        teacher_depth=1,
        max_plies=2,
        rounds=1,
        seed=33,
    )
    assert smoke["target_engines"] == [
        "experiment",
        "experiment 2:nn",
        "experiment 3:dl",
        "experiment 4:pv",
        "experiment 5:nnue",
    ]
    assert smoke["reference_engines"] == ["hard", "teacher"]
    assert smoke["opening_split"] == "eval"
    assert smoke["games_played"] == 20
    assert benchmark["engines"] == [
        "teacher",
        "hard",
        "experiment",
        "experiment 2:nn",
        "experiment 3:dl",
        "experiment 4:pv",
        "experiment 5:nnue",
    ]
    assert benchmark["games_played"] == 42
    assert len(benchmark["standings"]) == 7
    assert len(benchmark["elo"]) == 7
    assert benchmark["matrix"]["teacher"]["hard"]["games"] == 2
    assert benchmark["matrix"]["experiment"]["experiment 2:nn"]["games"] == 2
    assert benchmark["matrix"]["experiment 3:dl"]["experiment 4:pv"]["games"] == 2
    assert any(row["engine_a"] == "experiment" and row["engine_b"] == "experiment 2:nn" for row in benchmark["head_to_head"])
    assert any(match["opening_label"] for match in benchmark["matches"])
    assert benchmark["human_probes"]["cases"] == 10
    assert benchmark["human_probes"]["engines"] == benchmark["engines"]
    assert len(benchmark["human_probes"]["standings"]) == 7
    assert len(benchmark["human_probes"]["results"]) == 70
    assert all("reason" in row and "final_fen" in row for row in benchmark["human_probes"]["results"])
    assert benchmark["endgame_suite"]["cases"] == 6
    assert benchmark["endgame_suite"]["engines"] == benchmark["engines"]
    assert len(benchmark["endgame_suite"]["standings"]) == 7
    assert len(benchmark["endgame_suite"]["results"]) == 42
    assert all("reason" in row and "final_fen" in row for row in benchmark["endgame_suite"]["results"])


def test_teacher_only_training_distills_into_exp3_model(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    experiment_db = runtime_dir / "database" / "chess_experiment.db"
    experiment_nn = runtime_dir / "games" / "models" / "chess_experiment_2_nn.json"
    experiment_dl = runtime_dir / "games" / "models" / "chess_experiment_3_dl.json"
    experiment_pv = runtime_dir / "games" / "models" / "chess_experiment_4_pv.json"
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_DB_PATH", str(experiment_db))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_NN_MODEL_PATH", str(experiment_nn))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_DL_MODEL_PATH", str(experiment_dl))
    monkeypatch.setenv("HTML_LEARNING_CHESS_ENGINE_PV_MODEL_PATH", str(experiment_pv))
    monkeypatch.setattr(chess_dl_service, "_SEARCH_DEPTH", 1)
    monkeypatch.setattr(chess_dl_service, "_SEARCH_QUIESCENCE_DEPTH", 1)
    monkeypatch.setattr(chess_pv_service, "_SEARCH_DEPTH", 1)
    monkeypatch.setattr(chess_pv_service, "_SEARCH_QUIESCENCE_DEPTH", 1)
    monkeypatch.setattr(self_play_training_service, "_choose_training_move", lambda board_state, side, *_args, **_kwargs: _fast_legal_training_move(board_state, side))

    summary = run_training_session(
        exp1_teacher_games=1,
        exp2_teacher_games=0,
        exp3_teacher_games=0,
        hard_exp1_games=0,
        hard_exp2_games=0,
        hard_exp3_games=0,
        cross_games=0,
        cross_exp1_exp3_games=0,
        cross_exp2_exp3_games=0,
        teacher_depth=1,
        max_plies=8,
        student_exploration_rate=0.0,
        seed=44,
        store=ChessExperimentStore(experiment_db),
        nn_model_path=experiment_nn,
        dl_model_path=experiment_dl,
        pv_model_path=experiment_pv,
    )

    assert summary["games_played"] == 1
    assert summary["updates"]["teacher_distillation_exp3"] >= 1
    dl_model = json.loads(experiment_dl.read_text(encoding="utf-8"))
    assert dl_model["sample_count"] >= summary["updates"]["teacher_distillation_exp3"]
