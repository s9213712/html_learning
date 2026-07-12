from scripts.admin import split_main_database


def test_split_main_database_uses_configured_external_runtime(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HACKME_RUNTIME_DIR", str(runtime_root))

    assert split_main_database._default_db_path() == runtime_root / "database" / "database.db"


def test_split_main_database_falls_back_to_xdg_state(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKME_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert split_main_database._default_db_path() == (
        tmp_path / "state" / "hackme_web" / "database" / "database.db"
    )
