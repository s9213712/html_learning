import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_trading_backtest_probe_help_runs_outside_repo(tmp_path):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "trading" / "probes" / "backtest_20000_probe.py"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--include-route" in completed.stdout


def test_trading_backtest_20000_probe_includes_latest_regressions():
    probe = (ROOT / "scripts" / "trading" / "probes" / "backtest_20000_probe.py").read_text(encoding="utf-8")

    assert 'os.environ.get("HACKME_BACKTEST_CHAIN_SECRET") or secrets.token_urlsafe(48)' in probe
    assert 'chain_secret="probe-secret"' not in probe
    assert "single_candle_rejected_without_silent_fetch" in probe
    assert "workflow_flat_bollinger_guard" in probe
    assert "backtest_outlier_jump_skipped" in probe
    assert 'choices=("all", "conditional", "dca", "workflow", "grid", "route", "over_limit", "flat_bollinger", "outlier_jump", "single_candle_rejected")' in probe
    assert 'payload.get("max_backtest_candles_per_batch") == 10_000' in probe


def test_workflow_template_validation_includes_flat_sequence_guard():
    script = (ROOT / "scripts" / "trading" / "validation" / "trading_workflow_template_validation.py").read_text(encoding="utf-8")

    # Plan B: swing_bb_ma50 removed from workflows/trading_bot/, guard now applies
    # to bollinger_reversion alone.
    assert 'FLAT_SEQUENCE_GUARD_TEMPLATE_IDS = {"bollinger_reversion"}' in script
    assert 'test_artifact_path("reports", "trading", "workflow_template_validation")' in script
    assert "def validate_flat_sequence_guard" in script
    assert '"flat_sequence_guard": flat_guard' in script
    assert '"workflow_graph templates are validated via trigger scenarios, flat-sequence guards, and engine backtest sanity checks"' in script


def test_trading_exchange_validation_includes_avg_cost_sanity_and_clean_workflow_case():
    script = (ROOT / "scripts" / "trading" / "validation" / "trading_exchange_validation.py").read_text(encoding="utf-8")

    assert 'workflow_tmp = Path(tmp) / "workflow_case"' in script
    assert "workflow bot honors nested condition and does not repeat exhausted scaling steps" in script
    assert "incremental spot buys preserve sane average cost accounting" in script
    assert "ETH/POINTS live price is 1000; average cost should stay in a sane range after DCA and conditional buys." in script
