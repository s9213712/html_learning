from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import requests

from scripts.testing.formal_ai_agent_positive_operations_probe import (
    Api,
    ApiRequestConnectionError,
    INCIDENT_RECOVERY_CHILD_FLAG,
    INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION,
    SETTING_KEYS,
    _cleanup_orchestration_album,
    _cloud_drive_file_id,
    _exact_reward_row,
    _exact_governance_appeal,
    _exact_governance_violation,
    _incident_row_is_open,
    _incident_recovery_child_environment,
    _margin_cleanup_action,
    _margin_close_terminal_issues,
    _require_expected_provider_model,
    _settings_restore_payload,
    _run_browser_incident_recovery,
    _run_browser_incident_recovery_in_process,
    _validate_browser_incident_recovery,
    _verify_community_soft_delete,
    require_real_provider_configuration,
)


def test_governance_appeal_identity_is_exact_and_unique() -> None:
    row = {
        "id": 7,
        "user_id": 22,
        "username": "member",
        "latest_violation_id": 31,
        "status": "approved",
    }
    payload = {"appeals": [row], "violation_count": 0}

    assert _exact_governance_appeal(
        payload,
        violation_id=31,
        appeal_id=7,
        target_user_id=22,
        username="member",
    ) == row
    assert _exact_governance_appeal(
        {"appeals": []},
        violation_id=31,
        target_user_id=22,
        username="member",
        allow_absent=True,
    ) == {}
    with pytest.raises(RuntimeError, match="identity_count"):
        _exact_governance_appeal(
            {"appeals": [row, dict(row, id=8)]},
            violation_id=31,
            target_user_id=22,
            username="member",
        )
    with pytest.raises(RuntimeError, match="identity_mismatch"):
        _exact_governance_appeal(
            payload,
            violation_id=31,
            target_user_id=23,
            username="member",
        )


def test_governance_violation_recovery_requires_exact_append_only_identity() -> None:
    row = {
        "id": 31,
        "user_id": 22,
        "username": "member",
        "points": 1,
        "reason": "formal unique reason",
        "triggered_by": "super_admin",
        "actor_username": "root",
        "is_resolved": False,
    }
    payload = {"latest_violation": row, "violations": [dict(row)]}

    assert _exact_governance_violation(
        payload,
        reason="formal unique reason",
        target_user_id=22,
        username="member",
        actor_username="root",
    )["id"] == 31
    assert _exact_governance_violation(
        {"violations": []},
        reason="absent reason",
        target_user_id=22,
        username="member",
        actor_username="root",
        allow_absent=True,
    ) == {}
    with pytest.raises(RuntimeError, match="actor_mismatch"):
        _exact_governance_violation(
            {"violations": [dict(row, triggered_by="manager")]},
            reason="formal unique reason",
            target_user_id=22,
            username="member",
            actor_username="root",
        )


def _closed_margin_fixture() -> tuple[dict, dict, dict, dict, dict]:
    position = {
        "position_uuid": "margin-1",
        "status": "closed",
        "realized_pnl_points": -2,
    }
    funding_before = {
        "available_points": 100,
        "locked_points": 0,
        "wallet_locked_points": 0,
        "trial_locked_points": 0,
        "trial_deployed_points": 0,
        "missing_fields": [],
        "invalid_fields": [],
    }
    funding_after = dict(funding_before)
    funding_after["available_points"] = 98
    pool_before = {
        "outstanding_principal_points": 0,
        "missing_fields": [],
        "invalid_fields": [],
    }
    pool_after = {
        "balance_points": 801,
        "available_points": 801,
        "outstanding_principal_points": 0,
        "capacity_points": 801,
        "exchange_fund_balance_points": 1002,
        "exchange_fund_total_assets_points": 1002,
        "max_outstanding_principal_points": 801,
        "remaining_borrow_capacity_points": 801,
        "max_pool_utilization_percent": 80.0,
        "missing_fields": [],
        "invalid_fields": [],
    }
    return position, funding_before, funding_after, pool_before, pool_after


def test_margin_close_accepts_legitimate_fee_driven_capacity_change() -> None:
    position, funding_before, funding_after, pool_before, pool_after = _closed_margin_fixture()

    assert _margin_close_terminal_issues(
        position_uuid="margin-1",
        margin_terminal=position,
        funding_before=funding_before,
        funding_after=funding_after,
        pool_before=pool_before,
        pool_after=pool_after,
    ) == []


