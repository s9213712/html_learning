import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "docs" / "AGENTS" / "skills" / "hackme-web-qa" / "scripts" / "member_probe.py"


def load_probe_module():
    spec = importlib.util.spec_from_file_location("hackme_web_qa_member_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_member_probe_writes_requested_json_on_login_preflight_failure(monkeypatch, tmp_path):
    probe = load_probe_module()
    report = tmp_path / "member-probe.json"
    monkeypatch.setattr(probe, "write_fixtures", lambda _root: {})
    monkeypatch.setattr(probe.Client, "login", lambda _self: (_ for _ in ()).throw(TimeoutError("login timed out")))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "member_probe.py",
            "--base-url",
            "https://127.0.0.1:9",
            "--root-password",
            "root-test-secret",
            "--test-password",
            "member-test-secret",
            "--out",
            str(report),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        probe.cli_main()

    assert exc.value.code == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["fatal_error"] == "TimeoutError: login timed out"
    assert payload["findings"][-1]["severity"] == "critical"
    assert payload["checks"][-1]["name"] == "probe preflight/runtime failure"
