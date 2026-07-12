"""Path policy shared by standalone game training and validation scripts."""

from __future__ import annotations

import os
from pathlib import Path

from scripts.test_artifacts import test_artifact_path
from services.server.runtime import default_runtime_root_path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _external_absolute_env_path(name: str, *, allow_disposable_tmp_checkout: bool = False) -> Path | None:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    path = path.resolve()
    inside_checkout = path == REPO_ROOT or REPO_ROOT in path.parents
    disposable_checkout = allow_disposable_tmp_checkout and REPO_ROOT.is_relative_to(Path("/tmp"))
    if inside_checkout and not disposable_checkout:
        raise ValueError(f"{name} must stay outside the source checkout")
    return path


def chess_results_root() -> Path:
    return _external_absolute_env_path("HACKME_CHESS_RESULTS_DIR") or test_artifact_path(
        "games", "chess_results"
    )


def runtime_root() -> Path:
    return _external_absolute_env_path(
        "HACKME_RUNTIME_DIR",
        allow_disposable_tmp_checkout=True,
    ) or default_runtime_root_path()


def runtime_model_path(filename: str) -> Path:
    return runtime_root() / "games" / "models" / Path(filename).name


def exp6_private_dir() -> Path:
    return runtime_root() / "private" / "games" / "exp6"


def exp6_artifacts_root() -> Path:
    return _external_absolute_env_path("HACKME_EXP6_OUTPUT_DIR") or test_artifact_path(
        "games", "exp6"
    )