def test_margin_close_rejects_lock_principal_and_pool_equation_drift() -> None:
    position, funding_before, funding_after, pool_before, pool_after = _closed_margin_fixture()
    funding_after["trial_deployed_points"] = 1
    pool_after["outstanding_principal_points"] = 7

    issues = _margin_close_terminal_issues(
        position_uuid="margin-1",
        margin_terminal=position,
        funding_before=funding_before,
        funding_after=funding_after,
        pool_before=pool_before,
        pool_after=pool_after,
    )

    assert "funding_trial_deployed_points" in issues
    assert "outstanding_principal" in issues
    assert "pool_total_assets_equation" in issues
    assert "pool_available_equation" in issues


def test_margin_close_fails_closed_when_dashboard_fields_are_missing() -> None:
    position, funding_before, funding_after, pool_before, pool_after = _closed_margin_fixture()
    funding_after["missing_fields"] = ["funding.trial_credit.locked_points"]
    pool_after["missing_fields"] = ["exchange_fund_total_assets_points"]

    issues = _margin_close_terminal_issues(
        position_uuid="margin-1",
        margin_terminal=position,
        funding_before=funding_before,
        funding_after=funding_after,
        pool_before=pool_before,
        pool_after=pool_after,
    )

    assert "funding_after_missing_fields" in issues
    assert "pool_after_missing_fields" in issues


def test_margin_close_fails_closed_on_null_or_non_numeric_dashboard_fields() -> None:
    position, funding_before, funding_after, pool_before, pool_after = _closed_margin_fixture()
    funding_after["invalid_fields"] = ["funding.available_points"]
    pool_after["invalid_fields"] = ["capacity_points"]

    issues = _margin_close_terminal_issues(
        position_uuid="margin-1",
        margin_terminal=position,
        funding_before=funding_before,
        funding_after=funding_after,
        pool_before=pool_before,
        pool_after=pool_after,
    )

    assert "funding_after_invalid_fields" in issues
    assert "pool_after_invalid_fields" in issues


def test_margin_cleanup_is_state_aware_and_never_double_closes() -> None:
    assert _margin_cleanup_action(
        [{"position_uuid": "margin-1", "status": "open"}],
        "margin-1",
    ) == "close"
    assert _margin_cleanup_action(
        [{"position_uuid": "margin-1", "status": "closed"}],
        "margin-1",
    ) == "verify"
    assert _margin_cleanup_action(
        [{"position_uuid": "margin-1", "status": "liquidated"}],
        "margin-1",
    ) == "verify"
    with pytest.raises(RuntimeError, match="cleanup_margin_missing"):
        _margin_cleanup_action([], "margin-1")
    with pytest.raises(RuntimeError, match="cleanup_margin_unexpected_status"):
        _margin_cleanup_action(
            [{"position_uuid": "margin-1", "status": "cancelled"}],
            "margin-1",
        )


def test_governance_zero_count_is_compared_without_falsy_default() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "testing"
        / "formal_ai_agent_positive_operations_probe.py"
    ).read_text(encoding="utf-8")

    assert 'appeals_restored.get("violation_count") or -1' not in source
    assert 'restored.get("violation_count") or -1' not in source
    assert 'int(restored_appeal.get("user_id") or 0) == target_user_id' in source
    assert 'str(restored_appeal.get("username") or "") == user_two.username' in source


def test_member_appeal_list_selects_identity_and_restore_snapshot_fields() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "routes"
        / "appeals.py"
    ).read_text(encoding="utf-8")
    complete_projection = (
        'SELECT id, user_id, username, latest_violation_id, violation_count_snapshot, '
        'penalty_points, pre_status, pre_role, reason, status, reviewed_by, reviewed_at, '
        'review_note, created_at '
    )

    assert source.count(complete_projection) >= 3


def test_cloud_drive_identity_matches_create_and_terminal_list_schemas() -> None:
    file_id = "formal-file-identity"

    assert _cloud_drive_file_id({"file_id": file_id}) == file_id
    assert _cloud_drive_file_id({"id": file_id}) == file_id
    assert _cloud_drive_file_id({"storage_file_id": file_id}) == ""


class _Response:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    @staticmethod
    def json() -> dict:
        return {"ok": True}


class _Cookies:
    def __init__(self) -> None:
        self.value = "csrf-before"

    def get(self, name: str) -> str:
        return self.value if name == "csrf_token" else ""


class _Session:
    def __init__(self) -> None:
        self.cookies = _Cookies()
        self.verify = False

    def request(self, *_args, **_kwargs) -> _Response:
        self.cookies.value = "csrf-rotated"
        return _Response()


class _RetryResponse:
    def __init__(self, body: dict | None = None, *, status: int = 200) -> None:
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self._body = dict(body or {"ok": True})
        self.text = ""

    def json(self) -> dict:
        return dict(self._body)


