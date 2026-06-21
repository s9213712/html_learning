from pathlib import Path

from scripts.trading.competition.workflow_template_backtest_benchmark import REPO_ROOT, default_output_path
from services.trading import backtest_capacity as backtest_capacity_module


ROOT = Path(__file__).resolve().parents[3]


def test_default_output_path_uses_canonical_file_for_default_1h_asset():
    path = default_output_path("1h", use_relative_thresholds=False)

    assert path == REPO_ROOT / "workflows" / "trading_bot" / "benchmarks" / "workflow_template_benchmarks.json"


def test_default_output_path_keeps_variant_suffix_for_noncanonical_outputs():
    assert default_output_path("4h", use_relative_thresholds=False) == (
        REPO_ROOT / "workflows" / "trading_bot" / "benchmarks" / "workflow_template_benchmarks_4h.json"
    )
    assert default_output_path("1h", use_relative_thresholds=True) == (
        REPO_ROOT / "workflows" / "trading_bot" / "benchmarks" / "workflow_template_benchmarks_1h_relative.json"
    )


def test_measure_backtest_capacity_projects_slowest_and_fastest_throughput(monkeypatch):
    monkeypatch.setattr(backtest_capacity_module, "_make_probe_candles", lambda n: [{} for _ in range(n)])
    monkeypatch.setattr(
        backtest_capacity_module,
        "_build_probe_payloads",
        lambda market_symbol, candles: [("slow", {}), ("fast", {}), ("medium", {})],
    )

    class FakeTradingService:
        def backtest_trading_bot(self, *, actor, payload):
            return {"ok": True}

    timeline = iter([0.0, 2.0, 2.0, 2.5, 2.5, 3.5])
    monkeypatch.setattr(backtest_capacity_module.time, "perf_counter", lambda: next(timeline))

    result = backtest_capacity_module.measure_backtest_capacity(
        trading_service=FakeTradingService(),
        actor={"id": 1, "username": "alice", "role": "user"},
        probe_candles=200,
        time_budget_seconds=60,
    )

    assert result["bottleneck_strategy"] == "slow"
    assert result["fastest_strategy"] == "fast"
    assert result["measured_capacity_min"] == 6000
    assert result["measured_capacity_max"] == 24000


def test_canonical_workflow_template_benchmark_contract_uses_server_route():
    frontend = (
        (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "56-trading-bots.js").read_text(encoding="utf-8")
    )
    routes = (ROOT / "routes" / "trading.py").read_text(encoding="utf-8")

    assert "/trading/workflow-template-benchmarks" in frontend
    assert "/api/trading/workflow-template-benchmarks" in routes
    assert "/data/workflow_template_benchmarks.json" not in frontend
