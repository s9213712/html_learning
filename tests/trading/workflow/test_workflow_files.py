import json
from pathlib import Path

from flask import Flask, jsonify

from routes.trading import register_trading_routes


ROOT = Path(__file__).resolve().parents[3]


def test_trading_workflow_templates_live_under_workflows_directory():
    workflow_root = ROOT / "workflows"
    trading_bot_dir = workflow_root / "trading_bot"
    assert trading_bot_dir.is_dir()

    # Plan B (N=11) — head-to-head finalists + 4 trend followers + 2
    # mean-reversion + 3 exit-only tools.  See workflows/README.md for
    # the per-template ranking and rationale.
    expected = {
        "dipbuy_rsi35_70_size99_late_tp15_nopyr_codex",
        "auto_search_winner_claude_rev3_return",
        "ma200_trend_entry",
        "breakout_buy",
        "ma_pullback",
        "dip_buy",
        "kd_momentum",
        "bollinger_reversion",
        "risk_guard",
        "staged_profit_taking",
        "stop_loss",
    }
    found = {path.stem for path in trading_bot_dir.glob("*.json")}
    assert found == expected

    for path in trading_bot_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["scope"] == "system"
        explanation = payload.get("explanation")
        assert isinstance(explanation, dict), path.name
        for key in ("purpose", "entry_conditions", "actions", "risk_notes", "best_for", "tuning"):
            assert explanation.get(key), f"{path.name} missing explanation.{key}"
        assert payload["workflow"]["strategy_kind"] == "workflow_graph"
        assert payload["workflow"]["nodes"]
        assert payload["workflow"]["edges"] or payload["workflow"]["start_node_id"] == "start"


def test_workflow_custom_files_are_runtime_data_not_committed_templates():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!workflows/trading_bot/*.json" in gitignore
    assert "/runtime/" in gitignore

    routes = (ROOT / "routes" / "trading.py").read_text(encoding="utf-8")
    frontend = (
        (ROOT / "public" / "js" / "56-trading.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "56-trading-bots.js").read_text(encoding="utf-8")
    )
    index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert 'WORKFLOW_SYSTEM_DIR = WORKFLOW_ROOT / "trading_bot"' in routes
    assert '"/api/trading/workflow-templates"' in routes
    assert '"/api/trading/workflow-templates/custom"' in routes
    assert 'runtime/workflows/custom' in routes
    assert '"/api/trading/workflow-editor/backtest"' in routes
    assert '"/trading/workflow-templates"' in frontend
    assert '"/trading/workflow-templates/custom"' in frontend
    assert 'runtime/workflows/custom' in frontend
    assert 'id="trading-workflow-custom-save-btn"' in index


def test_workflow_template_api_loads_system_templates_from_trading_bot_directory():
    app = Flask(__name__)
    app.testing = True

    class FakeTradingService:
        def _validate_workflow(self, workflow):
            return workflow

    def passthrough(fn):
        return fn

    def json_resp(payload, status=None):
        response = jsonify(payload)
        return (response, status) if status else response

    register_trading_routes(app, {
        "trading_service": FakeTradingService(),
        "get_current_user_ctx": lambda: {"id": 1, "username": "alice", "role": "user"},
        "json_resp": json_resp,
        "require_csrf": passthrough,
        "require_csrf_safe": passthrough,
    })

    response = app.test_client().get("/api/trading/workflow-templates")

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["system_workflow_root"] == "workflows/trading_bot"
    assert len(data["system"]) == 11
    assert data["custom"] == []
    assert {item["id"] for item in data["system"]} == {
        "dipbuy_rsi35_70_size99_late_tp15_nopyr_codex",
        "auto_search_winner_claude_rev3_return",
        "ma200_trend_entry",
        "breakout_buy",
        "ma_pullback",
        "dip_buy",
        "kd_momentum",
        "bollinger_reversion",
        "risk_guard",
        "staged_profit_taking",
        "stop_loss",
    }
    assert all(item["source_path"].startswith("workflows/trading_bot/") for item in data["system"])


def test_workflow_readme_matches_current_kept_competition_templates():
    readme = (ROOT / "workflows" / "README.md").read_text(encoding="utf-8")

    # Plan B (N=11): README must list every kept template + reference
    # the head-to-head report so reviewers can trace ranking origin.
    for name in (
        "dipbuy_rsi35_70_size99_late_tp15_nopyr_codex",
        "auto_search_winner_claude_rev3_return",
        "ma200_trend_entry",
        "breakout_buy",
        "ma_pullback",
        "dip_buy",
        "kd_momentum",
        "bollinger_reversion",
        "risk_guard",
        "staged_profit_taking",
        "stop_loss",
    ):
        assert name in readme, f"README missing kept template {name!r}"
    assert "FINAL_HEAD_TO_HEAD_REPORT.md" in readme