class _RetrySession:
    def __init__(
        self,
        outcomes: list[object],
        *,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.outcomes = list(outcomes)
        self.cookies = requests.cookies.cookiejar_from_dict(dict(cookies or {}))
        self.verify = True
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def request(self, method: str, url: str, **_kwargs) -> _RetryResponse:
        self.calls.append((method, url))
        if not self.outcomes:
            raise AssertionError("unexpected extra API request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, _RetryResponse)
        return outcome

    def close(self) -> None:
        self.closed = True


class _SessionFactory:
    def __init__(self, sessions: list[_RetrySession]) -> None:
        self.sessions = list(sessions)
        self.call_count = 0

    def __call__(self) -> _RetrySession:
        self.call_count += 1
        if not self.sessions:
            raise AssertionError("unexpected fresh session")
        return self.sessions.pop(0)


class _AlbumBrowserApi:
    def __init__(
        self,
        albums: list[dict],
        *,
        list_status: int = 200,
        silent_delete: bool = False,
    ) -> None:
        self.albums = [dict(row) for row in albums]
        self.list_status = list_status
        self.silent_delete = silent_delete
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, _body=None) -> dict:
        method = method.upper()
        self.calls.append((method, path))
        if method == "GET" and path == "/api/storage/albums":
            return {
                "status": self.list_status,
                "body": {
                    "ok": self.list_status == 200,
                    "albums": [dict(row) for row in self.albums],
                },
            }
        prefix = "/api/storage/albums/"
        album_id = path.removeprefix(prefix) if path.startswith(prefix) else ""
        matching = [row for row in self.albums if str(row.get("id") or "") == album_id]
        if method == "DELETE" and album_id:
            if not matching:
                return {"status": 404, "body": {"ok": False}}
            if not self.silent_delete:
                self.albums = [
                    row for row in self.albums if str(row.get("id") or "") != album_id
                ]
            return {"status": 200, "body": {"ok": True}}
        if method == "GET" and album_id:
            if matching:
                return {"status": 200, "body": {"ok": True, "album": dict(matching[0])}}
            return {"status": 404, "body": {"ok": False}}
        raise AssertionError(f"unexpected browser API call: {method} {path}")


class _CommunityReadApi:
    def __init__(self, username: str, record: dict) -> None:
        self.username = username
        self.record = dict(record)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str) -> dict:
        self.calls.append((method, path))
        return dict(self.record)


def test_community_soft_delete_requires_root_audit_row_and_member_404() -> None:
    root = _CommunityReadApi(
        "root",
        {
            "status": 200,
            "body": {
                "ok": True,
                "thread": {
                    "id": 73,
                    "is_deleted": True,
                    "deleted_at": "2026-07-15T17:00:00+08:00",
                    "deleted_by": "root",
                },
            },
        },
    )
    member = _CommunityReadApi(
        "ordinary-member",
        {"status": 404, "body": {"ok": False, "msg": "not found"}},
    )

    receipt = _verify_community_soft_delete(root, member, 73)  # type: ignore[arg-type]

    assert receipt == {
        "thread_id": 73,
        "root_audit_status": 200,
        "root_audit_is_deleted": True,
        "root_audit_deleted_at": "2026-07-15T17:00:00+08:00",
        "root_audit_deleted_by": "root",
        "member_username": "ordinary-member",
        "member_absent_status": 404,
    }
    assert root.calls == [("GET", "/api/community/threads/73")]
    assert member.calls == [("GET", "/api/community/threads/73")]


def test_community_soft_delete_rejects_visible_root_row_not_marked_deleted() -> None:
    root = _CommunityReadApi(
        "root",
        {"status": 200, "body": {"ok": True, "thread": {"id": 73, "is_deleted": False}}},
    )
    member = _CommunityReadApi("ordinary-member", {"status": 404, "body": {"ok": False}})

    with pytest.raises(RuntimeError, match="root_audit_not_deleted"):
        _verify_community_soft_delete(root, member, 73)  # type: ignore[arg-type]
    assert member.calls == []


def test_community_soft_delete_rejects_member_visibility_after_root_audit_passes() -> None:
    root = _CommunityReadApi(
        "root",
        {"status": 200, "body": {"ok": True, "thread": {"id": 73, "is_deleted": True}}},
    )
    member = _CommunityReadApi(
        "ordinary-member",
        {"status": 200, "body": {"ok": True, "thread": {"id": 73, "is_deleted": True}}},
    )

    with pytest.raises(RuntimeError, match="member_not_absent"):
        _verify_community_soft_delete(root, member, 73)  # type: ignore[arg-type]


def test_api_uses_rotated_csrf_cookie_after_each_mutation() -> None:
    api = Api("http://unit.test", "member", "secret")
    api.session = _Session()  # type: ignore[assignment]
    api.csrf = "csrf-before"

    result = api.request("POST", "/api/example", json_body={"value": 1})

    assert result["status"] == 200
    assert api.csrf == "csrf-rotated"


