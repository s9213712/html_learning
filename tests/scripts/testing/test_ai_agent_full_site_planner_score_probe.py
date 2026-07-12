import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.testing.ai_agent_full_site_planner_score_probe import (
    CASES,
    _planner_settings_update,
    _score_case,
)


def _case(case_id: str) -> dict:
    return next(case for case in CASES if case["case_id"] == case_id)


def _matching_plan(case: dict) -> dict:
    return {
        "action": case["expect_action"],
        "tool": case["expect_tools"][0],
        "args": dict(case.get("expected_args") or {}),
    }


def test_planner_score_probe_help_runs_directly_from_repo_root(tmp_path: Path):
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")

    completed = subprocess.run(
        [sys.executable, "scripts/testing/ai_agent_full_site_planner_score_probe.py", "--help"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--case-id" in completed.stdout
    assert "ModuleNotFoundError" not in completed.stderr


def test_planner_settings_allow_the_selected_model():
    payload = _planner_settings_update(
        model="qwen3.5:cloud",
        api_base_url="http://127.0.0.1:11434/v1/",
        comfyui_api_url="http://127.0.0.1:8189/",
    )

    assert payload["ai_agent_model"] == "qwen3.5:cloud"
    assert payload["ai_agent_allowed_models"] == "qwen3.5:cloud"
    assert payload["ai_agent_api_base_url"] == "http://127.0.0.1:11434/v1"
    assert payload["comfyui_remote_api_url"] == "http://127.0.0.1:8189"


def test_scored_cases_define_exact_trading_and_launch_contracts():
    limit_order = _case("trade_limit_order")
    moving_average = _case("trade_bot_backtest")
    dry_run = _case("launch_preflight_dry_run")
    go_live = _case("launch_preflight_go_live")

    assert limit_order["expected_args"]["order_type"] == "limit"
    assert moving_average["expected_args"]["strategy"] == "moving_average"
    assert "GO_LIVE" not in dry_run["text"]
    assert dry_run["expected_args"]["auto_switch"] is False
    assert "confirm" not in dry_run["required_args"]
    assert dry_run["forbidden_args"] == ["confirm"]
    assert "GO_LIVE" in go_live["text"]
    assert go_live["expected_args"]["auto_switch"] is True
    assert go_live["expected_args"]["confirm"] == "GO_LIVE"
    assert "confirm" in go_live["required_args"]


@pytest.mark.parametrize(
    "case_id",
    [
        "trade_limit_order",
        "trade_bot_backtest",
        "launch_preflight_dry_run",
        "launch_preflight_go_live",
    ],
)
def test_score_case_accepts_matching_expected_arg_values(case_id: str):
    case = _case(case_id)

    score = _score_case(case, _matching_plan(case))

    assert score["passed"] is True
    assert score["arg_value_mismatches"] == []
    assert score["forbidden_args_present"] == []


@pytest.mark.parametrize(
    ("case_id", "arg_name", "wrong_value"),
    [
        ("trade_limit_order", "order_type", "market"),
        ("trade_bot_backtest", "strategy", "default"),
        ("launch_preflight_dry_run", "auto_switch", True),
        ("launch_preflight_go_live", "confirm", "EXECUTE"),
    ],
)
def test_score_case_rejects_wrong_expected_arg_values(case_id: str, arg_name: str, wrong_value):
    case = _case(case_id)
    plan = _matching_plan(case)
    plan["args"][arg_name] = wrong_value

    score = _score_case(case, plan)

    assert score["passed"] is False
    assert score["arg_value_mismatches"] == [
        {
            "arg": arg_name,
            "expected": case["expected_args"][arg_name],
            "actual": wrong_value,
        }
    ]


def test_launch_preflight_dry_run_rejects_confirmation_argument():
    case = _case("launch_preflight_dry_run")
    plan = _matching_plan(case)
    plan["args"]["confirm"] = "GO_LIVE"

    score = _score_case(case, plan)

    assert score["passed"] is False
    assert score["forbidden_args_present"] == ["confirm"]


def test_explicit_go_live_requires_confirmation_argument():
    case = _case("launch_preflight_go_live")
    plan = _matching_plan(case)
    del plan["args"]["confirm"]

    score = _score_case(case, plan)

    assert score["passed"] is False
    assert score["missing_args"] == ["confirm"]
