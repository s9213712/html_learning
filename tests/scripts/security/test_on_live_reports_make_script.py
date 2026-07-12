import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

from scripts.security.gate import full_generator_live_validate as full_gate
from scripts.security.gate import on_live_reports_make as helper


ROOT = Path(__file__).resolve().parents[3]

def test_root_wrapper_is_removed_and_helper_is_direct_entrypoint():
    assert not (ROOT / "on_live_reports_make.sh").exists()
    helper_source = (ROOT / "scripts" / "security" / "gate" / "on_live_reports_make.py").read_text(encoding="utf-8")
    assert 'tester="scripts/security/gate/on_live_reports_make.py"' in helper_source


def test_live_report_helper_covers_all_required_report_types_and_runtime_outputs():
    helper = (ROOT / "scripts" / "security" / "gate" / "on_live_reports_make.py").read_text(encoding="utf-8")

    assert "PRODUCTION_REQUIRED_REPORT_TYPES" in helper
    assert 'security_reports_root() / "production_gate"' in helper
    assert 'runtime/reports/security/production_gate/runs/<RUN_ID>/' in helper
    assert 'runtime/reports/security/production_gate/' in helper
    assert '"/api/root/server-mode/logs/verify"' in helper
    assert '"/api/root/integrity/report"' in helper
    assert '"/api/root/integrity/findings?status=pending"' in helper
    assert '"/api/root/integrity/findings/bulk-review"' in helper
    assert '"/api/root/production-report/upload"' in helper
    assert "run_functional_smoke.sh" in helper
    assert "run_pentest.sh" in helper
    assert "functional_permission_pentest.py" in helper
    assert "trading_stress_pentest.py" in helper
    assert "args.target_root_password" not in helper
    assert '"ROOT_PASSWORD": args.root_password' in helper
    assert '"USER_A_USERNAME": "test"' in helper
    assert '"USER_B_USERNAME": "admin"' in helper
    assert "rotate_to=args.root_new_password" in helper
    assert "rerun with --root-new-password" in helper
    assert "--server-mode-timeout" in helper
    assert '--permission-timeout' in helper
    assert "deployment_review_pending" in helper
    assert "canonical_json=_report_paths(out_root, report_type)[0]" in helper
    assert "--runtime-dir" in helper
    assert "retryable=True" in helper
    assert "client.fetch_csrf()" in helper
    assert "MODE_CONFIRM_PHRASES" in helper
    assert "functional_port" in helper
    assert "--operational-campaign-report" in helper
    assert 'payloads["operational_campaign_24h"]' in helper
    assert "OPERATIONAL_CAMPAIGN_MIN_SECONDS = 86_400" in helper
    assert '{"production", "internal_test", "test", "dev_ready"}' in helper
    assert '_switch_live_mode(client, "dev_ready", notes="go_live trading stress precheck")' in helper


def test_full_generator_parallel_long_defaults_keep_live_pentest_foreground():
    args = argparse.Namespace(no_parallel_long_generators=False, parallel_live_pentest=False)

    selected = full_gate._background_report_types(args)

    assert selected == ("pytest", "functional", "cloud_drive_quota_permission")
    assert "pentest" not in selected


def test_full_generator_parallel_long_can_include_live_pentest_or_disable_parallelism():
    args = argparse.Namespace(no_parallel_long_generators=False, parallel_live_pentest=True)
    assert "pentest" in full_gate._background_report_types(args)

    args.no_parallel_long_generators = True
    assert full_gate._background_report_types(args) == ()