def test_api_get_retries_one_disconnect_on_fresh_session_with_cookies() -> None:
    first = _RetrySession(
        [requests.ConnectionError("remote disconnected")],
        cookies={"session": "member-session", "csrf_token": "csrf-preserved"},
    )
    second = _RetrySession([_RetryResponse()])
    factory = _SessionFactory([first, second])
    api = Api(
        "http://unit.test",
        "member",
        "secret",
        session_factory=factory,  # type: ignore[arg-type]
    )

    result = api.request("GET", "/api/community/posts/7")

    evidence = result["request_evidence"]
    assert result["status"] == 200
    assert evidence["method"] == "GET"
    assert evidence["path"] == "/api/community/posts/7"
    assert evidence["attempt_count"] == 2
    assert evidence["retry_performed"] is True
    assert evidence["terminal"] == "response"
    assert [row["outcome"] for row in evidence["attempts"]] == [
        "connection_error",
        "response",
    ]
    assert evidence["attempts"][0]["no_response"] is True
    assert "remote disconnected" in evidence["attempts"][0]["error"]
    assert first.closed is True
    assert second.cookies.get("session") == "member-session"
    assert second.cookies.get("csrf_token") == "csrf-preserved"
    assert len(first.calls) == len(second.calls) == 1
    assert factory.call_count == 2


def test_api_get_fails_after_second_disconnect_with_terminal_evidence() -> None:
    first = _RetrySession([requests.ConnectionError("first disconnect")])
    second = _RetrySession([requests.ConnectionError("second disconnect")])
    factory = _SessionFactory([first, second])
    api = Api(
        "http://unit.test",
        "member",
        "secret",
        session_factory=factory,  # type: ignore[arg-type]
    )

    with pytest.raises(ApiRequestConnectionError) as caught:
        api.request("GET", "/api/community/posts/7")

    evidence = caught.value.request_evidence
    assert evidence["method"] == "GET"
    assert evidence["path"] == "/api/community/posts/7"
    assert evidence["attempt_count"] == 2
    assert evidence["retry_allowed"] is True
    assert evidence["retry_performed"] is True
    assert evidence["terminal"] == "connection_error"
    assert [row["error"] for row in evidence["attempts"]] == [
        "ConnectionError: first disconnect",
        "ConnectionError: second disconnect",
    ]
    assert '"method":"GET"' in str(caught.value)
    assert '"path":"/api/community/posts/7"' in str(caught.value)
    assert factory.call_count == 2
    assert len(first.calls) == len(second.calls) == 1


def test_api_post_disconnect_is_never_retried_and_names_method_and_path() -> None:
    only = _RetrySession([requests.ConnectionError("write disconnect")])
    factory = _SessionFactory([only])
    api = Api(
        "http://unit.test",
        "member",
        "secret",
        session_factory=factory,  # type: ignore[arg-type]
    )
    api.csrf = "csrf"

    with pytest.raises(ApiRequestConnectionError) as caught:
        api.request("POST", "/api/trading/order", json_body={"side": "buy"})

    evidence = caught.value.request_evidence
    assert evidence["method"] == "POST"
    assert evidence["path"] == "/api/trading/order"
    assert evidence["attempt_count"] == 1
    assert evidence["retry_allowed"] is False
    assert evidence["retry_performed"] is False
    assert evidence["terminal"] == "connection_error"
    assert "write disconnect" in evidence["terminal_error"]
    assert '"method":"POST"' in str(caught.value)
    assert '"path":"/api/trading/order"' in str(caught.value)
    assert factory.call_count == 1
    assert len(only.calls) == 1


def test_api_refresh_csrf_uses_safe_get_retry_policy() -> None:
    first = _RetrySession(
        [requests.ConnectionError("csrf disconnect")],
        cookies={"session": "member-session", "csrf_token": "csrf-old"},
    )
    second = _RetrySession(
        [_RetryResponse({"ok": True, "csrf_token": "csrf-fresh"})]
    )
    factory = _SessionFactory([first, second])
    api = Api(
        "http://unit.test",
        "member",
        "secret",
        session_factory=factory,  # type: ignore[arg-type]
    )

    result = api.refresh_csrf()

    assert api.csrf == "csrf-fresh"
    assert result["request_evidence"]["method"] == "GET"
    assert result["request_evidence"]["retry_performed"] is True
    assert second.cookies.get("session") == "member-session"


