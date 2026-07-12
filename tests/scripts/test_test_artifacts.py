import tempfile
from pathlib import Path

import pytest

from scripts.test_artifacts import test_artifact_path as artifact_path
from scripts.test_artifacts import test_artifact_root as artifact_root
from scripts.security import common_paths


def test_artifact_root_defaults_outside_checkout(monkeypatch):
    monkeypatch.delenv("HACKME_TEST_OUTPUT_ROOT", raising=False)

    root = artifact_root()

    assert root == Path(tempfile.gettempdir()).resolve() / "hackme_web_test_artifacts"
    assert artifact_path("qa", "report.json") == root / "qa" / "report.json"


def test_artifact_root_accepts_absolute_override(monkeypatch, tmp_path):
    target = tmp_path / "retained"
    monkeypatch.setenv("HACKME_TEST_OUTPUT_ROOT", str(target))

    assert artifact_root() == target.resolve()


def test_artifact_root_rejects_relative_override(monkeypatch):
    monkeypatch.setenv("HACKME_TEST_OUTPUT_ROOT", "repo-artifacts")

    with pytest.raises(ValueError, match="must be an absolute path"):
        artifact_root()


def test_artifact_root_rejects_checkout_path(monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("HACKME_TEST_OUTPUT_ROOT", str(repo_root / "artifacts"))

    with pytest.raises(ValueError, match="outside the source checkout"):
        artifact_root()


def test_security_report_paths_default_outside_checkout(monkeypatch):
    monkeypatch.delenv("HACKME_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("HTML_LEARNING_REPORTS_DIR", raising=False)

    assert common_paths.security_reports_root() == artifact_root() / "reports" / "security"


def test_security_report_paths_reject_checkout_runtime(monkeypatch):
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(common_paths.REPO_ROOT / "runtime"))
    monkeypatch.delenv("HTML_LEARNING_REPORTS_DIR", raising=False)

    with pytest.raises(ValueError, match="outside the source checkout"):
        common_paths.security_reports_root()