def test_go_live_scope_excludes_optional_product_suites_from_core_gate():
    helper_source = (ROOT / "scripts" / "security" / "gate" / "on_live_reports_make.py").read_text(encoding="utf-8")
    full_source = (ROOT / "scripts" / "security" / "gate" / "full_generator_live_validate.py").read_text(encoding="utf-8")
    functional_wrapper_source = (ROOT / "scripts" / "on_live_reports" / "functional.py").read_text(encoding="utf-8")
    pentest_source = (ROOT / "scripts" / "security" / "pentest" / "run_pentest.sh").read_text(encoding="utf-8")
    functional_source = (ROOT / "scripts" / "security" / "pentest" / "run_functional_smoke.sh").read_text(encoding="utf-8")
    permission_source = (ROOT / "scripts" / "security" / "pentest" / "functional_permission_pentest.py").read_text(encoding="utf-8")
    joined_targets = "\n".join(helper.GO_LIVE_CORE_PYTEST_TARGETS)

    assert "GO_LIVE_CORE_PYTEST_TARGETS" in helper_source
    assert "GO_LIVE_CORE_PYTEST_ARGS" in helper_source
    assert helper.GO_LIVE_CORE_PYTEST_TARGETS
    assert all("tests/" in target for target in helper.GO_LIVE_CORE_PYTEST_TARGETS)
    assert helper.GO_LIVE_CORE_PYTEST_ARGS[: len(helper.GO_LIVE_CORE_PYTEST_TARGETS)] == helper.GO_LIVE_CORE_PYTEST_TARGETS
    assert "-k" in helper.GO_LIVE_CORE_PYTEST_ARGS
    assert "tests/games/" not in joined_targets
    assert "tests/comfyui/" not in joined_targets
    assert "tests/frontend/comfyui/" not in joined_targets
    assert "tests/trading/" in joined_targets
    assert 'payloads["pytest"] = _pytest_report(out_root, raw_dir, "pytest", ["tests"]' not in helper_source
    assert '["tests"],' not in full_source
    assert "tests/games" not in helper_source
    assert "tests/comfyui" not in helper_source
    assert "functional-permissions" in helper.GO_LIVE_CORE_PENTEST_CHECKS
    assert "session-security" in helper.GO_LIVE_CORE_PENTEST_CHECKS
    assert "server-mode-v2-redteam-l2" in helper.GO_LIVE_CORE_PENTEST_CHECKS
    assert '"whole-site-production-gate"' not in helper.GO_LIVE_CORE_PENTEST_CHECKS
    assert '"video-module"' not in helper.GO_LIVE_CORE_PENTEST_CHECKS
    assert "--only" in helper_source and "GO_LIVE_CORE_PENTEST_CHECKS" in helper_source
    assert '"GO_LIVE_CORE_ONLY": "1"' in helper_source
    assert '"--core-only"' in helper_source
    assert 'smoke_args.append("--core-only")' in functional_wrapper_source
    assert 'env["GO_LIVE_CORE_ONLY"] = "1"' in functional_wrapper_source
    assert '"--qa-full" not in smoke_args and "--core-only" not in smoke_args' in functional_wrapper_source
    assert 'GO_LIVE_CORE_ONLY:-0}" == "1"' in pentest_source
    assert "--core-only" in permission_source
    assert 'GO_LIVE_CORE_ONLY:-0}" != "1"' in functional_source
    assert "--qa-full" in functional_source
    assert "--core-only" in functional_source
    assert "Scope: go-live core only; broad QA product workflows are skipped" in functional_source
    assert "Scope: QA full functional smoke" in functional_source
    assert "scope: \\`$FUNCTIONAL_SCOPE\\`" in functional_source
    assert 'if [[ "${GO_LIVE_CORE_ONLY:-0}" == "1" ]]; then\n    return 0\n  fi' in functional_source
    assert 'if [[ "${GO_LIVE_CORE_ONLY:-0}" != "1" ]]; then\n    create_forum_post_flow' in functional_source
    assert 'if [[ "${GO_LIVE_CORE_ONLY:-0}" != "1" ]]; then\n    login_smoke_user || return 1' in functional_source


def test_full_generator_passwords_default_to_environment(monkeypatch):
    monkeypatch.setenv("ROOT_PASSWORD", "RootFromEnv123!")
    monkeypatch.setenv("MANAGER_PASSWORD", "ManagerFromEnv123!")
    monkeypatch.setenv("TEST_PASSWORD", "TestFromEnv123!")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "full_generator_live_validate.py",
            "--runtime-dir",
            "/tmp/runtime",
            "--git-repo-dir",
            str(ROOT),
        ],
    )

    args = full_gate.parse_args()

    assert args.root_password == "RootFromEnv123!"
    assert args.manager_password == "ManagerFromEnv123!"
    assert args.test_password == "TestFromEnv123!"


def test_full_generator_login_failure_records_attempt_without_noninteractive_prompt(monkeypatch):
    class _FailingBrowser:
        def __init__(self):
            self.cookies = {}
            self.csrf = ""

        def login_with_rotation(self, username, password, *, rotate_to):
            return 401, {"ok": False, "msg": "bad password"}, password, [{"step": "login", "http_status": 401}]

    monkeypatch.setattr(full_gate.sys, "stdin", argparse.Namespace(isatty=lambda: False))
    args = argparse.Namespace(root_username="root", root_password="bad", root_new_password="next")

    status, payload, _, _, attempts = full_gate._browser_login_with_prompt(
        _FailingBrowser(),
        args,
        auto_root_new_password=False,
    )

    assert status == 401
    assert payload["msg"] == "bad password"
    assert attempts == [
        {
            "attempt": 1,
            "http_status": 401,
            "ok": False,
            "message": "bad password",
            "prompted_after_failure": False,
        }
    ]