def _browser_incident_receipt() -> dict:
    incident_id = "incident-formal-1"
    return {
        "browser": {
            "engine": "chromium",
            "user_agent": "Mozilla/5.0 HeadlessChrome/140.0 Safari/537.36",
            "webdriver": True,
        },
        "login": {"status": 200, "body": {"ok": True}},
        "csrf_after_login_status": 200,
        "me": {
            "status": 200,
            "body": {"ok": True, "username": "root", "role": "super_admin"},
        },
        "incident_before": {
            "status": 200,
            "body": {
                "ok": True,
                "incident": {
                    "id": incident_id,
                    "status": "open",
                    "reason": "formal owned incident",
                },
            },
        },
        "resolve": {
            "status": 200,
            "body": {
                "ok": True,
                "tool": "write_incident_resolve",
                "status": 200,
                "action_policy": {
                    "actor_role": "super_admin",
                    "operation_mode": "write",
                },
                "result": {"ok": True, "incident_id": incident_id},
            },
        },
        "csrf_before_resolve_login_status": 200,
        "post_resolve_login": {"status": 200, "body": {"ok": True}},
        "csrf_after_resolve_login_status": 200,
        "post_resolve_me": {
            "status": 200,
            "body": {"ok": True, "username": "root", "role": "super_admin"},
        },
        "incident_after": {"status": 200, "body": {"ok": True, "incident": None}},
        "mode_after": {
            "status": 200,
            "body": {"ok": True, "mode": {"current_mode": "dev_ready"}},
        },
        "already_resolved": False,
        "screenshot_bytes": 1024,
        "browser_requests": [
            {
                "method": method,
                "path": path,
                "maintenance_bypass_header_present": False,
            }
            for method, path in (
                ("GET", "/api/csrf-token"),
                ("POST", "/api/login"),
                ("GET", "/api/me"),
                ("GET", "/api/root/incident/status"),
                ("POST", "/api/ai-agent/write-tools/execute"),
            )
        ],
    }


def test_browser_incident_recovery_requires_real_root_sessions_and_ai_gateway() -> None:
    receipt = _browser_incident_receipt()

    validated = _validate_browser_incident_recovery(
        receipt,
        expected_incident_id="incident-formal-1",
        expected_reason="formal owned incident",
        expected_mode="dev_ready",
    )

    assert validated["resolve"]["body"]["tool"] == "write_incident_resolve"
    assert validated["me"]["body"]["username"] == "root"
    assert validated["post_resolve_me"]["body"]["username"] == "root"


def test_incident_row_open_state_does_not_treat_closed_history_as_active() -> None:
    assert _incident_row_is_open({"id": "one", "status": "open"}) is True
    assert _incident_row_is_open({"id": "one", "status": "resolving"}) is True
    assert _incident_row_is_open({"id": "one", "status": "resolved", "active": False}) is False
    assert _incident_row_is_open({"id": "one", "status": "closed"}) is False
    assert _incident_row_is_open(None) is False


def test_browser_incident_recovery_rejects_login_200_with_immediately_stale_session() -> None:
    receipt = _browser_incident_receipt()
    receipt["me"] = {"status": 401, "body": {"ok": False, "msg": "未登入"}}

    with pytest.raises(RuntimeError, match="root_session_terminal"):
        _validate_browser_incident_recovery(
            receipt,
            expected_incident_id="incident-formal-1",
            expected_reason="formal owned incident",
            expected_mode="dev_ready",
        )


def test_browser_incident_recovery_rejects_bypass_wrong_tool_and_missing_post_login() -> None:
    receipt = _browser_incident_receipt()
    receipt["browser_requests"][-1]["maintenance_bypass_header_present"] = True
    receipt["resolve"]["body"]["tool"] = "write_server_mode_switch"
    receipt["post_resolve_login"] = {"status": 401, "body": {"ok": False}}

    with pytest.raises(RuntimeError) as error:
        _validate_browser_incident_recovery(
            receipt,
            expected_incident_id="incident-formal-1",
            expected_reason="formal owned incident",
            expected_mode="dev_ready",
        )

    message = str(error.value)
    assert "maintenance_bypass_header" in message
    assert "ai_incident_resolve" in message
    assert "post_resolve_root_login" in message


def test_browser_incident_recovery_failure_preserves_bounded_gateway_diagnostics() -> None:
    receipt = _browser_incident_receipt()
    receipt["resolve"] = {
        "status": 400,
        "body": {
            "ok": False,
            "tool": "write_incident_resolve",
            "status": 400,
            "result": {
                "ok": False,
                "incident_id": "incident-formal-1",
                "msg": "目前 server mode 不是 incident_lockdown",
            },
        },
    }
    receipt["incident_after"] = receipt["incident_before"]

    with pytest.raises(RuntimeError) as error:
        _validate_browser_incident_recovery(
            receipt,
            expected_incident_id="incident-formal-1",
            expected_reason="formal owned incident",
            expected_mode="dev_ready",
        )

    message = str(error.value)
    assert "ai_incident_resolve" in message
    assert '"resolve_http_status": 400' in message
    assert "server mode" in message
    assert len(message) < 2400


