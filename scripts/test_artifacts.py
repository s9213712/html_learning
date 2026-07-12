"""Shared output paths for repository QA and operational drill artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


ENV_NAME = "HACKME_TEST_OUTPUT_ROOT"
DEFAULT_ROOT_NAME = "hackme_web_test_artifacts"
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_artifact_root() -> Path:
    configured = str(os.environ.get(ENV_NAME, "") or "").strip()
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            raise ValueError(f"{ENV_NAME} must be an absolute path")
        root = root.resolve()
    else:
        root = Path(tempfile.gettempdir()).resolve() / DEFAULT_ROOT_NAME
    if root == REPO_ROOT or REPO_ROOT in root.parents:
        raise ValueError(f"{ENV_NAME} must be outside the source checkout")
    return root


def test_artifact_path(*parts: str) -> Path:
    return test_artifact_root().joinpath(*parts)