def test_integrity_report_refreshes_csrf_before_mutating_calls():
    helper_source = (ROOT / "scripts" / "security" / "gate" / "on_live_reports_make.py").read_text(encoding="utf-8")

    assert "client.fetch_csrf()\n    review_status, review_payload, _ = client._request(\n        \"/api/root/integrity/findings/bulk-review\"" in helper_source
    assert "client.fetch_csrf()\n    rescan_status, rescan_payload, _ = client._request(\"/api/root/integrity/rescan\", method=\"POST\", body={})" in helper_source


def test_docs_and_frontend_expose_the_same_canonical_production_gate_paths():
    qa_docs = (ROOT / "docs" / "11_QA_TESTING.md").read_text(encoding="utf-8")
    prod_docs = (ROOT / "docs" / "02_DEPLOY_PRODUCTION.md").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )

    assert "python3 scripts/security/gate/on_live_reports_make.py --base-url https://127.0.0.1:5000" in qa_docs
    assert "python3 scripts/security/gate/on_live_reports_make.py --base-url https://<host>" in prod_docs
    assert "ROOT_PASSWORD='<root-password>'" in qa_docs
    assert "MANAGER_PASSWORD='<manager-password>'" in qa_docs
    assert "TEST_PASSWORD='<test-user-password>'" in qa_docs
    assert "--root-password" not in qa_docs
    assert "--root-password" not in prod_docs
    assert "runtime/reports/security/production_gate/log_chain_verify_report.json" in qa_docs
    assert "runtime/reports/security/production_gate/integrity_guard_report.json" in qa_docs
    assert "GET /api/root/server-mode/logs/verify" in qa_docs
    assert "`POST /api/root/integrity/rescan` + `GET /api/root/integrity/report`" in qa_docs
    assert "GET /api/root/server-mode/logs/verify" in admin_js
    assert "POST /api/root/integrity/rescan ＋ GET /api/root/integrity/report" in admin_js


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_live_client_retries_transient_get_errors(monkeypatch):
    client = helper.LiveClient("https://127.0.0.1:5002", timeout=1, max_retries=3, retry_backoff=0)
    attempts = {"count": 0}

    def fake_open(req, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise urllib.error.URLError("handshake timeout")
        return _FakeResponse({"ok": True, "csrf_token": "token-123"})

    monkeypatch.setattr(client.opener, "open", fake_open)
    status, payload, text = client._request("/api/csrf-token")

    assert attempts["count"] == 2
    assert status == 200
    assert payload["ok"] is True
    assert "token-123" in text


def test_resolve_output_root_prefers_runtime_dir(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "fresh-runtime"
    args = argparse.Namespace(runtime_dir=str(runtime_dir), out="")

    monkeypatch.delenv("HACKME_RUNTIME_DIR", raising=False)
    out_root = helper._resolve_output_root(args)

    assert out_root == (runtime_dir / "reports" / "security" / "production_gate").resolve()
    assert Path(os.environ["HACKME_RUNTIME_DIR"]).resolve() == runtime_dir.resolve()


def test_pick_available_port_falls_back_when_preferred_is_busy(monkeypatch):
    class _BusySocket:
        def bind(self, addr):
            raise OSError("busy")

        def close(self):
            return None

    class _FreeSocket:
        def __init__(self):
            self.bound = None

        def bind(self, addr):
            self.bound = addr

        def getsockname(self):
            return ("127.0.0.1", 54321)

        def close(self):
            return None

    sockets = [_BusySocket(), _FreeSocket()]
    monkeypatch.setattr(helper.socket, "socket", lambda *args, **kwargs: sockets.pop(0))

    chosen = helper._pick_available_port(50741)

    assert chosen == 54321


def test_make_payload_uses_meta_server_mode_by_default(tmp_path):
    captured = {}

    class _Signer:
        def build(self, **kwargs):
            captured.update(kwargs)
            return {
                "report_type": kwargs["report_type"],
                "test_result": kwargs["test_result"],
                "pass": kwargs["passed"],
                "report_hash": "sha256:" + ("a" * 64),
                "key_version": "local-dev-v1",
                "target_branch": kwargs["target_branch"],
                "target_commit": kwargs["target_commit"],
                "server_mode": kwargs["server_mode"],
                "report_source": kwargs["report_source"],
                "raw_report": kwargs["raw_report"],
            }

    payload = helper._make_payload(
        "clean_smoke",
        {
            "report_type": "clean_smoke",
            "status": "pass",
            "summary": "ok",
            "artifacts": {},
        },
        passed=True,
        tester="tests/scripts/security/test_on_live_reports_make_script.py",
        report_source="tests/scripts/security/test_on_live_reports_make_script.py",
        meta={
            "target_commit": "deadbeef",
            "target_branch": "main",
            "server_mode": "dev_ready",
        },
        canonical_json=tmp_path / "clean_smoke_report.json",
        canonical_md=tmp_path / "clean_smoke_report.md",
        signer=_Signer(),
    )

    assert captured["server_mode"] == "dev_ready"
    assert payload["server_mode"] == "dev_ready"


def test_operational_campaign_report_requires_formal_duration_scenarios_and_matching_source(tmp_path):
    from scripts.testing.operational_campaign_24h import manifest_digest, source_manifest

    class _Signer:
        def build(self, **kwargs):
            return {
                "report_type": kwargs["report_type"],
                "test_result": kwargs["test_result"],
                "pass": kwargs["passed"],
                "report_hash": "sha256:" + ("b" * 64),
                "signature": "hmac_sha256:" + ("c" * 64),
                "key_version": "test-v1",
                "target_branch": kwargs["target_branch"],
                "target_commit": kwargs["target_commit"],
                "server_mode": kwargs["server_mode"],
                "report_source": kwargs["report_source"],
                "raw_report": kwargs["raw_report"],
                "unresolved_findings": kwargs["unresolved"],
            }

    report_path = tmp_path / "operational_campaign_24h.json"
    report_path.write_text(json.dumps({
        "ok": True,
        "verdict": "PASS",
        "formal_campaign": True,
        "production_signoff_eligible": True,
        "active_test_seconds": 86_401,
        "required_active_test_seconds": 86_400,
        "authorization_wait_seconds_included": 0,
        "findings": [],
        "source_drift": {},
        "source_manifest_digest": manifest_digest(source_manifest()),
        "scenarios": {name: {"ok": True} for name in helper.OPERATIONAL_CAMPAIGN_SCENARIOS},
        "secret_scan": {"ok": True},
        "control_checks": {"primary": {"ok": True}, "recovery": {"ok": True}},
    }), encoding="utf-8")

    accepted = helper._operational_campaign_report(
        tmp_path / "gate",
        str(report_path),
        signer=_Signer(),
        meta={"target_commit": "abc", "target_branch": "main", "server_mode": "dev_ready"},
    )
    assert accepted["pass"] is True
    assert accepted["raw_report"]["campaign"]["checks"]["source_manifest_matches"] is True

    data = json.loads(report_path.read_text(encoding="utf-8"))
    data["source_manifest_digest"] = "stale-source"
    report_path.write_text(json.dumps(data), encoding="utf-8")
    rejected = helper._operational_campaign_report(
        tmp_path / "gate-stale",
        str(report_path),
        signer=_Signer(),
        meta={"target_commit": "abc", "target_branch": "main", "server_mode": "dev_ready"},
    )
    assert rejected["pass"] is False
    assert "source_manifest_matches" in rejected["unresolved_findings"]

    data.update({
        "source_manifest_digest": manifest_digest(source_manifest()),
        "findings": {},
        "source_drift": [],
        "scenarios": {name: "not-an-object" for name in helper.OPERATIONAL_CAMPAIGN_SCENARIOS},
        "secret_scan": [],
        "control_checks": {"primary": "not-an-object"},
    })
    report_path.write_text(json.dumps(data), encoding="utf-8")
    malformed = helper._operational_campaign_report(
        tmp_path / "gate-malformed",
        str(report_path),
        signer=_Signer(),
        meta={"target_commit": "abc", "target_branch": "main", "server_mode": "dev_ready"},
    )
    assert malformed["pass"] is False
    assert {
        "findings_shape",
        "source_drift_shape",
        "scenario_shape",
        "secret_scan_shape",
        "control_checks_shape",
    }.issubset(set(malformed["unresolved_findings"]))