def test_browser_incident_recovery_implementation_cannot_forge_browser_identity() -> None:
    import inspect

    parent_source = inspect.getsource(_run_browser_incident_recovery)
    child_source = inspect.getsource(_run_browser_incident_recovery_in_process)

    assert "sync_playwright" not in parent_source
    assert "start_new_session=True" in parent_source
    assert "os.killpg" in parent_source
    assert "stdin=subprocess.PIPE" in parent_source
    assert "INCIDENT_RECOVERY_CHILD_FLAG" in parent_source
    assert "sync_playwright" in child_source
    assert "playwright.chromium.launch" in child_source
    assert '"tool": "write_incident_resolve"' in child_source
    assert "user_agent=" not in child_source
    assert "set_extra_http_headers" not in child_source
    assert "X-Maintenance-Bypass-Token" not in child_source


class _CompletedIncidentChild:
    def __init__(self, stdout: str, *, stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 76543
        self.stdin_payload = ""
        self.timeout: float | None = None

    def communicate(self, input: str | None = None, timeout: float | None = None):
        if input is not None:
            self.stdin_payload = input
        self.timeout = timeout
        return self.stdout, self.stderr


def _incident_recovery_call_kwargs(tmp_path: Path) -> dict:
    return {
        "base_url": "https://127.0.0.1:54871",
        "password": "root-password-only-through-stdin",
        "expected_incident_id": "incident-formal-1",
        "expected_reason": "formal owned incident",
        "expected_mode": "dev_ready",
        "notes": "formal recovery",
        "verification": {"transport": "playwright_chromium"},
        "artifact_dir": tmp_path,
        "suffix": "unit-child",
    }


def test_browser_incident_recovery_uses_stdin_only_isolated_machine_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "root-password-only-through-stdin"
    monkeypatch.setenv("ROOT_PASSWORD", password)
    monkeypatch.setenv("HACKME_PROBE_ROOT_PASSWORD", password)
    child_output = json.dumps({
        "schema_version": INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION,
        "ok": True,
        "receipt": _browser_incident_receipt(),
    })
    child = _CompletedIncidentChild(child_output)
    captured: dict = {}

    def popen_factory(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = dict(kwargs)
        return child

    receipt = _run_browser_incident_recovery(
        **_incident_recovery_call_kwargs(tmp_path),
        _popen_factory=popen_factory,
        _timeout_seconds=37,
    )

    assert captured["command"][-1] == INCIDENT_RECOVERY_CHILD_FLAG
    assert password not in captured["command"]
    assert password not in str(captured["kwargs"]["cwd"])
    assert "ROOT_PASSWORD" not in captured["kwargs"]["env"]
    assert "HACKME_PROBE_ROOT_PASSWORD" not in captured["kwargs"]["env"]
    assert not any(str(key).upper().endswith("_PASSWORD") for key in captured["kwargs"]["env"])
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stdin"] is subprocess.PIPE
    assert captured["kwargs"]["stdout"] is subprocess.PIPE
    assert captured["kwargs"]["stderr"] is subprocess.PIPE
    assert captured["kwargs"]["close_fds"] is True
    input_payload = json.loads(child.stdin_payload)
    assert input_payload["password"] == password
    assert input_payload["schema_version"] == INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION
    assert child.timeout == 37
    assert receipt["recovery_subprocess"]["isolated_python_process"] is True
    assert receipt["recovery_subprocess"]["start_new_session"] is True
    assert receipt["recovery_subprocess"]["returncode"] == 0


def test_incident_recovery_child_environment_scrubs_all_root_password_aliases() -> None:
    cleaned = _incident_recovery_child_environment({
        "ROOT_PASSWORD": "one",
        "HTML_LEARNING_ROOT_PASSWORD": "two",
        "HACKME_CAMPAIGN_ROOT_PASSWORD": "three",
        "HACKME_PROBE_MANAGER_PASSWORD": "four",
        "SAFE_VALUE": "preserved",
    })

    assert cleaned == {"SAFE_VALUE": "preserved"}


def test_incident_recovery_child_entrypoint_always_emits_machine_json() -> None:
    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "testing"
        / "formal_ai_agent_positive_operations_probe.py"
    )
    completed = subprocess.run(
        [sys.executable, str(script), INCIDENT_RECOVERY_CHILD_FLAG],
        input="{}",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        cwd=str(script.parents[2]),
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert payload["schema_version"] == INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION
    assert payload["ok"] is False
    assert payload["error_type"] == "RuntimeError"
    assert "payload_fields_invalid" in payload["error"]


def test_browser_incident_recovery_timeout_kills_the_entire_child_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutChild(_CompletedIncidentChild):
        def __init__(self) -> None:
            super().__init__("")
            self.calls = 0

        def communicate(self, input: str | None = None, timeout: float | None = None):
            self.calls += 1
            if self.calls == 1:
                self.stdin_payload = str(input or "")
                raise subprocess.TimeoutExpired("incident-child", timeout)
            self.returncode = -signal.SIGKILL
            return "", ""

    child = TimeoutChild()
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "scripts.testing.formal_ai_agent_positive_operations_probe.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    with pytest.raises(RuntimeError, match="incident_recovery_child_timeout:0.01s"):
        _run_browser_incident_recovery(
            **_incident_recovery_call_kwargs(tmp_path),
            _popen_factory=lambda _command, **_kwargs: child,
            _timeout_seconds=0.01,
        )

    assert killed == [(child.pid, signal.SIGKILL)]
    assert child.calls == 2


def test_browser_incident_recovery_child_failure_redacts_password(
    tmp_path: Path,
) -> None:
    password = "root-password-only-through-stdin"
    child = _CompletedIncidentChild(
        json.dumps({
            "schema_version": INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION,
            "ok": False,
            "error_type": "RuntimeError",
            "error": f"login failed for {password}",
        }),
        stderr=f"browser stderr {password}",
        returncode=1,
    )

    with pytest.raises(RuntimeError) as caught:
        _run_browser_incident_recovery(
            **_incident_recovery_call_kwargs(tmp_path),
            _popen_factory=lambda _command, **_kwargs: child,
        )

    assert password not in str(caught.value)
    assert "<redacted>" in str(caught.value)


def test_formal_settings_contract_includes_audit_chain_and_reseal_state() -> None:
    assert "audit_chain_enabled" in SETTING_KEYS
    assert "audit_chain_reseal_required" in SETTING_KEYS
    assert {
        "ai_agent_provider",
        "ai_agent_api_base_url",
        "ai_agent_model",
        "ai_agent_allowed_models",
    }.issubset(SETTING_KEYS)
    assert "feature_cloud_drive_enabled" not in SETTING_KEYS
    assert "feature_privacy_uploads_enabled" in SETTING_KEYS
    assert "feature_storage_albums_enabled" in SETTING_KEYS
    assert len(SETTING_KEYS) == 17


def test_formal_provider_configuration_requires_explicit_real_model() -> None:
    with pytest.raises(RuntimeError, match="API_BASE_URL is required"):
        require_real_provider_configuration({})
    with pytest.raises(RuntimeError, match="MODEL is required"):
        require_real_provider_configuration({
            "HACKME_CAMPAIGN_AI_AGENT_API_BASE_URL": "http://127.0.0.1:11434/v1",
        })

    assert require_real_provider_configuration({
        "HACKME_CAMPAIGN_AI_AGENT_API_BASE_URL": "http://127.0.0.1:11434/v1/",
        "HACKME_CAMPAIGN_AI_AGENT_MODEL": "qwen3.5:cloud",
    }) == {
        "ai_agent_provider": "openai_compatible",
        "ai_agent_api_base_url": "http://127.0.0.1:11434/v1",
        "ai_agent_model": "qwen3.5:cloud",
        "ai_agent_allowed_models": "qwen3.5:cloud",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.18.19:11434/v1",
        "http://user:secret@127.0.0.1:11434/v1",
        "http://127.0.0.1:11434/v1?token=secret",
        "ftp://127.0.0.1:11434/v1",
        "http://127.0.0.1:11434/arbitrary",
    ],
)
def test_formal_provider_configuration_rejects_unsafe_origin(url: str) -> None:
    with pytest.raises(RuntimeError):
        require_real_provider_configuration({
            "HACKME_CAMPAIGN_AI_AGENT_API_BASE_URL": url,
            "HACKME_CAMPAIGN_AI_AGENT_MODEL": "qwen3.5:cloud",
        })


def test_formal_provider_response_must_match_frozen_model_identity() -> None:
    assert _require_expected_provider_model("qwen3.5:cloud", "qwen3.5:cloud") == "qwen3.5:cloud"
    assert _require_expected_provider_model("qwen3.5", "qwen3.5:cloud") == "qwen3.5"

    with pytest.raises(RuntimeError, match="model_missing"):
        _require_expected_provider_model("", "qwen3.5:cloud")
    with pytest.raises(RuntimeError, match="model_mismatch"):
        _require_expected_provider_model("qwen2.5:3b", "qwen3.5:cloud")
    with pytest.raises(RuntimeError, match="model_mismatch"):
        _require_expected_provider_model("qwen3.5:latest", "qwen3.5:cloud")


def test_settings_restore_confirms_the_dangerous_exact_snapshot() -> None:
    payload = _settings_restore_payload({"audit_chain_enabled": False, "feature_ai_agent_enabled": False})

    assert payload["audit_chain_enabled"] is False
    assert payload["feature_ai_agent_enabled"] is False
    assert set(payload["dangerous_confirm"]) == set(SETTING_KEYS)


def test_browser_preflight_uses_status_entrypoint_to_reconcile_authenticated_scope() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "testing"
        / "formal_ai_agent_positive_operations_probe.py"
    ).read_text(encoding="utf-8")

    assert "while (aiAgentCurrentAccountScope() === 'anonymous'" in source
    assert "ai_agent_authenticated_account_not_settled" in source
    assert "AI_AGENT_STATE.accountScope !== accountScope" in source
    assert "ai_agent_authenticated_account_scope_not_reconciled" in source
    assert source.index("ai_agent_authenticated_account_not_settled") < source.index(
        "await loadAiAgentStatus({force: true})"
    )
    assert source.index("await loadAiAgentStatus({force: true})") < source.index(
        "ai_agent_authenticated_account_scope_not_reconciled"
    )


