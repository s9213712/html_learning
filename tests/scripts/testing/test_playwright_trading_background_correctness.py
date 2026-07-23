from __future__ import annotations

from scripts.testing.playwright_trading_background_correctness import (
    api_with_backpressure_retry,
    points_service,
    resolve_probe_database_paths,
    stress_result_is_controlled_server_busy,
)


def test_trading_setup_retries_only_explicit_admission_control_backpressure(monkeypatch):
    responses = iter([
        {
            "status": 503,
            "ok": False,
            "body": {"ok": False, "error": "server_busy", "retry_after_seconds": 2},
        },
        {"status": 200, "ok": True, "body": {"ok": True}},
    ])
    sleeps = []
    monkeypatch.setattr(
        "scripts.testing.playwright_trading_background_correctness.api",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr(
        "scripts.testing.playwright_trading_background_correctness.time.sleep",
        sleeps.append,
    )

    result = api_with_backpressure_retry(object(), "POST", "/trading/orders", {"quantity": "1"})

    assert result["status"] == 200
    assert result["backpressure_attempts"] == 2
    assert sleeps == [2.0]


def test_trading_setup_does_not_retry_unexpected_failures(monkeypatch):
    calls = 0

    def unexpected_failure(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"status": 500, "ok": False, "body": {"error": "database_locked"}}

    monkeypatch.setattr(
        "scripts.testing.playwright_trading_background_correctness.api",
        unexpected_failure,
    )

    result = api_with_backpressure_retry(object(), "POST", "/trading/orders", {"quantity": "1"})

    assert result["status"] == 500
    assert result["backpressure_attempts"] == 1
    assert calls == 1


def test_trading_probe_routes_identity_and_finance_databases_separately(tmp_path):
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    identity = database_dir / "database.db"
    finance = database_dir / "finance.db"
    identity.touch()
    finance.touch()

    assert resolve_probe_database_paths(tmp_path) == (identity, finance)


def test_trading_probe_keeps_legacy_single_database_compatibility(tmp_path):
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    identity = database_dir / "database.db"
    identity.touch()

    assert resolve_probe_database_paths(tmp_path) == (identity, identity)


def test_trading_probe_finance_service_attaches_core_identity_view(tmp_path):
    import sqlite3

    database_dir = tmp_path / "database"
    database_dir.mkdir()
    identity = database_dir / "database.db"
    finance = database_dir / "finance.db"
    identity_conn = sqlite3.connect(identity)
    identity_conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    identity_conn.execute("INSERT INTO users(id, username) VALUES (7, 'probe-user')")
    identity_conn.commit()
    identity_conn.close()
    sqlite3.connect(finance).close()
    (tmp_path / ".chain_seed").write_text("test-chain-seed", encoding="utf-8")

    service = points_service(finance, tmp_path, identity_db_path=identity)
    conn = service.get_db()
    try:
        row = conn.execute("SELECT id, username FROM users WHERE id=7").fetchone()
    finally:
        conn.close()

    assert dict(row) == {"id": 7, "username": "probe-user"}


def test_trading_stress_classifies_only_explicit_server_busy_as_controlled():
    assert stress_result_is_controlled_server_busy(
        {"status": 503, "body": {"ok": False, "error": "server_busy"}}
    )
    assert not stress_result_is_controlled_server_busy(
        {"status": 503, "body": {"ok": False, "error": "database_locked"}}
    )
    assert not stress_result_is_controlled_server_busy(
        {"status": 429, "body": {"ok": False, "error": "server_busy"}}
    )
