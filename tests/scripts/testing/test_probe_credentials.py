import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.testing.probe_credentials import (
    add_manager_password_argument,
    add_root_password_argument,
    add_user_password_argument,
    first_secret_from_env,
    redact_artifact_data,
)
from scripts.testing.video_hls_quality_stress import parse_accounts as parse_hls_accounts


def test_probe_root_password_is_required_without_environment(monkeypatch):
    for name in (
        "HACKME_PROBE_ROOT_PASSWORD",
        "HACKME_ROOT_PASSWORD",
        "PLAYWRIGHT_ROOT_PASSWORD",
        "HTML_LEARNING_ROOT_PASSWORD",
        "ROOT_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    parser = argparse.ArgumentParser()
    add_root_password_argument(parser)

    with pytest.raises(SystemExit) as exc:
        parser.parse_args([])

    assert exc.value.code == 2


def test_probe_root_password_uses_precedence_and_never_logs_value(monkeypatch):
    monkeypatch.setenv("ROOT_PASSWORD", "fallback-secret")
    monkeypatch.setenv("HACKME_PROBE_ROOT_PASSWORD", "preferred-secret")
    parser = argparse.ArgumentParser()
    add_root_password_argument(parser)

    args = parser.parse_args([])
    help_text = parser.format_help()

    assert args.root_password == "preferred-secret"
    assert first_secret_from_env(("HACKME_PROBE_ROOT_PASSWORD", "ROOT_PASSWORD")) == (
        "preferred-secret",
        "HACKME_PROBE_ROOT_PASSWORD",
    )
    assert "preferred-secret" not in help_text


def test_probe_manager_password_uses_environment(monkeypatch):
    monkeypatch.setenv("HACKME_PROBE_MANAGER_PASSWORD", "manager-secret")
    parser = argparse.ArgumentParser()
    add_manager_password_argument(parser, "--admin-password")

    args = parser.parse_args([])

    assert args.admin_password == "manager-secret"
    assert "manager-secret" not in parser.format_help()


def test_probe_user_password_uses_environment(monkeypatch):
    monkeypatch.setenv("HACKME_PROBE_USER_PASSWORD", "user-secret")
    parser = argparse.ArgumentParser()
    add_user_password_argument(parser, "--user-password")

    assert parser.parse_args([]).user_password == "user-secret"


def test_member_probe_skill_uses_environment_credentials_without_fixed_examples():
    root = Path(__file__).resolve().parents[3]
    skill_root = root / "docs" / "AGENTS" / "skills" / "hackme-web-qa"
    script = (skill_root / "scripts" / "member_probe.py").read_text(encoding="utf-8")
    docs = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert "HACKME_QA_ROOT_PASSWORD" in script
    assert "HACKME_QA_TEST_PASSWORD" in script
    assert "--root-password RootQa123!" not in docs
    assert "--test-password TestQa123!" not in docs


def test_hls_stress_accounts_use_structured_environment_json():
    accounts = parse_hls_accounts(
        [],
        '[{"username":"alice","password":"a-secret"},{"username":"bob","password":"b-secret"}]',
    )

    assert accounts == [("alice", "a-secret"), ("bob", "b-secret")]

    with pytest.raises(ValueError, match="requires HACKME_HLS_STRESS_ACCOUNTS_JSON"):
        parse_hls_accounts([], "")


def test_probe_artifact_redaction_handles_nested_fields_and_json_request_bodies():
    secret = "generated-campaign-secret"
    payload = {
        "password": secret,
        "nested": {"api_key": "sk-test", "note": f"prefix {secret} suffix"},
        "post_data": '{"tool":"write_member_create_user","arguments":{"password":"%s"}}' % secret,
    }

    redacted = redact_artifact_data(payload, secret_values=(secret,))

    serialized = str(redacted)
    assert secret not in serialized
    assert "sk-test" not in serialized
    assert redacted["password"] == "[redacted]"
    assert '"password":"[redacted]"' in redacted["post_data"]


@pytest.mark.parametrize(
    "script_name",
    [
        "points_chain_post_stress_playwright.py",
        "pointschain_real_incident_frontend_probe.py",
        "pointschain_live_branch_drill.py",
        "chat_video_share_link_probe.py",
    ],
)
def test_operational_probe_help_runs_outside_repo_without_pythonpath(tmp_path: Path, script_name: str):
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")

    completed = subprocess.run(
        [sys.executable, str(root / "scripts" / "testing" / script_name), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--help" in completed.stdout