def test_orchestration_cleanup_recovers_missing_id_from_unique_run_title() -> None:
    api = _AlbumBrowserApi([
        {"id": "unrelated", "title": "Existing album"},
        {"id": "run-album", "title": "Formal Agent Orchestration run-123"},
    ])

    receipt = _cleanup_orchestration_album(
        api,
        album_title="Formal Agent Orchestration run-123",
    )

    assert receipt["album_id"] == "run-album"
    assert receipt["delete_status"] == 200
    assert receipt["album_absent_status"] == 404
    assert receipt["album_absent"] is True
    assert api.albums == [{"id": "unrelated", "title": "Existing album"}]
    assert ("DELETE", "/api/storage/albums/unrelated") not in api.calls


def test_orchestration_cleanup_does_not_delete_when_run_title_is_absent() -> None:
    api = _AlbumBrowserApi([{"id": "unrelated", "title": "Existing album"}])

    receipt = _cleanup_orchestration_album(
        api,
        album_title="Formal Agent Orchestration absent-run",
    )

    assert receipt["album_absent"] is True
    assert receipt["inventory_match_count"] == 0
    assert api.albums == [{"id": "unrelated", "title": "Existing album"}]
    assert all(method != "DELETE" for method, _path in api.calls)


def test_orchestration_cleanup_fails_closed_on_ambiguous_or_mismatched_identity() -> None:
    title = "Formal Agent Orchestration duplicate-run"
    ambiguous = _AlbumBrowserApi([
        {"id": "one", "title": title},
        {"id": "two", "title": title},
    ])
    with pytest.raises(RuntimeError, match="title_ambiguous"):
        _cleanup_orchestration_album(ambiguous, album_title=title)
    assert all(method != "DELETE" for method, _path in ambiguous.calls)

    mismatched = _AlbumBrowserApi([
        {"id": "captured", "title": "Somebody else's album"},
        {"id": "actual-run", "title": title},
    ])
    with pytest.raises(RuntimeError, match="identity_mismatch"):
        _cleanup_orchestration_album(
            mismatched,
            album_title=title,
            album_id="captured",
        )
    assert all(method != "DELETE" for method, _path in mismatched.calls)


