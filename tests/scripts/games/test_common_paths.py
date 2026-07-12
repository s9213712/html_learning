from pathlib import Path

import pytest

from scripts.games.common_paths import (
    REPO_ROOT,
    chess_results_root,
    exp6_artifacts_root,
    exp6_private_dir,
    runtime_model_path,
    runtime_root,
)


def test_game_results_default_to_external_test_artifacts(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKME_CHESS_RESULTS_DIR", raising=False)
    monkeypatch.setenv("HACKME_TEST_OUTPUT_ROOT", str(tmp_path / "artifacts"))

    assert chess_results_root() == tmp_path / "artifacts" / "games" / "chess_results"


def test_game_paths_reject_relative_and_source_overrides(monkeypatch):
    monkeypatch.setenv("HACKME_CHESS_RESULTS_DIR", "relative/results")
    with pytest.raises(ValueError, match="absolute path"):
        chess_results_root()

    monkeypatch.setenv("HACKME_CHESS_RESULTS_DIR", str(REPO_ROOT / "runtime" / "results"))
    with pytest.raises(ValueError, match="outside the source checkout"):
        chess_results_root()


def test_game_runtime_model_uses_configured_external_runtime(tmp_path, monkeypatch):
    configured = tmp_path / "runtime"
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(configured))

    assert runtime_root() == configured
    assert runtime_model_path("nested/not-allowed.json") == configured / "games" / "models" / "not-allowed.json"
    assert exp6_private_dir() == configured / "private" / "games" / "exp6"


def test_exp6_artifacts_default_to_test_output_root(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKME_EXP6_OUTPUT_DIR", raising=False)
    monkeypatch.setenv("HACKME_TEST_OUTPUT_ROOT", str(tmp_path / "artifacts"))

    assert exp6_artifacts_root() == tmp_path / "artifacts" / "games" / "exp6"
