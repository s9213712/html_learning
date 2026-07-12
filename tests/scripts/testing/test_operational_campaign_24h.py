from __future__ import annotations

import time
from pathlib import Path

import pytest

from scripts.testing.operational_campaign_24h import (
    MIN_FORMAL_SECONDS,
    Campaign,
    ServerController,
    build_parser,
    sanitized_command,
    source_manifest,
    validate_tmp_path,
)


def campaign_args(tmp_path: Path, *extra: str):
    return build_parser().parse_args([
        "--campaign-root",
        str(tmp_path / "campaign"),
        "--duration-seconds",
        "60",
        "--allow-short-duration",
        "--primary-port",
        "55101",
        "--recovery-port",
        "55102",
        "--minimum-free-gb",
        "0",
        *extra,
    ])


def test_formal_duration_is_24_hours() -> None:
    assert MIN_FORMAL_SECONDS == 86_400


def test_campaign_manifest_covers_product_code_harness_and_tests() -> None:
    manifest = source_manifest()

    for path in (
        "server.py",
        "test_for_develop.sh",
        "public/index.html",
        "routes/ai_agent.py",
        "services/snapshots/schema.py",
        "scripts/testing/operational_campaign_24h.py",
        "tests/scripts/testing/test_operational_campaign_24h.py",
    ):
        assert path in manifest


def test_campaign_matrix_contains_every_mandatory_operational_category(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    categories = {spec.category for spec in campaign.scenario_specs()}
    assert categories == {
        "long_video_upload_stream_hls_share",
        "ai_agent_full_operations",
        "trading_and_background_trading",
        "pointschain_high_frequency_mechanisms",
        "wallet_incident_and_chain_governance",
        "backup_restore_restart_emergency",
        "realtime_proxy_and_cross_browser_media",
        "desktop_mobile_prelaunch_and_member_ux",
    }
    assert all(spec.mandatory for spec in campaign.scenario_specs())
    assert max(spec.fraction for spec in campaign.scenario_specs()) < 1


def test_managed_server_launcher_keeps_credentials_out_of_argv(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    controller: ServerController = campaign.primary
    command = controller.launcher_command()
    assert "--root-password" not in command
    assert "--manager-password" not in command
    assert "--test-password" not in command
    assert campaign.credentials.root not in command
    assert campaign.credentials.manager not in command
    assert campaign.credentials.test not in command


def test_sanitized_command_redacts_all_supported_secret_flags() -> None:
    command = sanitized_command([
        "probe",
        "--root-password",
        "root-secret",
        "--member-password=member-secret",
        "--accounts",
        "a:secret,b:secret",
    ])
    assert command == [
        "probe",
        "--root-password",
        "[redacted]",
        "--member-password=[redacted]",
        "--accounts",
        "[redacted]",
    ]


def test_campaign_paths_must_stay_under_tmp(tmp_path: Path) -> None:
    assert validate_tmp_path(tmp_path / "ok", label="test") == (tmp_path / "ok").resolve()
    with pytest.raises(ValueError, match="must remain under /tmp"):
        validate_tmp_path(Path("/var/lib/hackme-campaign"), label="test")


def test_cli_restore_contract_preserves_storage_and_append_only_finance() -> None:
    script = (Path(__file__).resolve().parents[3] / "test_for_develop.sh").read_text(encoding="utf-8")
    assert "append_only_financial_restore_disabled" in script
    assert 'mv "$backup_existing/storage" "$RUNTIME_ROOT/storage"' in script
    for name in ("finance.db", "points_chain.db", "trading.db"):
        assert name in script


def test_campaign_detects_early_core_exit_without_waiting_for_delayed_scenarios(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.active_started = time.monotonic() - 10
    assert campaign.required_duration_completed() is False

    campaign.active_started = time.monotonic() - 60
    assert campaign.required_duration_completed() is True


def test_long_video_scenario_uploads_waits_and_measures_hls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    captured: dict[str, list[str]] = {}

    def fake_run_step(_scenario_id: str, _step_id: str, command: list[str], **_kwargs: object) -> dict[str, bool]:
        captured["command"] = command
        return {"ok": True}

    monkeypatch.setattr(campaign, "run_step", fake_run_step)

    result = campaign.scenario_media_long()

    assert result["ok"] is True
    for flag in ("--upload", "--wait", "--measure", "--verify-share", "--browser-seek", "--browser-mobile"):
        assert flag in captured["command"]