def test_orchestration_cleanup_fails_closed_when_inventory_cannot_be_reopened() -> None:
    api = _AlbumBrowserApi([], list_status=503)

    with pytest.raises(RuntimeError, match="cleanup_list_failed"):
        _cleanup_orchestration_album(
            api,
            album_title="Formal Agent Orchestration list-failure",
        )


def test_orchestration_cleanup_requires_terminal_absence_after_delete() -> None:
    title = "Formal Agent Orchestration silent-delete"
    api = _AlbumBrowserApi(
        [{"id": "run-album", "title": title}],
        silent_delete=True,
    )

    with pytest.raises(RuntimeError, match="cleanup_not_terminal"):
        _cleanup_orchestration_album(
            api,
            album_title=title,
            album_id="run-album",
        )


def test_exact_reward_row_requires_one_new_confirmed_identity() -> None:
    row = {
        "id": 12,
        "ledger_uuid": "ledger-12",
        "action_type": "forum_post_reward",
        "reference_type": "forum_thread",
        "reference_id": "7",
        "direction": "credit",
        "status": "confirmed",
        "amount": 3,
    }

    selected = _exact_reward_row(
        {"ledger": [row]},
        after_id=11,
        action_type="forum_post_reward",
        reference_type="forum_thread",
        reference_id=7,
    )

    assert selected["ledger_uuid"] == "ledger-12"

    with pytest.raises(RuntimeError, match="community_reward_ledger_identity_invalid"):
        _exact_reward_row(
            {"ledger": [row, dict(row, ledger_uuid="duplicate")]},
            after_id=11,
            action_type="forum_post_reward",
            reference_type="forum_thread",
            reference_id=7,
        )
