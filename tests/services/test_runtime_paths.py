from pathlib import Path

from services.server.runtime import default_runtime_root_path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_runtime_uses_xdg_state_home_outside_source(tmp_path, monkeypatch):
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    runtime_root = default_runtime_root_path()

    assert runtime_root == state_home / "hackme_web"
    assert runtime_root != REPO_ROOT / "runtime"
    assert REPO_ROOT not in runtime_root.parents


def test_relative_xdg_state_home_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_runtime_root_path() == tmp_path / ".local" / "state" / "hackme_web"
