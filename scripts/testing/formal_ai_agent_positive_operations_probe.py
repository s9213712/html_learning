#!/usr/bin/env python3
"""Strict live AI Agent positive-operation and operations-assistance probe.

The probe uses the public AI write-tool gateway for every claimed agent
operation, then independently reopens terminal product state through the
domain APIs.  It deliberately avoids provider mocks and expected-gap
semantics.  The supervised restart request is left for the campaign
orchestrator to consume; no detached replacement server is ever spawned.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urljoin, urlsplit

import requests


SCHEMA_VERSION = "hackme.formal-ai-agent-positive-operations-probe/v1"
INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION = "hackme.formal-ai-agent-incident-recovery-child/v1"
INCIDENT_RECOVERY_CHILD_FLAG = "--incident-recovery-child"
INCIDENT_RECOVERY_CHILD_TIMEOUT_SECONDS = 180.0
INCIDENT_RECOVERY_CHILD_INPUT_LIMIT_BYTES = 256 * 1024
ROOT = Path(__file__).resolve().parents[2]
SETTING_KEYS = (
    "feature_ai_agent_enabled",
    "feature_privacy_uploads_enabled",
    "feature_storage_albums_enabled",
    "feature_videos_enabled",
    "feature_trading_enabled",
    "feature_community_enabled",
    "feature_member_governance_enabled",
    "feature_audit_log_enabled",
    "audit_chain_enabled",
    "audit_chain_reseal_required",
    "module_ai_agent_min_role",
    "ai_agent_allowed_tools",
    "ai_agent_operation_mode",
    "ai_agent_provider",
    "ai_agent_api_base_url",
    "ai_agent_model",
    "ai_agent_allowed_models",
)


def require_real_provider_configuration(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an explicit, non-secret provider contract for the formal run.

    A fresh isolated runtime otherwise inherits the development Hermes default
    (``127.0.0.1:8642`` with no model), which can make the browser planner fail
    for configuration reasons or accidentally exercise a stale local service.
    Formal evidence must name the exact OpenAI-compatible origin and model.
    Plain HTTP is accepted only on loopback so prompts are never sent over an
    unauthenticated network path.
    """

    env = environ if environ is not None else os.environ
    raw_url = str(env.get("HACKME_CAMPAIGN_AI_AGENT_API_BASE_URL") or "").strip().rstrip("/")
    model = str(env.get("HACKME_CAMPAIGN_AI_AGENT_MODEL") or "").strip()
    if not raw_url:
        raise RuntimeError("HACKME_CAMPAIGN_AI_AGENT_API_BASE_URL is required")
    if not model:
        raise RuntimeError("HACKME_CAMPAIGN_AI_AGENT_MODEL is required")
    if len(model) > 200 or any(character in model for character in "\r\n\x00, "):
        raise RuntimeError("HACKME_CAMPAIGN_AI_AGENT_MODEL is invalid")
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise RuntimeError("campaign AI Agent API base URL must be an http(s) origin or /v1 endpoint")
    if parsed.scheme == "http" and str(parsed.hostname).lower() not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise RuntimeError("plaintext campaign AI Agent API base URL must be loopback")
    return {
        "ai_agent_provider": "openai_compatible",
        "ai_agent_api_base_url": raw_url,
        "ai_agent_model": model,
        "ai_agent_allowed_models": model,
    }


def _require_expected_provider_model(model: Any, expected_model: str) -> str:
    """Fail closed when a provider silently serves a different model.

    Ollama's OpenAI-compatible cloud transport reports ``qwen3.5`` for a
    request whose inventory/configured alias is ``qwen3.5:cloud``.  That one
    suffix normalization is accepted; no other tag, family, or fuzzy match is.
    """

    actual = str(model or "").strip()
    expected = str(expected_model or "").strip()
    if not actual:
        raise RuntimeError("real_provider_chat_model_missing")
    allowed = {expected}
    if expected.endswith(":cloud") and len(expected) > len(":cloud"):
        allowed.add(expected[: -len(":cloud")])
    if not expected or actual not in allowed:
        raise RuntimeError(f"real_provider_chat_model_mismatch:{actual}:{expected}")
    return actual


def _settings_restore_payload(before_settings: Mapping[str, Any]) -> dict[str, Any]:
    """Restore the exact snapshot through the public dangerous-change gate."""

    return {
        **dict(before_settings),
        "dangerous_confirm": list(SETTING_KEYS),
    }


def _body(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        payload = {"text_sample": response.text[:500]}
    return payload if isinstance(payload, dict) else {"value": payload}


def _record(response: requests.Response) -> dict[str, Any]:
    return {
        "status": int(response.status_code),
        "body": _body(response),
        "content_type": str(response.headers.get("Content-Type") or ""),
    }


class ApiRequestConnectionError(requests.ConnectionError):
    """Terminal no-response transport error with machine-readable evidence."""

    def __init__(self, evidence: Mapping[str, Any]):
        self.request_evidence = dict(evidence)
        encoded = json.dumps(
            self.request_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        super().__init__(f"api_request_connection_error:{encoded}")


class Api:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._session_factory = session_factory
        self.session = self._new_session()
        self.csrf = ""
        self.successful_tools: list[str] = []

    def _new_session(self) -> requests.Session:
        session = self._session_factory()
        session.verify = False
        return session

    def _fresh_session_preserving_cookies(self) -> None:
        previous = self.session
        cookie_snapshot = previous.cookies.copy()
        try:
            previous.close()
        finally:
            replacement = self._new_session()
        replacement.cookies.update(cookie_snapshot)
        self.session = replacement

    def sync_csrf_cookie(self) -> None:
        """Use the newest rotating CSRF cookie after every mutation.

        The application rotates CSRF tokens on successful state-changing
        requests and retains only a bounded history.  Reusing the login-time
        token would make a long positive-operation sequence fail after the
        old token is pruned even though the session itself remains valid.
        """

        rotated = str(self.session.cookies.get("csrf_token") or "")
        if rotated:
            self.csrf = rotated

    def refresh_csrf(self) -> dict[str, Any]:
        result = self.request("GET", "/api/csrf-token", timeout=30)
        if int(result.get("status") or 0) != 200:
            raise RuntimeError(
                f"csrf_refresh_failed:{self.username}:"
                f"{json.dumps(result, ensure_ascii=False)[:1000]}"
            )
        self.csrf = str(
            (result.get("body") or {}).get("csrf_token")
            or self.session.cookies.get("csrf_token")
            or ""
        )
        if not self.csrf:
            raise RuntimeError(f"csrf_missing:{self.username}")
        return result

    def login(self) -> dict[str, Any]:
        self.refresh_csrf()
        result = self.request(
            "POST",
            "/api/login",
            json_body={"username": self.username, "password": self.password},
            timeout=30,
        )
        if int(result.get("status") or 0) == 200 and result["body"].get("ok") is True:
            self.refresh_csrf()
        return result

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        timeout: float = 120,
    ) -> dict[str, Any]:
        normalized_method = method.upper()
        headers: dict[str, str] = {"Accept": "application/json"}
        safe_method = normalized_method in {"GET", "HEAD", "OPTIONS"}
        if not safe_method:
            headers["X-CSRF-Token"] = self.csrf
        attempts: list[dict[str, Any]] = []
        maximum_attempts = 2 if safe_method else 1
        for attempt_number in range(1, maximum_attempts + 1):
            try:
                response = self.session.request(
                    normalized_method,
                    f"{self.base_url}{path}",
                    json=dict(json_body) if json_body is not None else None,
                    files=files,
                    data=dict(data) if data is not None else None,
                    headers=headers,
                    timeout=timeout,
                )
            except requests.ConnectionError as exc:
                error = f"{exc.__class__.__name__}: {str(exc)[:500]}"
                no_response = getattr(exc, "response", None) is None
                attempts.append({
                    "attempt": attempt_number,
                    "connection": "initial" if attempt_number == 1 else "fresh",
                    "outcome": "connection_error",
                    "no_response": no_response,
                    "error": error,
                })
                may_retry = (
                    safe_method
                    and no_response
                    and attempt_number < maximum_attempts
                )
                if may_retry:
                    try:
                        self._fresh_session_preserving_cookies()
                    except Exception as replacement_exc:
                        replacement_error = (
                            f"{replacement_exc.__class__.__name__}: "
                            f"{str(replacement_exc)[:500]}"
                        )
                        attempts.append({
                            "attempt": attempt_number + 1,
                            "connection": "fresh",
                            "outcome": "session_replacement_error",
                            "error": replacement_error,
                        })
                        evidence = {
                            "method": normalized_method,
                            "path": path,
                            "attempt_count": len(attempts),
                            "retry_allowed": safe_method,
                            "retry_performed": True,
                            "terminal": "session_replacement_error",
                            "terminal_error": replacement_error,
                            "attempts": attempts,
                        }
                        raise ApiRequestConnectionError(evidence) from replacement_exc
                    continue
                evidence = {
                    "method": normalized_method,
                    "path": path,
                    "attempt_count": len(attempts),
                    "retry_allowed": safe_method and no_response,
                    "retry_performed": len(attempts) > 1,
                    "terminal": "connection_error",
                    "terminal_error": error,
                    "attempts": attempts,
                }
                raise ApiRequestConnectionError(evidence) from exc

            attempts.append({
                "attempt": attempt_number,
                "connection": "initial" if attempt_number == 1 else "fresh",
                "outcome": "response",
                "status": int(response.status_code),
            })
            self.sync_csrf_cookie()
            result = _record(response)
            result["request_evidence"] = {
                "method": normalized_method,
                "path": path,
                "attempt_count": len(attempts),
                "retry_allowed": safe_method,
                "retry_performed": len(attempts) > 1,
                "terminal": "response",
                "attempts": attempts,
            }
            return result
        raise AssertionError("unreachable API request loop")


def _must(
    record: Mapping[str, Any],
    label: str,
    statuses: Iterable[int] = (200,),
) -> Mapping[str, Any]:
    body = record.get("body") if isinstance(record.get("body"), Mapping) else {}
    if int(record.get("status") or 0) not in {int(item) for item in statuses} or body.get("ok") is not True:
        raise RuntimeError(f"{label}_failed:{json.dumps(record, ensure_ascii=False)[:1000]}")
    return body


def _tool(client: Api, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    response = client.request(
        "POST",
        "/api/ai-agent/write-tools/execute",
        json_body={
            "tool": name,
            "arguments": dict(arguments or {}),
            "confirm": "EXECUTE",
        },
        timeout=300,
    )
    body = _must(response, f"ai_tool_{name}")
    result = body.get("result") if isinstance(body.get("result"), Mapping) else {}
    if body.get("tool") != name or int(body.get("status") or 0) not in range(200, 400):
        raise RuntimeError(f"ai_tool_envelope_invalid:{name}:{body}")
    if result.get("ok") is False:
        raise RuntimeError(f"ai_tool_result_failed:{name}:{result}")
    client.successful_tools.append(name)
    return dict(body)


def _tool_summary(envelope: Mapping[str, Any]) -> dict[str, Any]:
    policy = envelope.get("action_policy") if isinstance(envelope.get("action_policy"), Mapping) else {}
    return {
        "ok": envelope.get("ok") is True,
        "tool": str(envelope.get("tool") or ""),
        "status": int(envelope.get("status") or 0),
        "risk_level": str(policy.get("risk_level") or ""),
        "actor_role": str(policy.get("actor_role") or ""),
        "operation_mode": str(policy.get("operation_mode") or ""),
    }


def _tool_result(envelope: Mapping[str, Any]) -> dict[str, Any]:
    value = envelope.get("result")
    return dict(value) if isinstance(value, Mapping) else {}


def _profile_id(client: Api) -> int:
    body = _must(client.request("GET", "/api/users/me/profile"), f"profile_{client.username}")
    profile = body.get("profile") if isinstance(body.get("profile"), Mapping) else {}
    user_id = int(profile.get("id") or profile.get("user_id") or 0)
    if user_id <= 0:
        raise RuntimeError(f"profile_id_missing:{client.username}")
    return user_id


def _mode(payload: Mapping[str, Any]) -> str:
    value = payload.get("mode")
    if isinstance(value, Mapping):
        return str(value.get("current_mode") or value.get("mode") or value.get("name") or "")
    return str(value or payload.get("current_mode") or "")


def _points_reward_state(client: Api) -> dict[str, Any]:
    """Capture the member wallet and recent immutable reward ledger."""

    wallet_body = _must(client.request("GET", "/api/points/wallet"), f"points_wallet_{client.username}")
    wallet = wallet_body.get("wallet") if isinstance(wallet_body.get("wallet"), Mapping) else {}
    ledger_body = _must(
        client.request("GET", "/api/points/ledger?limit=200"),
        f"points_ledger_{client.username}",
    )
    ledger = [dict(row) for row in (ledger_body.get("ledger") or []) if isinstance(row, Mapping)]
    return {
        "points_balance": int(wallet.get("points_balance") or 0),
        "max_ledger_id": max((int(row.get("id") or 0) for row in ledger), default=0),
        "ledger": ledger,
    }


def _exact_reward_row(
    state: Mapping[str, Any],
    *,
    after_id: int,
    action_type: str,
    reference_type: str,
    reference_id: int,
) -> dict[str, Any]:
    matches = [
        dict(row)
        for row in (state.get("ledger") or [])
        if isinstance(row, Mapping)
        and int(row.get("id") or 0) > int(after_id)
        and str(row.get("action_type") or "") == action_type
        and str(row.get("reference_type") or "") == reference_type
        and str(row.get("reference_id") or "") == str(reference_id)
        and str(row.get("direction") or "") == "credit"
        and str(row.get("status") or "") == "confirmed"
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"community_reward_ledger_identity_invalid:{action_type}:"
            f"{reference_type}:{reference_id}:count={len(matches)}"
        )
    return matches[0]


def _verify_community_soft_delete(
    root: Api,
    member: Api,
    thread_id: int,
) -> dict[str, Any]:
    """Prove both privileged audit visibility and ordinary-user absence.

    Community deletion is intentionally soft.  A board moderator/root can
    reopen the retained audit row while a normal member must receive a 404.
    Requiring root to receive 404 would reject the product's correct retention
    semantics and would fail to prove that the row was actually marked
    deleted.
    """

    expected_id = int(thread_id)
    if expected_id <= 0:
        raise RuntimeError("community_soft_delete_thread_id_invalid")
    path = f"/api/community/threads/{expected_id}"
    root_record = root.request("GET", path)
    root_body = _must(root_record, "community_thread_root_audit_view")
    thread = root_body.get("thread") if isinstance(root_body.get("thread"), Mapping) else {}
    if int(thread.get("id") or 0) != expected_id or thread.get("is_deleted") is not True:
        raise RuntimeError(
            "community_thread_root_audit_not_deleted:"
            + json.dumps(
                {
                    "expected_thread_id": expected_id,
                    "actual_thread_id": int(thread.get("id") or 0),
                    "is_deleted": thread.get("is_deleted"),
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    member_record = member.request("GET", path)
    if int(member_record.get("status") or 0) != 404:
        raise RuntimeError(
            "community_thread_member_not_absent:"
            + json.dumps(member_record, ensure_ascii=True, sort_keys=True, default=str)[:1000]
        )
    return {
        "thread_id": expected_id,
        "root_audit_status": int(root_record.get("status") or 0),
        "root_audit_is_deleted": True,
        "root_audit_deleted_at": str(thread.get("deleted_at") or ""),
        "root_audit_deleted_by": str(thread.get("deleted_by") or ""),
        "member_username": str(member.username),
        "member_absent_status": 404,
    }


def _cleanup_orchestration_album(
    browser_api: Any,
    *,
    album_title: str,
    album_id: str = "",
) -> dict[str, Any]:
    """Delete only this run's uniquely named orchestration album.

    A write request can commit before Playwright receives or parses its
    response.  Consequently ``album_id`` is only a hint, not a prerequisite
    for cleanup.  Reopen the account's album inventory, bind an existing ID to
    the per-run unique title, refuse ambiguous/mismatched identities, and
    require a terminal 404 after deletion.
    """

    expected_title = str(album_title or "")
    expected_id = str(album_id or "")
    if not expected_title:
        raise RuntimeError("orchestration_album_cleanup_title_missing")

    listing = browser_api("GET", "/api/storage/albums")
    listing_body = listing.get("body") if isinstance(listing.get("body"), Mapping) else {}
    if int(listing.get("status") or 0) != 200 or listing_body.get("ok") is not True:
        raise RuntimeError(f"orchestration_album_cleanup_list_failed:{listing}")
    if not isinstance(listing_body.get("albums"), list):
        raise RuntimeError(f"orchestration_album_cleanup_inventory_invalid:{listing}")
    rows = [dict(row) for row in listing_body["albums"] if isinstance(row, Mapping)]
    title_matches = [row for row in rows if str(row.get("title") or "") == expected_title]
    if len(title_matches) > 1:
        raise RuntimeError(
            f"orchestration_album_cleanup_title_ambiguous:{expected_title}:count={len(title_matches)}"
        )

    if expected_id:
        id_matches = [row for row in rows if str(row.get("id") or "") == expected_id]
        if len(id_matches) > 1:
            raise RuntimeError(f"orchestration_album_cleanup_id_ambiguous:{expected_id}")
        if id_matches and str(id_matches[0].get("title") or "") != expected_title:
            raise RuntimeError(
                f"orchestration_album_cleanup_identity_mismatch:{expected_id}:"
                f"{id_matches[0].get('title')}"
            )
        if title_matches and str(title_matches[0].get("id") or "") != expected_id:
            raise RuntimeError(
                f"orchestration_album_cleanup_identity_mismatch:{expected_id}:"
                f"title_id={title_matches[0].get('id')}"
            )
        target_id = expected_id
    else:
        target_id = str(title_matches[0].get("id") or "") if title_matches else ""
        if title_matches and not target_id:
            raise RuntimeError("orchestration_album_cleanup_identity_missing")

    if not target_id:
        return {
            "album_id": "",
            "album_title": expected_title,
            "inventory_match_count": 0,
            "delete_status": None,
            "album_absent_status": None,
            "album_absent": True,
        }

    # A captured ID absent from the inventory may already have been deleted;
    # prove that state before deciding no delete is necessary.
    if expected_id and not title_matches:
        absent = browser_api("GET", f"/api/storage/albums/{target_id}")
        if int(absent.get("status") or 0) == 404:
            return {
                "album_id": target_id,
                "album_title": expected_title,
                "inventory_match_count": 0,
                "delete_status": None,
                "album_absent_status": 404,
                "album_absent": True,
            }
        raise RuntimeError(f"orchestration_album_cleanup_inventory_mismatch:{target_id}:{absent}")

    deleted = browser_api("DELETE", f"/api/storage/albums/{target_id}")
    deleted_body = deleted.get("body") if isinstance(deleted.get("body"), Mapping) else {}
    if (
        int(deleted.get("status") or 0) not in {200, 404}
        or (int(deleted.get("status") or 0) == 200 and deleted_body.get("ok") is not True)
    ):
        raise RuntimeError(f"orchestration_album_cleanup_delete_failed:{target_id}:{deleted}")
    missing = browser_api("GET", f"/api/storage/albums/{target_id}")
    if int(missing.get("status") or 0) != 404:
        raise RuntimeError(f"orchestration_album_cleanup_not_terminal:{target_id}:{missing}")
    return {
        "album_id": target_id,
        "album_title": expected_title,
        "inventory_match_count": 1,
        "delete_status": int(deleted.get("status") or 0),
        "album_absent_status": 404,
        "album_absent": True,
    }


def _run_real_agent_orchestration(
    *,
    base_url: str,
    password: str,
    suffix: str,
    artifact_dir: Path,
    provider_config: Mapping[str, str],
) -> dict[str, Any]:
    """Exercise the real browser planner, executor, and readonly assistant.

    Direct write-tool calls prove the gateway, but not that a real provider can
    understand a natural-language request and drive the front-end orchestration
    path.  This check performs one reversible album write plus one operations
    readonly request through the shipped JavaScript functions.  No route is
    mocked and deterministic/fallback-only plans are rejected.
    """

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - formal hosts must provide it
        raise RuntimeError(f"playwright_unavailable:{exc}") from exc

    base = base_url.rstrip("/")
    album_title = f"Formal Agent Orchestration {suffix}"
    write_text = (
        f"請立即建立私人相簿「{album_title}」，描述為正式 AI Agent 可逆驗證，"
        "我明確確認要執行這次站內寫入。"
    )
    readonly_text = "請實際查詢目前伺服器模式、安全狀態及上線前阻擋，並用站內唯讀資料協助營運判斷。"
    browser = None
    page = None
    playwright_context = None
    album_id = ""
    album_write_attempted = False
    cleanup_error = ""
    console_errors: list[str] = []
    page_errors: list[str] = []
    screenshot_path = artifact_dir / f"real_agent_orchestration_{suffix}.png"

    def browser_api(method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if page is None:
            raise RuntimeError("orchestration_page_missing")
        return page.evaluate(
            """async ({method, path, body}) => {
              const cookie = document.cookie || "";
              const csrf = decodeURIComponent((cookie.match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || "");
              const headers = {"Accept": "application/json"};
              if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers['X-CSRF-Token'] = csrf;
              const options = {method, credentials: 'same-origin', headers};
              if (body !== null) {
                headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(body);
              }
              const response = await fetch(path, options);
              const text = await response.text();
              let json = {};
              try { json = text ? JSON.parse(text) : {}; } catch (error) { json = {raw: text}; }
              return {status: response.status, ok: response.ok, body: json};
            }""",
            {"method": method.upper(), "path": path, "body": dict(body) if body is not None else None},
        )

    try:
        playwright_context = sync_playwright()
        playwright = playwright_context.start()
        if True:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(f"{base}/", wait_until="domcontentloaded", timeout=60_000)
            login_result = page.evaluate(
                """async ({password}) => {
                  let response = await fetch('/api/csrf-token', {credentials: 'same-origin'});
                  let body = await response.json();
                  const token = body.csrf_token || decodeURIComponent(
                    ((document.cookie || '').match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || ''
                  );
                  response = await fetch('/api/login', {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token},
                    body: JSON.stringify({username: 'root', password}),
                  });
                  body = await response.json().catch(() => ({}));
                  return {status: response.status, body};
                }""",
                {"password": password},
            )
            if int(login_result.get("status") or 0) != 200 or (login_result.get("body") or {}).get("ok") is not True:
                raise RuntimeError(f"orchestration_login_failed:{login_result}")
            page.goto(f"{base}/", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_function(
                """() => typeof aiAgentPlanToolAction === 'function'
                  && typeof aiAgentExecuteToolPlan === 'function'
                  && typeof loadAiAgentStatus === 'function'
                  && typeof loadAiAgentWriteToolCatalog === 'function'""",
                timeout=60_000,
            )
            page.evaluate(
                """() => {
                  window.__formalAgentNetwork = [];
                  const original = window.fetch.bind(window);
                  window.fetch = async (...args) => {
                    const input = args[0];
                    const options = args[1] || {};
                    const url = typeof input === 'string' ? input : String(input?.url || '');
                    const response = await original(...args);
                    if (url.includes('/api/ai-agent/chat')
                        || url.includes('/api/ai-agent/write-tools/execute')
                        || url.includes('/api/ai-agent/readonly')) {
                      let responseText = '';
                      try { responseText = await response.clone().text(); } catch (error) {}
                      window.__formalAgentNetwork.push({
                        url,
                        method: String(options.method || input?.method || 'GET').toUpperCase(),
                        status: response.status,
                        request_body: url.includes('/write-tools/execute') ? String(options.body || '').slice(0, 4000) : '',
                        response_body: responseText.slice(0, 12000),
                      });
                    }
                    return response;
                  };
                }"""
            )
            preflight = page.evaluate(
                """async () => {
                  const accountDeadline = Date.now() + 60000;
                  while (aiAgentCurrentAccountScope() === 'anonymous' && Date.now() < accountDeadline) {
                    await new Promise((resolve) => setTimeout(resolve, 100));
                  }
                  const accountScope = aiAgentCurrentAccountScope();
                  if (accountScope === 'anonymous') throw new Error('ai_agent_authenticated_account_not_settled');
                  await loadAiAgentStatus({force: true});
                  if (AI_AGENT_STATE.accountScope !== accountScope) {
                    throw new Error('ai_agent_authenticated_account_scope_not_reconciled');
                  }
                  await loadAiAgentWriteToolCatalog({force: true, silent: false});
                  const statusResponse = await fetch('/api/ai-agent/status', {
                    credentials: 'same-origin', headers: {'Accept': 'application/json'}
                  });
                  const statusBody = await statusResponse.json().catch(() => ({}));
                  return {
                    available: AI_AGENT_STATE.available === true,
                    actor: AI_AGENT_STATE.actor || {},
                    settings: AI_AGENT_STATE.settings || {},
                    model_ids: (AI_AGENT_STATE.modelIds || []).slice(),
                    status_http: statusResponse.status,
                    status_ok: statusBody.ok === true,
                    health: statusBody.health || {},
                    tool_names: (AI_AGENT_STATE.writeToolCatalog || []).map((item) => item.name),
                    can_create_album: aiAgentCanRunWriteTool('write_album_create'),
                  };
                }"""
            )
            expected_url = str(provider_config.get("ai_agent_api_base_url") or "").rstrip("/")
            expected_model = str(provider_config.get("ai_agent_model") or "")
            public_settings = preflight.get("settings") if isinstance(preflight.get("settings"), Mapping) else {}
            if (
                preflight.get("available") is not True
                or int(preflight.get("status_http") or 0) != 200
                or preflight.get("status_ok") is not True
                or not isinstance(preflight.get("health"), Mapping)
                or preflight["health"].get("ok") is not True
                or (preflight.get("actor") or {}).get("role") != "super_admin"
                or public_settings.get("operation_mode") != "write"
                or public_settings.get("provider") != "openai_compatible"
                or str(public_settings.get("api_base_url") or "").rstrip("/") != expected_url
                or str(public_settings.get("model") or "") != expected_model
                or expected_model not in set(preflight.get("model_ids") or [])
                or "write_album_create" not in (preflight.get("tool_names") or [])
                or preflight.get("can_create_album") is not True
            ):
                raise RuntimeError(f"orchestration_preflight_invalid:{preflight}")

            album_write_attempted = True
            write_plan = page.evaluate(
                """async ({text}) => {
                  AI_AGENT_STATE.messages = [];
                  renderAiAgentThread();
                  const plan = await aiAgentPlanToolAction(text, {mode: 'text', hasImage: false});
                  const handled = await aiAgentExecuteToolPlan(
                    plan, text, null, {operation: aiAgentOperationContext(), mode: 'text', hasImage: false}
                  );
                  return {plan, handled, messages: AI_AGENT_STATE.messages.slice(-8)};
                }""",
                {"text": write_text},
            )
            plan = write_plan.get("plan") if isinstance(write_plan.get("plan"), Mapping) else {}
            if (
                plan.get("action") != "write_tool"
                or plan.get("tool") != "write_album_create"
                or plan.get("execute_write") is not True
                or write_plan.get("handled") is not True
                or str((plan.get("args") or {}).get("title") or "") != album_title
                or str(plan.get("planner_strategy") or "") in {"local_fast_path", "deterministic_fallback"}
                or str(plan.get("fallback_error") or "")
            ):
                raise RuntimeError(f"real_provider_write_plan_invalid:{write_plan}")

            albums = browser_api("GET", "/api/storage/albums")
            if int(albums.get("status") or 0) != 200 or (albums.get("body") or {}).get("ok") is not True:
                raise RuntimeError(f"orchestration_album_list_failed:{albums}")
            album = next(
                (
                    dict(row)
                    for row in ((albums.get("body") or {}).get("albums") or [])
                    if isinstance(row, Mapping) and str(row.get("title") or "") == album_title
                ),
                {},
            )
            album_id = str(album.get("id") or "")
            if not album_id or str(album.get("visibility") or "") != "private":
                raise RuntimeError(f"orchestration_album_side_effect_missing:{album}")

            readonly_plan = page.evaluate(
                """async ({text}) => {
                  const before = AI_AGENT_STATE.messages.length;
                  const plan = await aiAgentPlanToolAction(text, {mode: 'text', hasImage: false});
                  const handled = await aiAgentExecuteToolPlan(
                    plan, text, null, {operation: aiAgentOperationContext(), mode: 'text', hasImage: false}
                  );
                  return {plan, handled, messages: AI_AGENT_STATE.messages.slice(before)};
                }""",
                {"text": readonly_text},
            )
            readonly = readonly_plan.get("plan") if isinstance(readonly_plan.get("plan"), Mapping) else {}
            if (
                readonly.get("action") != "readonly"
                or str(readonly.get("readonly_scope") or "") not in {"server_mode", "resources", "all"}
                or readonly_plan.get("handled") is not True
                or str(readonly.get("planner_strategy") or "") in {"local_fast_path", "deterministic_fallback"}
                or str(readonly.get("fallback_error") or "")
            ):
                raise RuntimeError(f"real_provider_readonly_plan_invalid:{readonly_plan}")

            network = page.evaluate("() => window.__formalAgentNetwork || []")
            chat_events = [row for row in network if "/api/ai-agent/chat" in str(row.get("url") or "")]
            write_events = [row for row in network if "/api/ai-agent/write-tools/execute" in str(row.get("url") or "")]
            readonly_events = [row for row in network if "/api/ai-agent/readonly" in str(row.get("url") or "")]
            chat_models: list[str] = []
            for event in chat_events:
                try:
                    payload = json.loads(str(event.get("response_body") or "{}"))
                except Exception:
                    payload = {}
                if int(event.get("status") or 0) != 200 or payload.get("ok") is not True:
                    raise RuntimeError(f"real_provider_chat_event_failed:{event}")
                model = _require_expected_provider_model(payload.get("model"), expected_model)
                content = str((payload.get("message") or {}).get("content") or "")
                if not content:
                    raise RuntimeError(f"real_provider_chat_identity_missing:{event}")
                chat_models.append(model)
            if len(chat_events) < 2 or not write_events or not readonly_events:
                raise RuntimeError(
                    f"orchestration_network_coverage_missing:chat={len(chat_events)},"
                    f"write={len(write_events)},readonly={len(readonly_events)}"
                )
            try:
                write_request = json.loads(str(write_events[-1].get("request_body") or "{}"))
                write_response = json.loads(str(write_events[-1].get("response_body") or "{}"))
            except Exception as exc:
                raise RuntimeError(f"orchestration_write_envelope_unparseable:{exc}") from exc
            if (
                int(write_events[-1].get("status") or 0) != 200
                or write_request.get("tool") != "write_album_create"
                or write_request.get("confirm") != "EXECUTE"
                or write_response.get("ok") is not True
            ):
                raise RuntimeError(f"orchestration_write_envelope_invalid:{write_request}:{write_response}")
            if page_errors or console_errors:
                raise RuntimeError(
                    f"orchestration_browser_errors:page={page_errors[:3]}:console={console_errors[:3]}"
                )

            cleanup_receipt = _cleanup_orchestration_album(
                browser_api,
                album_title=album_title,
                album_id=album_id,
            )
            cleaned_album_id = str(cleanup_receipt.get("album_id") or album_id)
            album_id = ""
            album_write_attempted = False
            return {
                "real_provider": True,
                "provider_models": sorted(set(chat_models)),
                "provider_contract": {
                    "provider": public_settings.get("provider"),
                    "api_base_url": str(public_settings.get("api_base_url") or "").rstrip("/"),
                    "requested_model": expected_model,
                    "model_listed": expected_model in set(preflight.get("model_ids") or []),
                    "health_ok": True,
                },
                "chat_call_count": len(chat_events),
                "write_plan": dict(plan),
                "write_handled": write_plan.get("handled") is True,
                "write_request": {
                    "tool": write_request.get("tool"),
                    "confirm": write_request.get("confirm"),
                    "arguments": write_request.get("arguments") or {},
                },
                "write_terminal": {
                    "status": write_events[-1].get("status"),
                    "ok": write_response.get("ok") is True,
                    "album_id": cleaned_album_id,
                    "title": album.get("title"),
                    "visibility": album.get("visibility"),
                },
                "readonly_plan": dict(readonly),
                "readonly_handled": readonly_plan.get("handled") is True,
                "readonly_terminal": {
                    "status": readonly_events[-1].get("status"),
                    "ok": int(readonly_events[-1].get("status") or 0) == 200,
                },
                "cleanup": cleanup_receipt,
                "browser": {"page_errors": [], "console_errors": []},
            }
    except Exception:
        if page is not None:
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception:
                pass
        raise
    finally:
        if album_write_attempted and page is not None:
            try:
                _cleanup_orchestration_album(
                    browser_api,
                    album_title=album_title,
                    album_id=album_id,
                )
                album_id = ""
                album_write_attempted = False
            except Exception as exc:
                cleanup_error = f"{exc.__class__.__name__}:{exc}"
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright_context is not None:
            try:
                playwright_context.stop()
            except Exception:
                pass
        if cleanup_error:
            raise RuntimeError(f"orchestration_failure_cleanup_failed:{cleanup_error}")


def _incident_row_is_open(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    status = str(value.get("status") or "").strip().lower()
    return bool(value.get("active") is True or status in {"open", "active", "resolving"})


def _validate_browser_incident_recovery(
    receipt: Mapping[str, Any],
    *,
    expected_incident_id: str,
    expected_reason: str,
    expected_mode: str,
    allow_already_resolved: bool = False,
) -> dict[str, Any]:
    """Validate a browser-only root recovery without trusting HTTP 200 alone."""

    issues: list[str] = []
    browser = receipt.get("browser") if isinstance(receipt.get("browser"), Mapping) else {}
    login = receipt.get("login") if isinstance(receipt.get("login"), Mapping) else {}
    login_body = login.get("body") if isinstance(login.get("body"), Mapping) else {}
    me = receipt.get("me") if isinstance(receipt.get("me"), Mapping) else {}
    me_body = me.get("body") if isinstance(me.get("body"), Mapping) else {}
    post_login = (
        receipt.get("post_resolve_login")
        if isinstance(receipt.get("post_resolve_login"), Mapping)
        else {}
    )
    post_login_body = post_login.get("body") if isinstance(post_login.get("body"), Mapping) else {}
    post_me = (
        receipt.get("post_resolve_me")
        if isinstance(receipt.get("post_resolve_me"), Mapping)
        else {}
    )
    post_me_body = post_me.get("body") if isinstance(post_me.get("body"), Mapping) else {}
    before = receipt.get("incident_before") if isinstance(receipt.get("incident_before"), Mapping) else {}
    before_body = before.get("body") if isinstance(before.get("body"), Mapping) else {}
    before_incident = (
        before_body.get("incident")
        if isinstance(before_body.get("incident"), Mapping)
        else {}
    )
    resolve = receipt.get("resolve") if isinstance(receipt.get("resolve"), Mapping) else {}
    resolve_body = resolve.get("body") if isinstance(resolve.get("body"), Mapping) else {}
    resolve_result = (
        resolve_body.get("result")
        if isinstance(resolve_body.get("result"), Mapping)
        else {}
    )
    resolve_policy = (
        resolve_body.get("action_policy")
        if isinstance(resolve_body.get("action_policy"), Mapping)
        else {}
    )
    after = receipt.get("incident_after") if isinstance(receipt.get("incident_after"), Mapping) else {}
    after_body = after.get("body") if isinstance(after.get("body"), Mapping) else {}
    after_incident = (
        after_body.get("incident")
        if isinstance(after_body.get("incident"), Mapping)
        else {}
    )
    mode = receipt.get("mode_after") if isinstance(receipt.get("mode_after"), Mapping) else {}
    mode_body = mode.get("body") if isinstance(mode.get("body"), Mapping) else {}
    request_evidence = [
        dict(row)
        for row in (receipt.get("browser_requests") or [])
        if isinstance(row, Mapping)
    ]

    user_agent = str(browser.get("user_agent") or "")
    if (
        browser.get("engine") != "chromium"
        or browser.get("webdriver") is not True
        or "Mozilla/" not in user_agent
        or "Chrome/" not in user_agent
    ):
        issues.append("browser_identity")
    if int(login.get("status") or 0) != 200 or login_body.get("ok") is not True:
        issues.append("root_login")
    if (
        int(me.get("status") or 0) != 200
        or me_body.get("ok") is not True
        or str(me_body.get("username") or "") != "root"
        or str(me_body.get("role") or "") != "super_admin"
    ):
        issues.append("root_session_terminal")
    if (
        int(post_login.get("status") or 0) != 200
        or post_login_body.get("ok") is not True
    ):
        issues.append("post_resolve_root_login")
    if (
        int(post_me.get("status") or 0) != 200
        or post_me_body.get("ok") is not True
        or str(post_me_body.get("username") or "") != "root"
        or str(post_me_body.get("role") or "") != "super_admin"
    ):
        issues.append("post_resolve_root_session_terminal")
    if (
        int(receipt.get("csrf_after_login_status") or 0) != 200
        or int(receipt.get("csrf_after_resolve_login_status") or 0) != 200
    ):
        issues.append("csrf_rotation")
    if any(row.get("maintenance_bypass_header_present") is not False for row in request_evidence):
        issues.append("maintenance_bypass_header")
    required_paths = {
        "/api/csrf-token",
        "/api/login",
        "/api/me",
        "/api/root/incident/status",
    }
    seen_paths = {str(row.get("path") or "") for row in request_evidence}
    if not required_paths.issubset(seen_paths):
        issues.append("browser_request_evidence")
    if int(receipt.get("screenshot_bytes") or 0) <= 0:
        issues.append("browser_screenshot")

    already_resolved = receipt.get("already_resolved") is True
    if already_resolved:
        if not allow_already_resolved:
            issues.append("incident_missing_before_resolve")
    else:
        incident_id = str(before_incident.get("id") or "")
        incident_reason = str(before_incident.get("reason") or "")
        if not incident_id or (expected_incident_id and incident_id != expected_incident_id):
            issues.append("incident_identity")
        if expected_reason and incident_reason != expected_reason:
            issues.append("incident_reason")
        if str(before_incident.get("status") or "") not in {"open", "active"}:
            issues.append("incident_not_open")
        gateway_requests = [
            row
            for row in request_evidence
            if str(row.get("path") or "") == "/api/ai-agent/write-tools/execute"
        ]
        if len(gateway_requests) != 1 or str(gateway_requests[0].get("method") or "") != "POST":
            issues.append("ai_gateway_request_evidence")
        if (
            int(resolve.get("status") or 0) != 200
            or resolve_body.get("ok") is not True
            or str(resolve_body.get("tool") or "") != "write_incident_resolve"
            or int(resolve_body.get("status") or 0) != 200
            or str(resolve_policy.get("actor_role") or "") != "super_admin"
            or str(resolve_policy.get("operation_mode") or "") != "write"
            or resolve_result.get("ok") is not True
            or (
                expected_incident_id
                and str(resolve_result.get("incident_id") or "") != expected_incident_id
            )
        ):
            issues.append("ai_incident_resolve")

    if int(after.get("status") or 0) != 200 or after_body.get("ok") is not True:
        issues.append("incident_terminal_readback")
    if _incident_row_is_open(after_incident):
        issues.append("incident_still_active")
    if int(mode.get("status") or 0) != 200 or mode_body.get("ok") is not True:
        issues.append("mode_terminal_readback")
    if expected_mode and _mode(mode_body) != expected_mode:
        issues.append("mode_not_restored")
    if issues:
        # Preserve a bounded, non-secret failure envelope.  A generic issue
        # list hid the inner AI-gateway error during the first real restart
        # recovery attempt, forcing operators to reconstruct it from audit
        # logs.  These public response fields are sufficient to distinguish
        # a gateway-policy rejection from an incident-state or session fault.
        diagnostics = {
            "login_status": int(login.get("status") or 0),
            "me_status": int(me.get("status") or 0),
            "resolve_http_status": int(resolve.get("status") or 0),
            "resolve": {
                key: resolve_body.get(key)
                for key in ("ok", "tool", "status", "msg", "blocked_by")
                if key in resolve_body
            },
            "resolve_result": {
                key: resolve_result.get(key)
                for key in ("ok", "incident_id", "msg", "error", "resolution_in_progress")
                if key in resolve_result
            },
            "post_login_status": int(post_login.get("status") or 0),
            "post_me_status": int(post_me.get("status") or 0),
            "incident_after_status": int(after.get("status") or 0),
            "incident_after_open": _incident_row_is_open(after_incident),
            "mode_after_status": int(mode.get("status") or 0),
            "mode_after": _mode(mode_body),
        }
        raise RuntimeError(
            f"browser_incident_recovery_invalid:{','.join(issues)}:"
            + json.dumps(diagnostics, ensure_ascii=True, sort_keys=True, default=str)[:2000]
        )
    return dict(receipt)


def _run_browser_incident_recovery_in_process(
    *,
    base_url: str,
    password: str,
    expected_incident_id: str,
    expected_reason: str,
    expected_mode: str,
    notes: str,
    verification: Mapping[str, Any],
    artifact_dir: Path,
    suffix: str,
    allow_already_resolved: bool = False,
) -> dict[str, Any]:
    """Run the Chromium recovery inside a dedicated one-shot process.

    Incident mode intentionally enables browser-only access and rotates the
    security epoch.  A requests client, a forged browser User-Agent, or a
    maintenance bypass token would all invalidate this recovery proof.

    The public parent helper below is the only production caller.  Keeping the
    sync Playwright implementation here lets the child process start with a
    pristine asyncio/greenlet state even when the parent already completed a
    separate sync Playwright orchestration run.
    """

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - formal hosts must provide it
        raise RuntimeError(f"playwright_unavailable:{exc}") from exc

    base = base_url.rstrip("/")
    screenshot_path = artifact_dir / f"incident_browser_recovery_{suffix}.png"
    browser_requests: list[dict[str, Any]] = []
    browser = None
    context = None
    page = None
    playwright_context = None

    def capture_request(request: Any) -> None:
        path = urlsplit(str(request.url or "")).path
        if not path.startswith("/api/"):
            return
        headers = {
            str(key).strip().lower(): str(value)
            for key, value in dict(request.headers or {}).items()
        }
        browser_requests.append({
            "method": str(request.method or "").upper(),
            "path": path,
            "maintenance_bypass_header_present": "x-maintenance-bypass-token" in headers,
        })

    def browser_fetch(method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if page is None:
            raise RuntimeError("incident_browser_page_missing")
        return page.evaluate(
            """async ({method, path, body}) => {
              const cookie = document.cookie || '';
              const csrf = decodeURIComponent((cookie.match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || '');
              const headers = {'Accept': 'application/json'};
              const options = {method, credentials: 'same-origin', headers};
              if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers['X-CSRF-Token'] = csrf;
              if (body !== null) {
                headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(body);
              }
              const response = await fetch(path, options);
              const text = await response.text();
              let json = {};
              try { json = text ? JSON.parse(text) : {}; } catch (_) { json = {raw: text}; }
              return {status: response.status, body: json, content_type: response.headers.get('content-type') || ''};
            }""",
            {"method": method.upper(), "path": path, "body": dict(body) if body is not None else None},
        )

    try:
        playwright_context = sync_playwright()
        playwright = playwright_context.start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("request", capture_request)
        page.goto(f"{base}/", wait_until="domcontentloaded", timeout=60_000)
        browser_identity = page.evaluate(
            """() => ({
              engine: 'chromium',
              user_agent: navigator.userAgent || '',
              webdriver: navigator.webdriver === true,
            })"""
        )

        csrf_before = browser_fetch("GET", "/api/csrf-token")
        if int(csrf_before.get("status") or 0) != 200 or not str(
            (csrf_before.get("body") or {}).get("csrf_token") or ""
        ):
            raise RuntimeError(f"incident_browser_csrf_failed:{csrf_before}")
        login = browser_fetch(
            "POST",
            "/api/login",
            {"username": "root", "password": password},
        )
        csrf_after_login = browser_fetch("GET", "/api/csrf-token")
        me = browser_fetch("GET", "/api/me")
        before = browser_fetch("GET", "/api/root/incident/status")
        before_body = before.get("body") if isinstance(before.get("body"), Mapping) else {}
        incident = before_body.get("incident") if isinstance(before_body.get("incident"), Mapping) else {}
        incident_is_open = _incident_row_is_open(incident)
        already_resolved = not incident_is_open
        resolve: dict[str, Any] = {}

        if incident_is_open:
            actual_id = str(incident.get("id") or "")
            actual_reason = str(incident.get("reason") or "")
            if expected_incident_id and actual_id != expected_incident_id:
                raise RuntimeError(f"incident_browser_identity_mismatch:{actual_id}:{expected_incident_id}")
            if expected_reason and actual_reason != expected_reason:
                raise RuntimeError("incident_browser_reason_mismatch")
            page.screenshot(path=str(screenshot_path), full_page=True)
            resolve = browser_fetch(
                "POST",
                "/api/ai-agent/write-tools/execute",
                {
                    "tool": "write_incident_resolve",
                    "arguments": {
                        "confirm": "RESOLVE_INCIDENT",
                        "notes": notes,
                        "verification": dict(verification),
                    },
                    "confirm": "EXECUTE",
                },
            )
        elif not allow_already_resolved:
            raise RuntimeError("incident_browser_open_incident_missing")

        # Resolution restores the previous settings while preserving the
        # newest security epoch.  Always perform another real browser login
        # and /api/me proof before trusting terminal incident/mode readback.
        csrf_before_post_login = browser_fetch("GET", "/api/csrf-token")
        post_resolve_login = browser_fetch(
            "POST",
            "/api/login",
            {"username": "root", "password": password},
        )
        csrf_after_post_login = browser_fetch("GET", "/api/csrf-token")
        post_resolve_me = browser_fetch("GET", "/api/me")
        after = browser_fetch("GET", "/api/root/incident/status")
        mode_after = browser_fetch("GET", "/api/root/server-mode")
        if not screenshot_path.is_file():
            page.screenshot(path=str(screenshot_path), full_page=True)
        receipt = {
            "browser": dict(browser_identity),
            "login": login,
            "csrf_after_login_status": int(csrf_after_login.get("status") or 0),
            "me": me,
            "incident_before": before,
            "resolve": resolve,
            "csrf_before_resolve_login_status": int(csrf_before_post_login.get("status") or 0),
            "post_resolve_login": post_resolve_login,
            "csrf_after_resolve_login_status": int(csrf_after_post_login.get("status") or 0),
            "post_resolve_me": post_resolve_me,
            "incident_after": after,
            "mode_after": mode_after,
            "already_resolved": already_resolved,
            "browser_requests": browser_requests,
            "screenshot_path": str(screenshot_path),
            "screenshot_bytes": screenshot_path.stat().st_size if screenshot_path.is_file() else 0,
        }
        return _validate_browser_incident_recovery(
            receipt,
            expected_incident_id=expected_incident_id,
            expected_reason=expected_reason,
            expected_mode=expected_mode,
            allow_already_resolved=allow_already_resolved,
        )
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright_context is not None:
            try:
                playwright_context.stop()
            except Exception:
                pass


def _redact_child_secret(value: Any, secret: str) -> str:
    text = str(value or "")
    return text.replace(secret, "<redacted>") if secret else text


def _incident_recovery_child_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child environment with every password variable removed.

    The actual credential is sent exactly once through the child's stdin pipe.
    It must never be duplicated into argv, an environment variable, or a
    temporary file that can outlive the recovery process.
    """

    source = environ if environ is not None else os.environ
    return {
        str(key): str(value)
        for key, value in source.items()
        if str(key).upper() != "PASSWORD"
        and not str(key).upper().endswith("_PASSWORD")
    }


def _incident_recovery_child_payload(
    *,
    base_url: str,
    password: str,
    expected_incident_id: str,
    expected_reason: str,
    expected_mode: str,
    notes: str,
    verification: Mapping[str, Any],
    artifact_dir: Path,
    suffix: str,
    allow_already_resolved: bool,
) -> dict[str, Any]:
    return {
        "schema_version": INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION,
        "base_url": str(base_url),
        "password": str(password),
        "expected_incident_id": str(expected_incident_id),
        "expected_reason": str(expected_reason),
        "expected_mode": str(expected_mode),
        "notes": str(notes),
        "verification": dict(verification),
        "artifact_dir": str(Path(artifact_dir).expanduser().resolve()),
        "suffix": str(suffix),
        "allow_already_resolved": bool(allow_already_resolved),
    }


def _validate_incident_recovery_child_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RuntimeError("incident_recovery_child_payload_not_object")
    expected_keys = {
        "schema_version",
        "base_url",
        "password",
        "expected_incident_id",
        "expected_reason",
        "expected_mode",
        "notes",
        "verification",
        "artifact_dir",
        "suffix",
        "allow_already_resolved",
    }
    if set(payload) != expected_keys:
        raise RuntimeError("incident_recovery_child_payload_fields_invalid")
    if payload.get("schema_version") != INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION:
        raise RuntimeError("incident_recovery_child_payload_schema_invalid")
    required_text = (
        "base_url",
        "password",
        "expected_reason",
        "expected_mode",
        "notes",
        "artifact_dir",
        "suffix",
    )
    if any(not isinstance(payload.get(key), str) or not str(payload.get(key)) for key in required_text):
        raise RuntimeError("incident_recovery_child_payload_required_text_invalid")
    if not isinstance(payload.get("expected_incident_id"), str):
        raise RuntimeError("incident_recovery_child_payload_incident_id_invalid")
    if not isinstance(payload.get("verification"), Mapping):
        raise RuntimeError("incident_recovery_child_payload_verification_invalid")
    if not isinstance(payload.get("allow_already_resolved"), bool):
        raise RuntimeError("incident_recovery_child_payload_allow_resolved_invalid")
    return dict(payload)


def _incident_recovery_child_main() -> int:
    """Read one secret-bearing request from stdin and emit one JSON result."""

    raw = sys.stdin.buffer.read(INCIDENT_RECOVERY_CHILD_INPUT_LIMIT_BYTES + 1)
    password = ""
    try:
        if len(raw) > INCIDENT_RECOVERY_CHILD_INPUT_LIMIT_BYTES:
            raise RuntimeError("incident_recovery_child_input_too_large")
        payload = _validate_incident_recovery_child_payload(json.loads(raw.decode("utf-8")))
        password = str(payload.pop("password"))
        payload.pop("schema_version", None)
        payload["artifact_dir"] = Path(str(payload["artifact_dir"])).expanduser().resolve()
        receipt = _run_browser_incident_recovery_in_process(**payload)
        output = {
            "schema_version": INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION,
            "ok": True,
            "receipt": receipt,
        }
        return_code = 0
    except Exception as exc:
        output = {
            "schema_version": INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION,
            "ok": False,
            "error_type": exc.__class__.__name__,
            "error": _redact_child_secret(exc, password)[:4000],
        }
        return_code = 1
    sys.stdout.write(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return return_code


def _run_browser_incident_recovery(
    *,
    base_url: str,
    password: str,
    expected_incident_id: str,
    expected_reason: str,
    expected_mode: str,
    notes: str,
    verification: Mapping[str, Any],
    artifact_dir: Path,
    suffix: str,
    allow_already_resolved: bool = False,
    _popen_factory: Callable[..., Any] = subprocess.Popen,
    _timeout_seconds: float = INCIDENT_RECOVERY_CHILD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Resolve an incident in a pristine child Python + real Chromium.

    Password transport is stdin-only.  The child is its own process group so a
    timeout kills Chromium and every descendant before the result is rejected.
    Only a strict machine-readable receipt can reach the parent validator.
    """

    if not password:
        raise RuntimeError("incident_recovery_password_missing")
    if not math.isfinite(float(_timeout_seconds)) or float(_timeout_seconds) <= 0:
        raise RuntimeError("incident_recovery_timeout_invalid")
    payload = _incident_recovery_child_payload(
        base_url=base_url,
        password=password,
        expected_incident_id=expected_incident_id,
        expected_reason=expected_reason,
        expected_mode=expected_mode,
        notes=notes,
        verification=verification,
        artifact_dir=artifact_dir,
        suffix=suffix,
        allow_already_resolved=allow_already_resolved,
    )
    command = [sys.executable, str(Path(__file__).resolve()), INCIDENT_RECOVERY_CHILD_FLAG]
    process = _popen_factory(
        command,
        cwd=str(ROOT),
        env=_incident_recovery_child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
        close_fds=True,
    )
    encoded_input = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        stdout, stderr = process.communicate(
            input=encoded_input,
            timeout=float(_timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(int(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise RuntimeError(
            f"incident_recovery_child_timeout:{float(_timeout_seconds):g}s"
        ) from exc

    stderr_text = _redact_child_secret(stderr, password)
    try:
        child_result = json.loads(stdout)
    except Exception as exc:
        diagnostics = {
            "returncode": int(process.returncode or 0),
            "stdout_bytes": len((stdout or "").encode("utf-8", errors="replace")),
            "stderr_bytes": len((stderr or "").encode("utf-8", errors="replace")),
            "stderr_sha256": hashlib.sha256((stderr or "").encode("utf-8", errors="replace")).hexdigest(),
        }
        raise RuntimeError(
            "incident_recovery_child_result_not_json:"
            + json.dumps(diagnostics, sort_keys=True, separators=(",", ":"))
        ) from exc
    if not isinstance(child_result, Mapping):
        raise RuntimeError("incident_recovery_child_result_not_object")
    if (
        child_result.get("schema_version") != INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION
        or process.returncode != 0
        or child_result.get("ok") is not True
        or not isinstance(child_result.get("receipt"), Mapping)
    ):
        diagnostics = {
            "returncode": int(process.returncode or 0),
            "schema_version": child_result.get("schema_version"),
            "ok": child_result.get("ok"),
            "error_type": child_result.get("error_type"),
            "error": _redact_child_secret(child_result.get("error"), password)[:2000],
            "stderr_bytes": len((stderr or "").encode("utf-8", errors="replace")),
            "stderr_sample": stderr_text[-1000:],
        }
        raise RuntimeError(
            "incident_recovery_child_failed:"
            + json.dumps(diagnostics, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    receipt = _validate_browser_incident_recovery(
        child_result["receipt"],
        expected_incident_id=expected_incident_id,
        expected_reason=expected_reason,
        expected_mode=expected_mode,
        allow_already_resolved=allow_already_resolved,
    )
    receipt["recovery_subprocess"] = {
        "schema_version": INCIDENT_RECOVERY_CHILD_SCHEMA_VERSION,
        "isolated_python_process": True,
        "start_new_session": True,
        "timeout_seconds": float(_timeout_seconds),
        "returncode": int(process.returncode or 0),
        "stderr_bytes": len((stderr or "").encode("utf-8", errors="replace")),
        "stderr_sha256": hashlib.sha256((stderr or "").encode("utf-8", errors="replace")).hexdigest(),
    }
    return receipt


def _parse_playlist(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _make_video_fixture(directory: Path) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ai_agent_positive_operations.mp4"
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=523:sample_rate=48000:duration=6",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest", str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    if completed.returncode != 0 or not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"video_fixture_failed:{completed.stderr[-800:]}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    payload = json.loads(probe.stdout or "{}") if probe.returncode == 0 else {}
    duration = float((payload.get("format") or {}).get("duration") or 0)
    if duration < 5:
        raise RuntimeError(f"video_fixture_duration_invalid:{duration}")
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": digest,
        "duration_seconds": duration,
        "ffprobe_returncode": probe.returncode,
    }


def _wait_video_playback(client: Api, video_id: int, *, timeout: float = 600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    attempts = 0
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        attempts += 1
        record = client.request("GET", f"/api/videos/{video_id}/playback", timeout=60)
        last = record
        body = record.get("body") if isinstance(record.get("body"), Mapping) else {}
        status = body.get("status") if isinstance(body.get("status"), Mapping) else {}
        if (
            int(record.get("status") or 0) == 200
            and body.get("ok") is True
            and body.get("streaming_ready") is True
            and status.get("status") == "ready"
            and str(body.get("master_url") or "")
        ):
            return {"attempts": attempts, "terminal": record}
        if status.get("status") == "failed":
            break
        time.sleep(1)
    raise RuntimeError(f"video_hls_not_terminal:{attempts}:{json.dumps(last, ensure_ascii=False)[:1200]}")


def _hls_bytes(client: Api, master_path: str) -> dict[str, Any]:
    master_url = urljoin(client.base_url + "/", master_path.lstrip("/"))
    master = client.session.get(master_url, timeout=60)
    master_uris = _parse_playlist(master.text)
    if master.status_code != 200 or not master.text.startswith("#EXTM3U") or not master_uris:
        raise RuntimeError("video_hls_master_invalid")
    variant_url = urljoin(master_url, master_uris[0])
    variant = client.session.get(variant_url, timeout=60)
    variant_uris = _parse_playlist(variant.text)
    segment_uri = next(
        (item for item in variant_uris if item.lower().endswith((".ts", ".m4s", ".mp4", ".aac"))),
        variant_uris[-1] if variant_uris else "",
    )
    if variant.status_code != 200 or not variant.text.startswith("#EXTM3U") or not segment_uri:
        raise RuntimeError("video_hls_variant_invalid")
    segment = client.session.get(urljoin(variant_url, segment_uri), timeout=60)
    if segment.status_code != 200 or not segment.content:
        raise RuntimeError("video_hls_segment_invalid")
    return {
        "master_status": master.status_code,
        "master_extm3u": True,
        "variant_status": variant.status_code,
        "variant_extm3u": True,
        "segment_status": segment.status_code,
        "segment_bytes": len(segment.content),
    }


def _wait_management_job(
    client: Api,
    started: Mapping[str, Any],
    *,
    timeout: float = 300,
) -> dict[str, Any]:
    status_url = str(started.get("status_url") or "")
    job_uuid = str(started.get("job_uuid") or started.get("job_id") or "")
    if not status_url or not job_uuid:
        raise RuntimeError(f"management_job_identity_missing:{started}")
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = _must(client.request("GET", status_url), f"management_job_{job_uuid}")
        job = body.get("job") if isinstance(body.get("job"), Mapping) else {}
        last = dict(job)
        state = str(job.get("status") or "").lower()
        if state in {"succeeded", "failed", "cancelled", "error"}:
            return {
                "job_uuid": job_uuid,
                "terminal_status": state,
                "stage": str(job.get("stage") or ""),
                "progress_percent": job.get("progress_percent"),
                "error_code": str(job.get("error_code") or ""),
                "error_message": str(job.get("error_message") or "")[:500],
            }
        time.sleep(0.5)
    raise RuntimeError(f"management_job_timeout:{job_uuid}:{last}")


def _find_by(rows: Iterable[Any], key: str, value: Any) -> dict[str, Any]:
    for row in rows:
        if isinstance(row, Mapping) and row.get(key) == value:
            return dict(row)
    return {}


def _cloud_drive_file_id(row: Mapping[str, Any]) -> str:
    """Return the canonical file identity from create or list payloads.

    Create envelopes expose ``file_id`` while ``GET /api/cloud-drive/files``
    uses ``id`` for the same underlying file.  Accept only those two official
    schema fields so terminal presence and absence checks cannot silently look
    at the wrong identity.
    """

    return str(row.get("file_id") or row.get("id") or "")


def _funding_state(trading: Mapping[str, Any]) -> dict[str, Any]:
    funding = trading.get("funding") if isinstance(trading.get("funding"), Mapping) else {}
    trial = funding.get("trial_credit") if isinstance(funding.get("trial_credit"), Mapping) else {}
    required_funding = (
        "available_points",
        "locked_points",
        "wallet_available_points",
        "wallet_locked_points",
    )
    required_trial = ("available_points", "locked_points", "deployed_points")
    invalid_funding = [
        f"funding.{key}"
        for key in required_funding
        if key in funding and (
            funding.get(key) is None
            or isinstance(funding.get(key), bool)
            or not isinstance(funding.get(key), (int, float))
        )
    ]
    invalid_trial = [
        f"funding.trial_credit.{key}"
        for key in required_trial
        if key in trial and (
            trial.get(key) is None
            or isinstance(trial.get(key), bool)
            or not isinstance(trial.get(key), (int, float))
        )
    ]
    return {
        "available_points": int(funding.get("available_points") or 0),
        "locked_points": int(funding.get("locked_points") or 0),
        "wallet_available_points": int(funding.get("wallet_available_points") or 0),
        "wallet_locked_points": int(funding.get("wallet_locked_points") or 0),
        "trial_available_points": int(trial.get("available_points") or 0),
        "trial_locked_points": int(trial.get("locked_points") or 0),
        "trial_deployed_points": int(trial.get("deployed_points") or 0),
        "missing_fields": [
            *(f"funding.{key}" for key in required_funding if key not in funding),
            *(f"funding.trial_credit.{key}" for key in required_trial if key not in trial),
        ],
        "invalid_fields": [*invalid_funding, *invalid_trial],
    }


def _funding_pool_state(trading: Mapping[str, Any]) -> dict[str, Any]:
    pool = trading.get("funding_pool") if isinstance(trading.get("funding_pool"), Mapping) else {}
    required_fields = (
        "balance_points",
        "available_points",
        "outstanding_principal_points",
        "capacity_points",
        "exchange_fund_balance_points",
        "exchange_fund_total_assets_points",
        "max_outstanding_principal_points",
        "remaining_borrow_capacity_points",
        "max_pool_utilization_percent",
    )
    invalid_fields = [
        key
        for key in required_fields
        if key in pool and (
            pool.get(key) is None
            or isinstance(pool.get(key), bool)
            or not isinstance(pool.get(key), (int, float))
        )
    ]
    return {
        "balance_points": int(pool.get("balance_points") or 0),
        "available_points": int(pool.get("available_points") or 0),
        "outstanding_principal_points": int(pool.get("outstanding_principal_points") or 0),
        "capacity_points": int(pool.get("capacity_points") or 0),
        "exchange_fund_balance_points": int(pool.get("exchange_fund_balance_points") or 0),
        "exchange_fund_total_assets_points": int(pool.get("exchange_fund_total_assets_points") or 0),
        "max_outstanding_principal_points": int(pool.get("max_outstanding_principal_points") or 0),
        "remaining_borrow_capacity_points": int(pool.get("remaining_borrow_capacity_points") or 0),
        "max_pool_utilization_percent": float(pool.get("max_pool_utilization_percent") or 0),
        "missing_fields": [key for key in required_fields if key not in pool],
        "invalid_fields": invalid_fields,
    }


def _margin_close_terminal_issues(
    *,
    position_uuid: str,
    margin_terminal: Mapping[str, Any],
    funding_before: Mapping[str, Any],
    funding_after: Mapping[str, Any],
    pool_before: Mapping[str, Any],
    pool_after: Mapping[str, Any],
    expected_statuses: Iterable[str] = ("closed",),
) -> list[str]:
    """Return exact terminal/accounting violations for a closed margin loan.

    The exchange fund legitimately earns fees/interest or pays trading profit,
    so its total assets and utilization-derived capacity are not required to
    equal their pre-open values.  The invariant is instead that the loan is
    closed, all collateral locks and outstanding principal return to baseline,
    and the post-close pool fields satisfy their own accounting equations.
    """

    issues: list[str] = []
    if str(margin_terminal.get("position_uuid") or "") != str(position_uuid or ""):
        issues.append("position_identity")
    allowed_statuses = {str(value) for value in expected_statuses}
    if str(margin_terminal.get("status") or "") not in allowed_statuses:
        issues.append("position_status")
    if funding_before.get("missing_fields"):
        issues.append("funding_before_missing_fields")
    if funding_after.get("missing_fields"):
        issues.append("funding_after_missing_fields")
    if funding_before.get("invalid_fields"):
        issues.append("funding_before_invalid_fields")
    if funding_after.get("invalid_fields"):
        issues.append("funding_after_invalid_fields")
    if pool_before.get("missing_fields"):
        issues.append("pool_before_missing_fields")
    if pool_after.get("missing_fields"):
        issues.append("pool_after_missing_fields")
    if pool_before.get("invalid_fields"):
        issues.append("pool_before_invalid_fields")
    if pool_after.get("invalid_fields"):
        issues.append("pool_after_invalid_fields")
    for key in (
        "locked_points",
        "wallet_locked_points",
        "trial_locked_points",
        "trial_deployed_points",
    ):
        if int(funding_after.get(key) or 0) != int(funding_before.get(key) or 0):
            issues.append(f"funding_{key}")
    realized_delta = int(margin_terminal.get("realized_pnl_points") or 0)
    if int(funding_after.get("available_points") or 0) != (
        int(funding_before.get("available_points") or 0) + realized_delta
    ):
        issues.append("funding_available_equation")
    if int(pool_after.get("outstanding_principal_points") or 0) != int(
        pool_before.get("outstanding_principal_points") or 0
    ):
        issues.append("outstanding_principal")

    balance = int(pool_after.get("balance_points") or 0)
    available = int(pool_after.get("available_points") or 0)
    outstanding = int(pool_after.get("outstanding_principal_points") or 0)
    capacity = int(pool_after.get("capacity_points") or 0)
    exchange_balance = int(pool_after.get("exchange_fund_balance_points") or 0)
    exchange_total = int(pool_after.get("exchange_fund_total_assets_points") or 0)
    max_outstanding = int(pool_after.get("max_outstanding_principal_points") or 0)
    remaining = int(pool_after.get("remaining_borrow_capacity_points") or 0)
    utilization_percent = float(pool_after.get("max_pool_utilization_percent") or 0)

    if any(value < 0 for value in (
        balance,
        available,
        outstanding,
        capacity,
        exchange_balance,
        exchange_total,
        max_outstanding,
        remaining,
    )):
        issues.append("pool_negative_value")
    if exchange_total != exchange_balance + outstanding:
        issues.append("pool_total_assets_equation")
    if capacity != max_outstanding:
        issues.append("pool_capacity_alias")
    expected_capacity = int(math.floor(exchange_total * utilization_percent / 100.0))
    if capacity != expected_capacity:
        issues.append("pool_capacity_equation")
    expected_available = min(
        max(0, exchange_balance),
        max(0, capacity - outstanding),
    )
    if not (balance == available == remaining == expected_available):
        issues.append("pool_available_equation")
    return issues


def _margin_cleanup_action(rows: Iterable[Any], position_uuid: str) -> str:
    """Choose a state-aware failure cleanup without double-closing a loan."""

    row = _find_by(rows, "position_uuid", position_uuid)
    if not row:
        raise RuntimeError(f"cleanup_margin_missing:{position_uuid}")
    status = str(row.get("status") or "")
    if status == "open":
        return "close"
    if status in {"closed", "liquidated"}:
        return "verify"
    raise RuntimeError(f"cleanup_margin_unexpected_status:{position_uuid}:{status}")


def _exact_governance_appeal(
    payload: Mapping[str, Any],
    *,
    violation_id: int,
    target_user_id: int,
    username: str,
    appeal_id: int = 0,
    allow_absent: bool = False,
) -> dict[str, Any]:
    """Return one exact member appeal, rejecting projection/identity drift."""

    candidates = [
        dict(row)
        for row in (payload.get("appeals") or [])
        if isinstance(row, Mapping)
        and int(row.get("latest_violation_id") or 0) == int(violation_id or 0)
        and (not appeal_id or int(row.get("id") or 0) == int(appeal_id))
    ]
    if not candidates and allow_absent:
        return {}
    if len(candidates) != 1:
        raise RuntimeError(
            f"governance_appeal_identity_count:{violation_id}:{appeal_id}:{len(candidates)}"
        )
    row = candidates[0]
    if (
        int(row.get("id") or 0) <= 0
        or int(row.get("user_id") or 0) != int(target_user_id or 0)
        or str(row.get("username") or "") != str(username or "")
    ):
        raise RuntimeError(f"governance_appeal_identity_mismatch:{row}")
    return row


def _exact_governance_violation(
    payload: Mapping[str, Any],
    *,
    reason: str,
    target_user_id: int,
    username: str,
    actor_username: str,
    violation_id: int = 0,
    allow_absent: bool = False,
) -> dict[str, Any]:
    """Recover one exact append-only governance warning from member state."""

    rows: list[dict[str, Any]] = []
    latest = payload.get("latest_violation")
    if isinstance(latest, Mapping):
        rows.append(dict(latest))
    rows.extend(
        dict(row)
        for row in (payload.get("violations") or [])
        if isinstance(row, Mapping)
    )
    candidates: dict[int, dict[str, Any]] = {}
    for row in rows:
        row_id = int(row.get("id") or 0)
        if (
            row_id > 0
            and (not violation_id or row_id == int(violation_id))
            and int(row.get("user_id") or 0) == int(target_user_id or 0)
            and str(row.get("username") or "") == str(username or "")
            and int(row.get("points") or 0) == 1
            and str(row.get("reason") or "") == str(reason or "")
            and str(row.get("actor_username") or "") == str(actor_username or "")
        ):
            candidates[row_id] = row
    if not candidates and allow_absent:
        return {}
    if len(candidates) != 1:
        raise RuntimeError(
            f"governance_violation_identity_count:{violation_id}:{len(candidates)}"
        )
    row = next(iter(candidates.values()))
    if str(row.get("triggered_by") or "") != "super_admin":
        raise RuntimeError(f"governance_violation_actor_mismatch:{row}")
    return row


def _catalog(client: Api, *, include_all: bool = False) -> dict[str, Any]:
    suffix = "?include_all=1" if include_all else ""
    body = _must(client.request("GET", f"/api/ai-agent/write-tools{suffix}"), f"catalog_{client.username}")
    tools = [dict(row) for row in (body.get("tools") or []) if isinstance(row, Mapping)]
    names = sorted(str(row.get("name") or "") for row in tools if row.get("name"))
    return {
        "actor_role": str(body.get("actor_role") or ""),
        "operation_mode": str(body.get("operation_mode") or ""),
        "write_enabled": body.get("write_enabled") is True,
        "role_scoped": body.get("role_scoped") is True,
        "catalog_sha256": str(body.get("catalog_sha256") or ""),
        "tool_count": len(names),
        "names": names,
        "catalog_tool_count": len(body.get("catalog_tools") or []),
    }


def _workflow(suffix: str) -> dict[str, Any]:
    return {
        "version": 2,
        "source": f"formal_ai_agent_{suffix}",
        "start_node_id": "start",
        "nodes": [
            {"id": "start", "type": "start", "label": "Start", "x": 0, "y": 100},
            {
                "id": "condition",
                "type": "condition",
                "label": "Always",
                "condition": {"type": "always"},
                "x": 180,
                "y": 100,
            },
            {
                "id": "buy",
                "type": "action",
                "label": "Bounded limit buy",
                "action": {
                    "type": "buy_amount",
                    "amount_points": 2,
                    "order_type": "limit",
                    "limit_price_points": 1,
                    "step": 1,
                },
                "x": 380,
                "y": 100,
            },
        ],
        "edges": [
            {"id": "e1", "from": "start", "from_port": "out", "to": "condition", "to_port": "in"},
            {"id": "e2", "from": "condition", "from_port": "true", "to": "buy", "to_port": "in"},
        ],
    }


def _audit_max_id(runtime_root: Path) -> int:
    audit_db = runtime_root / "database" / "audit.db"
    if not audit_db.is_file() or audit_db.is_symlink():
        raise RuntimeError(f"audit_database_unavailable:{audit_db}")
    connection = sqlite3.connect(f"file:{audit_db}?mode=ro", uri=True, timeout=30)
    try:
        row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM secure_audit").fetchone()
        return int((row or [0])[0] or 0)
    finally:
        connection.close()


def _audit_tool_rows(runtime_root: Path, after_id: int) -> list[dict[str, Any]]:
    audit_db = runtime_root / "database" / "audit.db"
    connection = sqlite3.connect(f"file:{audit_db}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, ts, action, user, success, detail, chain_hash
            FROM secure_audit
            WHERE id>? AND action='AI_AGENT_WRITE_TOOL'
            ORDER BY id ASC
            """,
            (int(after_id),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def main() -> int:
    if sys.argv[1:] == [INCIDENT_RECOVERY_CHILD_FLAG]:
        return _incident_recovery_child_main()

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--root-password", default=os.environ.get("HACKME_PROBE_ROOT_PASSWORD", ""))
    parser.add_argument("--manager-username", default="admin")
    parser.add_argument("--manager-password", default=os.environ.get("HACKME_PROBE_MANAGER_PASSWORD", ""))
    parser.add_argument("--user-one", required=True)
    parser.add_argument("--user-one-password", default=os.environ.get("HACKME_PROBE_USER_ONE_PASSWORD", ""))
    parser.add_argument("--user-two", required=True)
    parser.add_argument("--user-two-password", default=os.environ.get("HACKME_PROBE_USER_TWO_PASSWORD", ""))
    parser.add_argument("--restart-request-file", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if not all((
        args.root_password,
        args.manager_password,
        args.user_one_password,
        args.user_two_password,
    )):
        parser.error("root, manager, and both campaign-user passwords are required")

    runtime_root = Path(args.runtime_root).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir = Path(args.artifact_dir).expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    restart_request_file = Path(args.restart_request_file).expanduser().resolve()
    suffix = uuid.uuid4().hex[:12]
    root = Api(args.base_url, "root", args.root_password)
    manager = Api(args.base_url, args.manager_username, args.manager_password)
    user_one = Api(args.base_url, args.user_one, args.user_one_password)
    user_two = Api(args.base_url, args.user_two, args.user_two_password)
    clients = (root, manager, user_one, user_two)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "fixture_suffix": suffix,
        "logins": {},
        "catalogs": {},
        "settings": {},
        "orchestration": {},
        "drive": {},
        "video": {},
        "trading": {},
        "community": {},
        "governance": {},
        "launch": {},
        "incident": {},
        "restart_request": {},
        "audit": {},
        "cleanup": {},
        "errors": [],
    }
    before_settings: dict[str, Any] = {}
    drive_file_id = ""
    drive_storage_id = ""
    share_id = ""
    share_token = ""
    video_file_id = ""
    video_storage_id = ""
    video_id = 0
    spot_order_uuid = ""
    margin_uuid = ""
    bot_uuid = ""
    bot_order_uuid = ""
    thread_id = 0
    incident_active = False
    incident_id = ""
    incident_reason = ""
    incident_previous_mode = ""
    audit_start_id = 0
    governance_violation_id = 0
    governance_appeal_id = 0
    governance_target_user_id = 0
    governance_pre_violation_count: int | None = None
    governance_warn_executed = False
    governance_execute_attempted = False
    governance_appeal_approved = False
    governance_reason = ""
    restart_request_created = False
    funding_before: dict[str, Any] = {}
    pool_before: dict[str, Any] = {}

    try:
        if restart_request_file.exists() or restart_request_file.is_symlink():
            raise RuntimeError("restart_request_file_not_fresh")
        for client in clients:
            login = client.login()
            result["logins"][client.username] = {
                "status": login.get("status"),
                "ok": (login.get("body") or {}).get("ok") is True,
            }
            _must(login, f"login_{client.username}")
        audit_start_id = _audit_max_id(runtime_root)

        settings_body = _must(root.request("GET", "/api/admin/settings"), "settings_snapshot")
        settings = settings_body.get("settings") if isinstance(settings_body.get("settings"), Mapping) else {}
        missing = [key for key in SETTING_KEYS if key not in settings]
        if missing:
            raise RuntimeError(f"settings_snapshot_missing:{','.join(missing)}")
        before_settings = {key: settings.get(key) for key in SETTING_KEYS}
        provider_payload = require_real_provider_configuration()
        enabled_payload = {
            "feature_ai_agent_enabled": True,
            "feature_privacy_uploads_enabled": True,
            "feature_storage_albums_enabled": True,
            "feature_videos_enabled": True,
            "feature_trading_enabled": True,
            "feature_community_enabled": True,
            "feature_member_governance_enabled": True,
            "feature_audit_log_enabled": True,
            # The isolated load target starts with the secure audit chain
            # disabled.  Formal evidence must explicitly enable it, while
            # also suppressing the settings layer's automatic reseal flag:
            # every audit row is chained even while enforcement is disabled,
            # so a clean chain can be verified without mutating history.
            "audit_chain_enabled": True,
            "audit_chain_reseal_required": False,
            "module_ai_agent_min_role": "user",
            "ai_agent_allowed_tools": "",
            "ai_agent_operation_mode": "write",
            **provider_payload,
        }
        _must(root.request("PUT", "/api/admin/settings", json_body=enabled_payload), "settings_enable")
        enabled_read = _must(root.request("GET", "/api/admin/settings"), "settings_enable_readback")
        enabled_settings = enabled_read.get("settings") if isinstance(enabled_read.get("settings"), Mapping) else {}
        if any(enabled_settings.get(key) != value for key, value in enabled_payload.items()):
            raise RuntimeError("settings_enable_readback_mismatch")
        result["settings"] = {"before": before_settings, "enabled": enabled_payload, "enabled_readback": True}

        catalogs = {
            "root": _catalog(root, include_all=True),
            "manager": _catalog(manager),
            "user": _catalog(user_one),
        }
        user_names = set(catalogs["user"]["names"])
        manager_names = set(catalogs["manager"]["names"])
        root_names = set(catalogs["root"]["names"])
        if not {
            "write_cloud_drive_create_text",
            "write_video_publish",
            "write_trading_place_order",
            "write_trading_margin_open",
            "write_appeal_create",
        }.issubset(user_names):
            raise RuntimeError("user_catalog_missing_required_tools")
        if "write_governance_vote" in user_names or "write_server_restart" in user_names:
            raise RuntimeError("user_catalog_role_leak")
        if (
            "write_governance_vote" not in manager_names
            or "write_server_restart" in manager_names
            or "write_appeal_review" in manager_names
        ):
            raise RuntimeError("manager_catalog_role_scope_invalid")
        if not {
            "write_server_restart",
            "write_incident_enter",
            "write_incident_resolve",
            "write_launch_preflight_execute",
            "write_appeal_review",
            "write_trading_verify_jobs",
        }.issubset(root_names):
            raise RuntimeError("root_catalog_missing_required_tools")
        if any(not row.get("catalog_sha256") or not row.get("write_enabled") for row in catalogs.values()):
            raise RuntimeError("catalog_hash_or_write_mode_missing")
        result["catalogs"] = catalogs

        result["orchestration"] = _run_real_agent_orchestration(
            base_url=args.base_url,
            password=args.root_password,
            suffix=suffix,
            artifact_dir=artifact_dir,
            provider_config=provider_payload,
        )
        # The browser authenticates a second root session. Its mutations rotate
        # the bounded per-user CSRF history, so the API probe session must obtain
        # a fresh token before continuing with root write tools.
        root.refresh_csrf()
        # The browser uses a separate authenticated root session, but its
        # successful write must still be part of the exact durable audit
        # multiplicity contract checked below.
        root.successful_tools.append("write_album_create")

        # Cloud-drive text -> share -> update -> public read -> revoke -> delete.
        drive_name = f"formal-ai-agent-{suffix}.txt"
        drive_content = f"AI Agent strict lifecycle {suffix}"
        created_env = _tool(user_one, "write_cloud_drive_create_text", {
            "filename": drive_name,
            "content": drive_content,
            "privacy_mode": "standard_plain",
            "virtual_path": f"/formal/{suffix}/{drive_name}",
        })
        created = _tool_result(created_env)
        created_file = created.get("file") if isinstance(created.get("file"), Mapping) else {}
        created_storage = created.get("storage_file") if isinstance(created.get("storage_file"), Mapping) else {}
        drive_file_id = str(created_file.get("file_id") or "")
        drive_storage_id = str(created_storage.get("id") or "")
        if not drive_file_id or not drive_storage_id:
            raise RuntimeError("drive_create_identity_missing")
        drive_list = _must(user_one.request("GET", "/api/cloud-drive/files"), "drive_terminal_list")
        terminal_file = next(
            (
                dict(row)
                for row in (drive_list.get("files") or [])
                if isinstance(row, Mapping) and _cloud_drive_file_id(row) == drive_file_id
            ),
            {},
        )
        if not terminal_file or str(terminal_file.get("display_name") or terminal_file.get("filename") or "") != drive_name:
            raise RuntimeError("drive_terminal_file_missing")
        expected_drive_bytes = drive_content.encode("utf-8")
        owner_download = user_one.session.get(
            f"{args.base_url.rstrip('/')}/api/cloud-drive/files/{drive_file_id}/download",
            timeout=60,
        )
        if owner_download.status_code != 200 or owner_download.content != expected_drive_bytes:
            raise RuntimeError(
                f"drive_owner_content_mismatch:status={owner_download.status_code},"
                f"bytes={len(owner_download.content)}"
            )
        share_env = _tool(user_one, "write_share_create", {
            "storage_file_id": drive_storage_id,
            "can_preview": True,
            "access_scope": "link",
            "max_views": 0,
        })
        share_result = _tool_result(share_env)
        share = share_result.get("share") if isinstance(share_result.get("share"), Mapping) else {}
        share_id = str(share.get("id") or "")
        share_token = str(share.get("token") or "")
        if not share_id or not share_token:
            raise RuntimeError("drive_share_identity_missing")
        update_env = _tool(user_one, "write_share_update", {
            "share_type": "file", "share_id": share_id, "max_views": 7,
        })
        update_result = _tool_result(update_env)
        updated_share = update_result.get("share") if isinstance(update_result.get("share"), Mapping) else {}
        share_list = _must(user_one.request("GET", "/api/shares?limit=100"), "share_terminal_list")
        terminal_share = next(
            (dict(row) for row in (share_list.get("shares") or []) if isinstance(row, Mapping) and str(row.get("id") or "") == share_id),
            {},
        )
        public_before = requests.get(f"{args.base_url.rstrip('/')}/api/storage/shared/{share_token}", verify=False, timeout=60)
        public_download = requests.get(
            f"{args.base_url.rstrip('/')}/api/storage/shared/{share_token}/download",
            verify=False,
            timeout=60,
        )
        if (
            int(updated_share.get("max_views") or 0) != 7
            or int(terminal_share.get("max_views") or 0) != 7
            or public_before.status_code != 200
            or public_download.status_code != 200
            or public_download.content != expected_drive_bytes
        ):
            raise RuntimeError("drive_share_update_or_access_missing")
        revoke_env = _tool(user_one, "write_share_revoke", {"share_type": "file", "share_id": share_id})
        public_after = requests.get(f"{args.base_url.rstrip('/')}/api/storage/shared/{share_token}", verify=False, timeout=60)
        if public_after.status_code not in {404, 410}:
            raise RuntimeError(f"drive_share_revoke_not_terminal:{public_after.status_code}")
        delete_env = _tool(user_one, "write_cloud_drive_delete", {"file_id": drive_file_id})
        drive_file_id = ""
        drive_storage_id = ""
        drive_after = _must(user_one.request("GET", "/api/cloud-drive/files"), "drive_delete_readback")
        drive_absent = not any(
            isinstance(row, Mapping)
            and _cloud_drive_file_id(row) == _cloud_drive_file_id(created_file)
            for row in (drive_after.get("files") or [])
        )
        if not drive_absent:
            raise RuntimeError("drive_delete_not_terminal")
        result["drive"] = {
            "create": _tool_summary(created_env),
            "file_terminal": {"file_id": str(created_file.get("file_id") or ""), "name": drive_name, "present": True},
            "owner_content": {
                "status": owner_download.status_code,
                "size_bytes": len(owner_download.content),
                "sha256": hashlib.sha256(owner_download.content).hexdigest(),
                "expected_sha256": hashlib.sha256(expected_drive_bytes).hexdigest(),
                "exact": owner_download.content == expected_drive_bytes,
            },
            "share_create": _tool_summary(share_env),
            "share_id": share_id,
            "share_token_sha256": hashlib.sha256(share_token.encode()).hexdigest(),
            "share_update": _tool_summary(update_env),
            "terminal_max_views": int(terminal_share.get("max_views") or 0),
            "public_access_status": public_before.status_code,
            "shared_content": {
                "status": public_download.status_code,
                "size_bytes": len(public_download.content),
                "sha256": hashlib.sha256(public_download.content).hexdigest(),
                "expected_sha256": hashlib.sha256(expected_drive_bytes).hexdigest(),
                "exact": public_download.content == expected_drive_bytes,
            },
            "share_revoke": _tool_summary(revoke_env),
            "revoked_access_status": public_after.status_code,
            "delete": _tool_summary(delete_env),
            "file_absent": drive_absent,
        }
        share_id = ""
        share_token = ""

        # Binary upload is a user file-selection operation; publishing and
        # deleting the resulting video are performed through AI tools.
        fixture = _make_video_fixture(artifact_dir)
        fixture_path = Path(fixture["path"])
        with fixture_path.open("rb") as handle:
            upload = user_one.request(
                "POST",
                "/api/storage/files",
                files={"file": (fixture_path.name, handle, "video/mp4")},
                data={
                    "virtual_path": f"formal/{suffix}/{fixture_path.name}",
                    "display_name": fixture_path.name,
                    "privacy_mode": "standard_plain",
                },
                timeout=600,
            )
        upload_body = _must(upload, "video_upload")
        upload_file = upload_body.get("file") if isinstance(upload_body.get("file"), Mapping) else {}
        upload_storage = upload_body.get("storage_file") if isinstance(upload_body.get("storage_file"), Mapping) else {}
        video_file_id = str(upload_storage.get("file_id") or upload_file.get("file_id") or "")
        video_storage_id = str(upload_storage.get("id") or "")
        if not video_file_id or not video_storage_id:
            raise RuntimeError("video_upload_identity_missing")
        publish_env = _tool(user_one, "write_video_publish", {
            "cloud_file_id": video_file_id,
            "title": f"Formal AI Agent Video {suffix}",
            "visibility": "unlisted",
            "streaming_modes": ["prepared_hls"],
        })
        publish = _tool_result(publish_env)
        video = publish.get("video") if isinstance(publish.get("video"), Mapping) else {}
        video_id = int(video.get("id") or 0)
        if video_id <= 0:
            raise RuntimeError("video_publish_identity_missing")
        playback_wait = _wait_video_playback(user_one, video_id)
        playback_record = playback_wait["terminal"]
        playback = playback_record.get("body") if isinstance(playback_record.get("body"), Mapping) else {}
        hls = _hls_bytes(user_one, str(playback.get("master_url") or ""))
        delete_video_env = _tool(user_one, "write_video_delete", {"video_id": video_id})
        missing_playback = user_one.request("GET", f"/api/videos/{video_id}/playback")
        video_id = 0
        delete_video_file_env = _tool(user_one, "write_cloud_drive_delete", {"file_id": video_file_id})
        video_file_id = ""
        video_storage_id = ""
        if int(missing_playback.get("status") or 0) != 404:
            raise RuntimeError("video_delete_not_terminal")
        result["video"] = {
            "fixture": fixture,
            "upload": {"status": upload.get("status"), "file_id": str(upload_storage.get("file_id") or "")},
            "publish": _tool_summary(publish_env),
            "published_video_id": int(video.get("id") or 0),
            "streaming_modes": list(video.get("streaming_modes") or []),
            "terminal": {
                "attempts": playback_wait["attempts"],
                "status": (playback.get("status") or {}).get("status") if isinstance(playback.get("status"), Mapping) else "",
                "streaming_ready": playback.get("streaming_ready") is True,
                "mode": playback.get("mode"),
                "master_url_present": bool(playback.get("master_url")),
            },
            "hls": hls,
            "delete_video": _tool_summary(delete_video_env),
            "delete_cloud_file": _tool_summary(delete_video_file_env),
            "playback_after_delete_status": missing_playback.get("status"),
        }

        # Spot cancellation, margin borrow/repay, and a custom workflow bot.
        dashboard_before = _must(user_one.request("GET", "/api/trading/dashboard"), "trading_dashboard_before")
        trading_before = dashboard_before.get("trading") if isinstance(dashboard_before.get("trading"), Mapping) else {}
        funding_before = _funding_state(trading_before)
        pool_before = _funding_pool_state(trading_before)
        spot_env = _tool(user_one, "write_trading_place_order", {
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "limit",
            "quantity": "1",
            "limit_price_points": 1,
        })
        spot_result = _tool_result(spot_env)
        spot_order = spot_result.get("order") if isinstance(spot_result.get("order"), Mapping) else {}
        spot_order_uuid = str(spot_order.get("order_uuid") or "")
        if not spot_order_uuid or str(spot_order.get("status") or "") not in {"open", "pending", "partially_filled"}:
            raise RuntimeError(f"spot_order_not_open:{spot_order}")
        spot_cancel_env = _tool(user_one, "write_trading_cancel_order", {"order_uuid": spot_order_uuid})
        spot_dashboard = _must(user_one.request("GET", "/api/trading/dashboard"), "spot_cancel_terminal")
        spot_trading = spot_dashboard.get("trading") if isinstance(spot_dashboard.get("trading"), Mapping) else {}
        spot_terminal = _find_by(spot_trading.get("orders") or [], "order_uuid", spot_order_uuid)
        funding_after_spot = _funding_state(spot_trading)
        if spot_terminal.get("status") != "cancelled" or funding_after_spot != funding_before:
            raise RuntimeError(f"spot_cancel_not_terminal:{spot_terminal}")
        spot_order_uuid = ""

        quote_body = _must(
            user_one.request("GET", "/api/trading/live-price?market=ETH%2FPOINTS", timeout=60),
            "margin_live_quote",
        )
        quote_market = quote_body.get("market") if isinstance(quote_body.get("market"), Mapping) else {}
        risk_context = (
            quote_body.get("risk_grade_price_context")
            if isinstance(quote_body.get("risk_grade_price_context"), Mapping)
            else {}
        )
        if quote_body.get("high_risk_blocked") is True or risk_context.get("risk_grade_usable") is False:
            raise RuntimeError(f"margin_high_risk_quote_unusable:{quote_body}")
        quote_price = float(
            risk_context.get("price_points")
            or quote_market.get("manual_price_points")
            or 0
        )
        if quote_price <= 0:
            raise RuntimeError(f"margin_quote_price_missing:{quote_body}")
        funding = trading_before.get("funding") if isinstance(trading_before.get("funding"), Mapping) else {}
        available_points = int(funding.get("available_points") or 0)
        if available_points < 25:
            raise RuntimeError(f"margin_funding_too_low:{available_points}")
        market_settings = (
            trading_before.get("settings")
            if isinstance(trading_before.get("settings"), Mapping)
            else {}
        )
        maintenance_percent = float(market_settings.get("margin_maintenance_percent") or 15)
        financing_percent = float(market_settings.get("margin_long_financing_percent") or 90)
        fee_percent = float(quote_market.get("fee_rate_percent") or 0)
        collateral_percent = max(
            25.0,
            maintenance_percent + fee_percent + 10.0,
            100.0 - financing_percent + 10.0,
        )
        if collateral_percent >= 80:
            raise RuntimeError(f"margin_collateral_policy_unusable:{collateral_percent}")
        target_notional = float(min(200, max(50, available_points // 5)))
        lot_size = max(float(quote_market.get("lot_size") or 0.00000001), 0.00000001)
        raw_quantity = target_notional / quote_price
        margin_quantity = max(float(quote_market.get("min_order_size") or 0), raw_quantity)
        margin_quantity = math.ceil(margin_quantity / lot_size) * lot_size
        margin_quantity_text = f"{margin_quantity:.8f}".rstrip("0").rstrip(".")
        estimated_notional = max(1, int(math.ceil(margin_quantity * quote_price)))
        margin_collateral = int(math.ceil(estimated_notional * collateral_percent / 100.0)) + 3
        if margin_collateral >= estimated_notional or margin_collateral > available_points:
            raise RuntimeError(
                f"margin_derived_parameters_unusable:notional={estimated_notional},"
                f"collateral={margin_collateral},available={available_points}"
            )
        margin_env = _tool(user_one, "write_trading_margin_open", {
            "market_symbol": "ETH/POINTS",
            "position_type": "margin_long",
            "quantity": margin_quantity_text,
            "collateral_points": margin_collateral,
            "idempotency_key": f"formal-ai-margin-{suffix}",
        })
        margin_result = _tool_result(margin_env)
        margin_position = margin_result.get("position") if isinstance(margin_result.get("position"), Mapping) else {}
        margin_uuid = str(margin_position.get("position_uuid") or "")
        if not margin_uuid or margin_position.get("status") != "open" or not margin_position.get("borrowed_asset_symbol"):
            raise RuntimeError(f"margin_open_not_terminal:{margin_position}")
        margin_close_env = _tool(user_one, "write_trading_margin_close", {"position_uuid": margin_uuid})
        margin_dashboard = _must(user_one.request("GET", "/api/trading/dashboard"), "margin_close_terminal")
        margin_trading = margin_dashboard.get("trading") if isinstance(margin_dashboard.get("trading"), Mapping) else {}
        margin_terminal = _find_by(margin_trading.get("margin_positions") or [], "position_uuid", margin_uuid)
        funding_after_margin = _funding_state(margin_trading)
        pool_after_margin = _funding_pool_state(margin_trading)
        margin_issues = _margin_close_terminal_issues(
            position_uuid=margin_uuid,
            margin_terminal=margin_terminal,
            funding_before=funding_before,
            funding_after=funding_after_margin,
            pool_before=pool_before,
            pool_after=pool_after_margin,
        )
        if margin_issues:
            raise RuntimeError(
                "margin_close_not_terminal:"
                + json.dumps(
                    {
                        "issues": margin_issues,
                        "position": margin_terminal,
                        "funding_before": funding_before,
                        "funding_after": funding_after_margin,
                        "pool_before": pool_before,
                        "pool_after": pool_after_margin,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        margin_uuid = ""

        workflow = _workflow(suffix)
        bot_env = _tool(user_one, "write_trading_bot_create", {
            "bot_type": "conditional",
            "name": f"formal-ai-workflow-{suffix}",
            "market_symbol": "ETH/POINTS",
            "side": "buy",
            "order_type": "limit",
            "quantity": "1",
            "limit_price_points": 1,
            "trigger_type": "always",
            "trigger_price_points": 0,
            "enabled": True,
            "max_runs": 1,
            "cooldown_seconds": 0,
            "workflow_json": workflow,
        })
        bot_result = _tool_result(bot_env)
        created_bot = bot_result.get("bot") if isinstance(bot_result.get("bot"), Mapping) else {}
        bot_uuid = str(created_bot.get("bot_uuid") or "")
        if not bot_uuid or not isinstance(created_bot.get("workflow"), Mapping):
            raise RuntimeError(f"custom_workflow_bot_missing:{created_bot}")
        scan_env = _tool(user_one, "write_trading_bot_scan", {"limit": 50})
        scan_result = _tool_result(scan_env)
        triggered = next(
            (dict(row) for row in (scan_result.get("triggered") or []) if isinstance(row, Mapping) and str(row.get("bot_uuid") or "") == bot_uuid),
            {},
        )
        if not triggered or scan_result.get("failed"):
            raise RuntimeError(f"custom_workflow_scan_not_triggered:{scan_result}")
        bot_order_uuid = str(triggered.get("order_uuid") or "")
        if not bot_order_uuid:
            raise RuntimeError("custom_workflow_order_identity_missing")
        bot_cancel_env = _tool(user_one, "write_trading_cancel_order", {"order_uuid": bot_order_uuid})
        cancelled_bot_order_uuid = bot_order_uuid
        bot_order_uuid = ""
        bot_dashboard = _must(user_one.request("GET", "/api/trading/dashboard"), "custom_workflow_order_cancel_terminal")
        bot_trading = bot_dashboard.get("trading") if isinstance(bot_dashboard.get("trading"), Mapping) else {}
        bot_order_terminal = _find_by(bot_trading.get("orders") or [], "order_uuid", cancelled_bot_order_uuid)
        funding_after_bot = _funding_state(bot_trading)
        if bot_order_terminal.get("status") != "cancelled" or funding_after_bot != funding_after_margin:
            raise RuntimeError(f"custom_workflow_order_cancel_not_terminal:{bot_order_terminal}")
        bots_body = _must(user_one.request("GET", "/api/trading/bots"), "custom_workflow_terminal")
        terminal_bot = _find_by(bots_body.get("bots") or [], "bot_uuid", bot_uuid)
        if int(terminal_bot.get("run_count") or 0) != 1 or not isinstance(terminal_bot.get("workflow"), Mapping):
            raise RuntimeError(f"custom_workflow_terminal_invalid:{terminal_bot}")
        bot_delete = user_one.request("DELETE", f"/api/trading/bots/{bot_uuid}")
        _must(bot_delete, "custom_workflow_bot_delete")
        deleted_bot_uuid = bot_uuid
        bot_uuid = ""
        bots_after = _must(user_one.request("GET", "/api/trading/bots"), "custom_workflow_delete_terminal")
        bot_absent = not any(
            isinstance(row, Mapping) and str(row.get("bot_uuid") or "") == deleted_bot_uuid
            for row in (bots_after.get("bots") or [])
        )
        if not bot_absent:
            raise RuntimeError("custom_workflow_bot_not_deleted")
        verify_env = _tool(root, "write_trading_verify_jobs")
        verify_job = _wait_management_job(root, _tool_result(verify_env), timeout=300)
        verify_latest = _must(
            root.request("GET", "/api/root/trading/verify/latest"),
            "trading_verify_latest",
        )
        verification = (
            verify_latest.get("verification")
            if isinstance(verify_latest.get("verification"), Mapping)
            else {}
        )
        if (
            verify_job.get("terminal_status") != "succeeded"
            or verification.get("ok") is not True
            or list(verification.get("errors") or [])
        ):
            raise RuntimeError(f"trading_invariants_failed:{verify_job}:{verification}")
        result["trading"] = {
            "funding_before": funding_before,
            "spot": {
                "create": _tool_summary(spot_env),
                "order_uuid": str(spot_order.get("order_uuid") or ""),
                "initial_status": spot_order.get("status"),
                "cancel": _tool_summary(spot_cancel_env),
                "terminal_status": spot_terminal.get("status"),
                "funding_after": funding_after_spot,
            },
            "margin_lending": {
                "open": _tool_summary(margin_env),
                "position_uuid": str(margin_position.get("position_uuid") or ""),
                "initial_status": margin_position.get("status"),
                "borrowed_asset_symbol": margin_position.get("borrowed_asset_symbol"),
                "principal_points": margin_position.get("principal_points"),
                "derived_input": {
                    "quote_price_points": quote_price,
                    "quantity": margin_quantity_text,
                    "estimated_notional_points": estimated_notional,
                    "collateral_points": margin_collateral,
                    "collateral_percent": collateral_percent,
                    "available_points": available_points,
                    "risk_grade_usable": risk_context.get("risk_grade_usable"),
                },
                "close": _tool_summary(margin_close_env),
                "terminal_status": margin_terminal.get("status"),
                "interest_paid_points": margin_terminal.get("interest_paid_points"),
                "funding_after": funding_after_margin,
                "funding_pool_before": pool_before,
                "funding_pool_after": pool_after_margin,
            },
            "custom_workflow_bot": {
                "create": _tool_summary(bot_env),
                "bot_uuid": deleted_bot_uuid,
                "workflow_source": (terminal_bot.get("workflow") or {}).get("source"),
                "workflow_node_count": len((terminal_bot.get("workflow") or {}).get("nodes") or []),
                "scan": _tool_summary(scan_env),
                "scan_triggered": triggered,
                "run_count": terminal_bot.get("run_count"),
                "cancel_order": _tool_summary(bot_cancel_env),
                "cancelled_order_terminal_status": bot_order_terminal.get("status"),
                "funding_after": funding_after_bot,
                "delete_status": bot_delete.get("status"),
                "absent": bot_absent,
            },
            "invariants": {
                "verify": _tool_summary(verify_env),
                "job": verify_job,
                "verification_ok": verification.get("ok") is True,
                "errors": list(verification.get("errors") or []),
                "snapshot": verify_latest.get("snapshot") or {},
            },
        }

        # Community operations through a normal user AI catalog.
        boards = _must(user_one.request("GET", "/api/community/boards"), "community_boards")
        board = next(
            (
                dict(row) for row in (boards.get("boards") or [])
                if isinstance(row, Mapping)
                and int(row.get("id") or 0) > 0
                and str(row.get("status") or "") == "approved"
                and row.get("is_active") is not False
            ),
            {},
        )
        board_id = int(board.get("id") or 0)
        if board_id <= 0:
            raise RuntimeError("approved_community_board_missing")
        # Approved posts deliberately mint immutable points-chain rewards.
        # Deleting the content fixture must not pretend to erase that ledger;
        # capture the exact balances and ledger cursors so the permanent side
        # effect is explicit and independently attributable.
        manager_reward_before = _points_reward_state(manager)
        user_reward_before = _points_reward_state(user_one)
        title = f"Formal AI Agent Community {suffix}"
        thread_env = _tool(manager, "write_community_create_thread", {
            "board_id": board_id, "title": title, "content": f"Agent-created thread {suffix}",
        })
        thread_result = _tool_result(thread_env)
        thread_payload = thread_result.get("thread") if isinstance(thread_result.get("thread"), Mapping) else {}
        thread_id = int(thread_payload.get("id") or thread_result.get("thread_id") or 0)
        if thread_id <= 0:
            lookup = _must(
                user_one.request("GET", f"/api/community/boards/{board_id}/threads?limit=20&q={quote(title)}"),
                "community_thread_lookup",
            )
            found = next((dict(row) for row in (lookup.get("threads") or []) if isinstance(row, Mapping) and row.get("title") == title), {})
            thread_id = int(found.get("id") or 0)
        reply_text = f"AI Agent terminal reply {suffix}"
        reply_env = _tool(user_one, "write_community_reply_thread", {"thread_id": thread_id, "content": reply_text})
        detail = _must(user_two.request("GET", f"/api/community/threads/{thread_id}"), "community_terminal_reopen")
        terminal_thread = detail.get("thread") if isinstance(detail.get("thread"), Mapping) else {}
        terminal_reply = next(
            (dict(row) for row in (detail.get("posts") or []) if isinstance(row, Mapping) and str(row.get("content") or "") == reply_text),
            {},
        )
        if terminal_thread.get("title") != title or not terminal_reply:
            raise RuntimeError("community_ai_operations_not_terminal")
        manager_reward_after = _points_reward_state(manager)
        user_reward_after = _points_reward_state(user_one)
        post_reward = _exact_reward_row(
            manager_reward_after,
            after_id=int(manager_reward_before.get("max_ledger_id") or 0),
            action_type="forum_post_reward",
            reference_type="forum_thread",
            reference_id=thread_id,
        )
        comment_reward = _exact_reward_row(
            user_reward_after,
            after_id=int(user_reward_before.get("max_ledger_id") or 0),
            action_type="forum_comment_reward",
            reference_type="forum_post",
            reference_id=int(terminal_reply.get("id") or 0),
        )
        post_amount = int(post_reward.get("amount") or 0)
        comment_amount = int(comment_reward.get("amount") or 0)
        reward_accounted = bool(
            post_amount > 0
            and comment_amount > 0
            and int(manager_reward_after.get("points_balance") or 0)
            == int(manager_reward_before.get("points_balance") or 0) + post_amount
            and int(user_reward_after.get("points_balance") or 0)
            == int(user_reward_before.get("points_balance") or 0) + comment_amount
            and int(post_reward.get("balance_after") or 0)
            == int(post_reward.get("balance_before") or 0) + post_amount
            and int(comment_reward.get("balance_after") or 0)
            == int(comment_reward.get("balance_before") or 0) + comment_amount
        )
        if not reward_accounted:
            raise RuntimeError("community_persistent_reward_accounting_mismatch")
        result["community"] = {
            "create": _tool_summary(thread_env),
            "reply": _tool_summary(reply_env),
            "thread_id": thread_id,
            "terminal_title": terminal_thread.get("title"),
            "terminal_status": terminal_thread.get("status"),
            "terminal_reply_id": terminal_reply.get("id"),
            "persistent_rewards": {
                "accounted": reward_accounted,
                "thread_author": {
                    "username": manager.username,
                    "balance_before": manager_reward_before.get("points_balance"),
                    "balance_after": manager_reward_after.get("points_balance"),
                    "ledger_id": post_reward.get("id"),
                    "ledger_uuid": post_reward.get("ledger_uuid"),
                    "action_type": post_reward.get("action_type"),
                    "reference_type": post_reward.get("reference_type"),
                    "reference_id": post_reward.get("reference_id"),
                    "amount": post_amount,
                },
                "reply_author": {
                    "username": user_one.username,
                    "balance_before": user_reward_before.get("points_balance"),
                    "balance_after": user_reward_after.get("points_balance"),
                    "ledger_id": comment_reward.get("id"),
                    "ledger_uuid": comment_reward.get("ledger_uuid"),
                    "action_type": comment_reward.get("action_type"),
                    "reference_type": comment_reward.get("reference_type"),
                    "reference_id": comment_reward.get("reference_id"),
                    "amount": comment_amount,
                },
            },
        }

        # Cross-role governance: root proposes, manager votes, root executes.
        # The warning is immediately appealed and approved through supported
        # product APIs so the campaign account's violation count returns to
        # its exact pre-scenario value instead of contaminating later work.
        target_user_id = _profile_id(user_two)
        governance_target_user_id = target_user_id
        appeals_before = _must(user_two.request("GET", "/api/appeals"), "governance_appeals_before")
        governance_pre_violation_count = int(appeals_before.get("violation_count") or 0)
        governance_reason = f"Formal AI Agent governance lifecycle {suffix}"
        proposal_env = _tool(root, "write_governance_proposal_create", {
            "target_user_id": target_user_id,
            "action_type": "warn",
            "reason": governance_reason,
            "evidence": {"campaign": suffix, "reversible_fixture": True},
        })
        proposal_result = _tool_result(proposal_env)
        proposal = proposal_result.get("proposal") if isinstance(proposal_result.get("proposal"), Mapping) else {}
        proposal_id = int(proposal.get("id") or proposal_result.get("proposal_id") or 0)
        if (
            proposal_id <= 0
            or int(proposal.get("target_user_id") or 0) != target_user_id
            or str(proposal.get("action_type") or "") != "warn"
            or str(proposal.get("reason") or "") != governance_reason
            or str(proposal.get("status") or "") != "pending"
        ):
            raise RuntimeError(f"governance_proposal_identity_missing:{proposal}")
        vote_env = _tool(manager, "write_governance_vote", {
            "proposal_id": proposal_id, "vote": "approve", "comment": f"Formal approval {suffix}",
        })
        vote_result = _tool_result(vote_env)
        voted = vote_result.get("proposal") if isinstance(vote_result.get("proposal"), Mapping) else {}
        if (
            int(voted.get("id") or 0) != proposal_id
            or int(voted.get("target_user_id") or 0) != target_user_id
            or str(voted.get("action_type") or "") != "warn"
            or str(voted.get("reason") or "") != governance_reason
            or voted.get("status") != "approved"
        ):
            raise RuntimeError(f"governance_vote_not_approved:{voted}")
        governance_execute_attempted = True
        execute_env = _tool(root, "write_governance_execute", {"proposal_id": proposal_id})
        governance_warn_executed = True
        governance_detail = _must(root.request("GET", f"/api/admin/moderation/proposals/{proposal_id}"), "governance_terminal")
        terminal_proposal = governance_detail.get("proposal") if isinstance(governance_detail.get("proposal"), Mapping) else {}
        terminal_target = (
            terminal_proposal.get("target")
            if isinstance(terminal_proposal.get("target"), Mapping)
            else {}
        )
        if (
            int(terminal_proposal.get("id") or 0) != proposal_id
            or int(terminal_proposal.get("target_user_id") or 0) != target_user_id
            or str(terminal_proposal.get("action_type") or "") != "warn"
            or str(terminal_proposal.get("reason") or "") != governance_reason
            or terminal_proposal.get("status") != "executed"
            or not str(terminal_proposal.get("executed_at") or "")
            or int(terminal_target.get("id") or 0) != target_user_id
            or str(terminal_target.get("username") or "") != user_two.username
        ):
            raise RuntimeError(f"governance_execute_not_terminal:{terminal_proposal}")
        appeals_after_warn = _must(user_two.request("GET", "/api/appeals"), "governance_warning_readback")
        if int(appeals_after_warn.get("violation_count") or 0) != governance_pre_violation_count + 1:
            raise RuntimeError(f"governance_warning_count_not_applied:{appeals_after_warn}")
        latest_violation = _exact_governance_violation(
            appeals_after_warn,
            reason=governance_reason,
            target_user_id=target_user_id,
            username=user_two.username,
            actor_username=root.username,
        )
        governance_violation_id = int(latest_violation.get("id") or 0)
        if governance_violation_id <= 0:
            raise RuntimeError("governance_warning_violation_identity_missing")
        appeal_create_env = _tool(user_two, "write_appeal_create", {
            "violation_id": governance_violation_id,
            "reason": f"Formal reversible governance verification {suffix}",
        })
        appeals_pending = _must(user_two.request("GET", "/api/appeals"), "governance_appeal_pending")
        pending_appeal = _exact_governance_appeal(
            appeals_pending,
            violation_id=governance_violation_id,
            target_user_id=target_user_id,
            username=user_two.username,
        )
        governance_appeal_id = int(pending_appeal.get("id") or 0)
        if (
            governance_appeal_id <= 0
            or pending_appeal.get("status") != "pending"
            or int(pending_appeal.get("violation_count_snapshot") or -1)
            != governance_pre_violation_count + 1
            or int(pending_appeal.get("penalty_points") or 0) != 1
            or not str(pending_appeal.get("pre_status") or "")
            or not str(pending_appeal.get("pre_role") or "")
        ):
            raise RuntimeError(f"governance_appeal_not_pending:{pending_appeal}")
        appeal_review_env = _tool(root, "write_appeal_review", {
            "appeal_id": governance_appeal_id,
            "action": "approve",
            "note": f"Formal governance fixture rollback {suffix}",
        })
        appeals_restored = _must(user_two.request("GET", "/api/appeals"), "governance_appeal_restored")
        restored_appeal = _exact_governance_appeal(
            appeals_restored,
            violation_id=governance_violation_id,
            appeal_id=governance_appeal_id,
            target_user_id=target_user_id,
            username=user_two.username,
        )
        restored_violation_count = appeals_restored.get("violation_count")
        restored_violation = _exact_governance_violation(
            appeals_restored,
            reason=governance_reason,
            violation_id=governance_violation_id,
            target_user_id=target_user_id,
            username=user_two.username,
            actor_username=root.username,
        )
        violation_appeal = (
            restored_violation.get("appeal")
            if isinstance(restored_violation.get("appeal"), Mapping)
            else {}
        )
        governance_appeal_approved = bool(
            restored_appeal.get("status") == "approved"
            and int(restored_appeal.get("user_id") or 0) == target_user_id
            and str(restored_appeal.get("username") or "") == user_two.username
            and int(restored_appeal.get("latest_violation_id") or 0) == governance_violation_id
            and int(restored_appeal.get("violation_count_snapshot") or -1)
            == governance_pre_violation_count + 1
            and int(restored_appeal.get("penalty_points") or 0) == 1
            and str(restored_appeal.get("reviewed_by") or "") == root.username
            and bool(str(restored_appeal.get("reviewed_at") or ""))
            and restored_violation.get("is_resolved") is True
            and int(violation_appeal.get("id") or 0) == governance_appeal_id
            and violation_appeal.get("status") == "approved"
            and restored_violation_count is not None
            and int(restored_violation_count) == governance_pre_violation_count
        )
        if not governance_appeal_approved:
            raise RuntimeError(
                f"governance_appeal_did_not_restore_account:{restored_appeal}:"
                f"count={appeals_restored.get('violation_count')}"
            )
        result["governance"] = {
            "create": _tool_summary(proposal_env),
            "vote": _tool_summary(vote_env),
            "execute": _tool_summary(execute_env),
            "proposal_id": proposal_id,
            "approved_status": voted.get("status"),
            "terminal_status": terminal_proposal.get("status"),
            "executed_at": terminal_proposal.get("executed_at"),
            "target_user_id": terminal_proposal.get("target_user_id"),
            "action_type": terminal_proposal.get("action_type"),
            "violation_id": governance_violation_id,
            "violation_count_before": governance_pre_violation_count,
            "violation_count_after_warning": appeals_after_warn.get("violation_count"),
            "appeal_create": _tool_summary(appeal_create_env),
            "appeal_id": governance_appeal_id,
            "appeal_review": _tool_summary(appeal_review_env),
            "appeal_terminal_status": restored_appeal.get("status"),
            "violation_count_restored": appeals_restored.get("violation_count"),
            "account_state_restored": governance_appeal_approved,
        }

        # Dry-run launch gate must never switch the server mode.
        mode_before_body = _must(root.request("GET", "/api/root/server-mode"), "mode_before_launch")
        mode_before = _mode(mode_before_body)
        incident_previous_mode = mode_before
        launch_env = _tool(root, "write_launch_preflight_execute", {
            "target_mode": "production",
            "auto_switch": False,
            "force_audit": True,
            "reason": f"Formal AI Agent dry run {suffix}",
        })
        launch_result = _tool_result(launch_env)
        step_names = [str(row.get("name") or "") for row in (launch_result.get("steps") or []) if isinstance(row, Mapping)]
        launch_blockers = [dict(row) if isinstance(row, Mapping) else {"value": str(row)} for row in (launch_result.get("blockers") or [])]
        preflight_passed = launch_result.get("preflight_passed")
        mode_after_launch = _mode(_must(root.request("GET", "/api/root/server-mode"), "mode_after_launch"))
        if (
            launch_result.get("dry_run") is not True
            or launch_result.get("auto_switch") is not False
            or mode_after_launch != mode_before
            or set(step_names) != {"requirements_gate", "log_chain_verify", "ai_agent_audit_scan", "switch_production", "final_mode_status"}
            or not isinstance(preflight_passed, bool)
            or preflight_passed != (not launch_blockers)
        ):
            raise RuntimeError(f"launch_dry_run_invalid:{launch_result}")
        logs_env = _tool(root, "write_launch_logs_verify")
        logs_result = _tool_result(logs_env)
        audit_scan_env = _tool(root, "audit_scan", {"force": True})
        audit_scan_result = _tool_result(audit_scan_env)
        if logs_result.get("ok") is not True or int(logs_result.get("broken_links") or 0) != 0:
            raise RuntimeError(f"server_mode_log_chain_invalid:{logs_result}")
        if not isinstance(audit_scan_result.get("scan"), Mapping):
            raise RuntimeError("ai_agent_audit_scan_terminal_missing")
        result["launch"] = {
            "preflight": _tool_summary(launch_env),
            "dry_run": launch_result.get("dry_run"),
            "auto_switch": launch_result.get("auto_switch"),
            "preflight_passed": preflight_passed,
            "blocker_count": len(launch_blockers),
            "blockers": launch_blockers,
            "outcome_consistent": preflight_passed == (not launch_blockers),
            "step_names": step_names,
            "mode_before": mode_before,
            "mode_after": mode_after_launch,
            "logs_verify": {
                **_tool_summary(logs_env),
                "chain_length": logs_result.get("chain_length"),
                "broken_links": logs_result.get("broken_links"),
                "result": logs_result.get("result"),
            },
            "audit_scan": {
                **_tool_summary(audit_scan_env),
                "has_scan": isinstance(audit_scan_result.get("scan"), Mapping),
            },
        }

        # Enter and resolve incident through AI tools.  The global request
        # guard only admits this gateway during lockdown for the resolve tool.
        incident_reason = f"Formal AI Agent incident lifecycle {suffix}"
        incident_enter_env = _tool(root, "write_incident_enter", {
            "confirm": "ENTER_INCIDENT_LOCKDOWN",
            "trigger_type": "formal_ai_agent",
            "reason": incident_reason,
            "verification": {"campaign": suffix, "phase": "enter"},
        })
        incident_active = True
        incident_enter_result = _tool_result(incident_enter_env)
        incident_id = str(incident_enter_result.get("incident_id") or "")
        if not incident_id:
            raise RuntimeError(f"incident_enter_identity_missing:{incident_enter_result}")

        # The incident profile intentionally enables browser-only access and
        # advances the security epoch.  Prove a non-browser requests client is
        # rejected, then recover with a genuinely new Chromium root session.
        # No forged User-Agent and no maintenance bypass token are permitted.
        nonbrowser_response = root.session.get(
            f"{root.base_url}/api/csrf-token",
            timeout=30,
        )
        nonbrowser_denial = _record(nonbrowser_response)
        nonbrowser_body = (
            nonbrowser_denial.get("body")
            if isinstance(nonbrowser_denial.get("body"), Mapping)
            else {}
        )
        if (
            int(nonbrowser_denial.get("status") or 0) != 403
            or nonbrowser_body.get("requires") != "maintenance_bypass_token"
            or nonbrowser_body.get("header") != "X-Maintenance-Bypass-Token"
        ):
            raise RuntimeError(f"incident_nonbrowser_not_rejected:{nonbrowser_denial}")

        browser_recovery = _run_browser_incident_recovery(
            base_url=args.base_url,
            password=args.root_password,
            expected_incident_id=incident_id,
            expected_reason=incident_reason,
            expected_mode=mode_before,
            notes=f"Formal AI Agent incident resolved {suffix}",
            verification={"campaign": suffix, "phase": "resolve", "transport": "playwright_chromium"},
            artifact_dir=artifact_dir,
            suffix=suffix,
        )
        incident_enter_relogin = browser_recovery["login"]
        incident_status = _must(browser_recovery["incident_before"], "incident_enter_terminal")
        incident_row = incident_status.get("incident") if isinstance(incident_status.get("incident"), Mapping) else {}
        if not incident_row.get("active") and str(incident_row.get("status") or "") not in {"active", "open"}:
            raise RuntimeError(f"incident_not_active:{incident_row}")
        incident_resolve_body = browser_recovery["resolve"].get("body")
        incident_resolve_env = (
            dict(incident_resolve_body)
            if isinstance(incident_resolve_body, Mapping)
            else {}
        )
        if not incident_resolve_env:
            raise RuntimeError("incident_browser_ai_resolve_envelope_missing")
        # The browser used a separate authenticated root session, so include
        # its successful gateway call in the exact durable audit contract.
        root.successful_tools.append("write_incident_resolve")
        incident_active = False
        # The browser helper performs a second root login after resolution
        # before its terminal readback.  Refresh the requests client only
        # after that independent browser proof so later cleanup/operations do
        # not accidentally keep using the pre-lockdown session.
        incident_resolve_relogin = browser_recovery["post_resolve_login"]
        _must(incident_resolve_relogin, "incident_resolve_root_browser_relogin")
        api_relogin_after_restore = root.login()
        _must(api_relogin_after_restore, "incident_resolve_root_api_relogin")
        resolved_status = _must(browser_recovery["incident_after"], "incident_resolve_terminal")
        resolved_row = resolved_status.get("incident") if isinstance(resolved_status.get("incident"), Mapping) else {}
        resolved_mode = _mode(_must(browser_recovery["mode_after"], "incident_mode_restored"))
        if resolved_row.get("active") is True or str(resolved_row.get("status") or "") in {"active", "open"} or resolved_mode != mode_before:
            raise RuntimeError(f"incident_resolve_or_mode_restore_failed:{resolved_row}:{resolved_mode}")
        result["incident"] = {
            "enter": _tool_summary(incident_enter_env),
            "incident_id": incident_id,
            "nonbrowser_denial": nonbrowser_denial,
            "enter_root_relogin": {
                "status": incident_enter_relogin.get("status"),
                "ok": (incident_enter_relogin.get("body") or {}).get("ok") is True,
                "transport": "playwright_chromium",
                "session_terminal_ok": (
                    (browser_recovery.get("me") or {}).get("body") or {}
                ).get("ok") is True,
            },
            "active_terminal": {"active": incident_row.get("active"), "status": incident_row.get("status")},
            "resolve": _tool_summary(incident_resolve_env),
            "resolve_root_relogin": {
                "status": incident_resolve_relogin.get("status"),
                "ok": (incident_resolve_relogin.get("body") or {}).get("ok") is True,
                "transport": "playwright_chromium",
                "session_terminal_ok": (
                    (browser_recovery.get("post_resolve_me") or {}).get("body") or {}
                ).get("ok") is True,
            },
            "api_relogin_after_restore": {
                "status": api_relogin_after_restore.get("status"),
                "ok": (api_relogin_after_restore.get("body") or {}).get("ok") is True,
            },
            "resolved_terminal": {"active": resolved_row.get("active"), "status": resolved_row.get("status")},
            "mode_before": mode_before,
            "mode_after": resolved_mode,
            "browser_recovery": browser_recovery,
        }

        # Remove the reversible community fixture before requesting restart.
        thread_delete = root.request(
            "DELETE", f"/api/community/threads/{thread_id}", json_body={"reason": "formal AI Agent fixture cleanup"},
        )
        _must(thread_delete, "community_thread_delete")
        thread_soft_delete = _verify_community_soft_delete(root, user_two, thread_id)
        result["community"]["delete_status"] = thread_delete.get("status")
        result["community"]["root_audit_status"] = thread_soft_delete["root_audit_status"]
        result["community"]["root_audit_is_deleted"] = thread_soft_delete["root_audit_is_deleted"]
        result["community"]["root_audit_deleted_at"] = thread_soft_delete["root_audit_deleted_at"]
        result["community"]["root_audit_deleted_by"] = thread_soft_delete["root_audit_deleted_by"]
        result["community"]["member_absent_status"] = thread_soft_delete["member_absent_status"]
        # Compatibility field retained for existing machine selectors.  Its
        # semantics are explicitly the ordinary-member view, never root.
        result["community"]["absent_status"] = thread_soft_delete["member_absent_status"]
        thread_id = 0

        restart_reason = f"Formal AI Agent restart {suffix}"
        restart_env = _tool(root, "write_server_restart", {"reason": restart_reason})
        restart_result = _tool_result(restart_env)
        restart_payload = restart_result.get("restart") if isinstance(restart_result.get("restart"), Mapping) else {}
        if (
            restart_payload.get("mode") != "supervised-request"
            or restart_payload.get("requires_supervisor_restart") is not True
            or restart_payload.get("request_schema_version") != "hackme.supervised-restart-request/v1"
            or not restart_payload.get("request_nonce")
            or not restart_request_file.is_file()
            or restart_request_file.is_symlink()
        ):
            raise RuntimeError(f"supervised_restart_request_invalid:{restart_result}")
        restart_request_created = True
        receipt = json.loads(restart_request_file.read_text(encoding="utf-8"))
        if (
            receipt.get("schema_version") != "hackme.supervised-restart-request/v1"
            or receipt.get("nonce") != restart_payload.get("request_nonce")
            or int(receipt.get("requesting_pid") or 0) <= 0
            or receipt.get("reason") != restart_reason
        ):
            raise RuntimeError(f"supervised_restart_receipt_mismatch:{receipt}")
        result["restart_request"] = {
            "tool": _tool_summary(restart_env),
            "mode": restart_payload.get("mode"),
            "requires_supervisor_restart": restart_payload.get("requires_supervisor_restart"),
            "request_schema_version": restart_payload.get("request_schema_version"),
            "request_nonce": restart_payload.get("request_nonce"),
            "receipt_schema_version": receipt.get("schema_version"),
            "receipt_nonce_matches": receipt.get("nonce") == restart_payload.get("request_nonce"),
            "requesting_pid": receipt.get("requesting_pid"),
            "reason": receipt.get("reason"),
            "reason_matches_request": receipt.get("reason") == restart_reason,
        }

        security = _must(root.request("GET", "/api/admin/security-center"), "security_center_audit")
        center = security.get("security_center") if isinstance(security.get("security_center"), Mapping) else {}
        audit_integrity = (
            center.get("audit_integrity")
            if isinstance(center.get("audit_integrity"), Mapping)
            else {}
        )
        if audit_integrity.get("enabled") is not True or audit_integrity.get("ok") is not True:
            raise RuntimeError(f"secure_audit_chain_invalid:{audit_integrity}")
        audit_entries = [dict(row) for row in (center.get("audit_entries") or []) if isinstance(row, Mapping)]
        recent_ai_tool_entries = [
            row for row in audit_entries
            if str(row.get("action") or "") == "AI_AGENT_WRITE_TOOL" and row.get("success") is True
        ]
        durable_ai_rows = _audit_tool_rows(runtime_root, audit_start_id)
        ai_tool_entries = [row for row in durable_ai_rows if int(row.get("success") or 0) == 1]
        expected_counter = Counter(
            (client.username, tool_name)
            for client in clients
            for tool_name in client.successful_tools
        )
        actual_counter: Counter[tuple[str, str]] = Counter()
        for row in ai_tool_entries:
            detail = str(row.get("detail") or "")
            if "tool=" not in detail:
                continue
            tool_name = detail.split("tool=", 1)[1].split(",", 1)[0]
            actual_counter[(str(row.get("user") or ""), tool_name)] += 1
        missing_expected = {
            f"{actor}:{tool_name}": expected_count - actual_counter.get((actor, tool_name), 0)
            for (actor, tool_name), expected_count in expected_counter.items()
            if actual_counter.get((actor, tool_name), 0) < expected_count
        }
        if missing_expected:
            raise RuntimeError(f"ai_tool_audit_entries_missing:{missing_expected}")
        audited_tools = {tool_name for (_actor, tool_name), count in actual_counter.items() if count > 0}
        result["audit"] = {
            "entry_count": len(audit_entries),
            "ai_tool_success_count": len(ai_tool_entries),
            "security_center_recent_ai_tool_success_count": len(recent_ai_tool_entries),
            "audit_start_id": audit_start_id,
            "audit_last_id": max((int(row.get("id") or 0) for row in durable_ai_rows), default=audit_start_id),
            "audited_tools": sorted(audited_tools),
            "expected_tool_calls": {
                f"{actor}:{tool_name}": count
                for (actor, tool_name), count in sorted(expected_counter.items())
            },
            "audited_tool_calls": {
                f"{actor}:{tool_name}": count
                for (actor, tool_name), count in sorted(actual_counter.items())
                if (actor, tool_name) in expected_counter
            },
            "expected_tool_call_count": sum(expected_counter.values()),
            "audited_expected_tool_call_count": sum(
                min(count, actual_counter.get(key, 0))
                for key, count in expected_counter.items()
            ),
            "missing_expected_tool_calls": missing_expected,
            "required_tools_present": not missing_expected,
            "secure_audit_chain": dict(audit_integrity),
            "log_chain_verified": logs_result.get("ok") is True and int(logs_result.get("broken_links") or 0) == 0,
            "audit_scan_terminal": isinstance(audit_scan_result.get("scan"), Mapping),
        }

        restored = _must(root.request("PUT", "/api/admin/settings", json_body=before_settings), "settings_restore")
        del restored
        restored_read = _must(root.request("GET", "/api/admin/settings"), "settings_restore_readback")
        restored_settings = restored_read.get("settings") if isinstance(restored_read.get("settings"), Mapping) else {}
        settings_restored = all(restored_settings.get(key) == value for key, value in before_settings.items())
        if not settings_restored:
            raise RuntimeError("settings_restore_mismatch")
        result["cleanup"] = {
            "settings_restored": settings_restored,
            "orchestration_album_absent": result["orchestration"].get("cleanup", {}).get("album_absent") is True,
            "drive_fixture_absent": result["drive"].get("file_absent") is True,
            "video_fixture_absent": result["video"].get("playback_after_delete_status") == 404,
            "trading_orders_terminal": result["trading"].get("spot", {}).get("terminal_status") == "cancelled"
            and result["trading"].get("margin_lending", {}).get("terminal_status") == "closed",
            "custom_workflow_bot_absent": result["trading"].get("custom_workflow_bot", {}).get("absent") is True,
            "community_thread_absent": result["community"].get("absent_status") == 404,
            "community_thread_soft_deleted": result["community"].get("root_audit_is_deleted") is True,
            "community_persistent_rewards_accounted": result["community"].get("persistent_rewards", {}).get("accounted") is True,
            "governance_account_restored": result["governance"].get("account_state_restored") is True,
            "incident_resolved": result["incident"].get("mode_after") == result["incident"].get("mode_before"),
            "restart_receipt_preserved_for_supervisor": restart_request_file.is_file(),
            "errors": [],
        }
        result["ok"] = bool(
            not result["errors"]
            and result["catalogs"]["root"]["role_scoped"]
            and result["drive"]["file_absent"]
            and result["video"]["terminal"]["streaming_ready"]
            and result["video"]["hls"]["segment_bytes"] > 0
            and result["trading"]["spot"]["terminal_status"] == "cancelled"
            and result["trading"]["margin_lending"]["terminal_status"] == "closed"
            and result["trading"]["custom_workflow_bot"]["absent"]
            and result["governance"]["terminal_status"] == "executed"
            and result["launch"]["dry_run"] is True
            and result["incident"]["mode_after"] == result["incident"]["mode_before"]
            and result["restart_request"]["receipt_nonce_matches"]
            and result["audit"]["required_tools_present"]
            and all(value is True for value in result["cleanup"].values() if isinstance(value, bool))
        )
    except Exception as exc:
        result["errors"].append(f"{exc.__class__.__name__}: {exc}")
    finally:
        cleanup_errors: list[str] = []
        try:
            if incident_active:
                cleanup_recovery = _run_browser_incident_recovery(
                    base_url=args.base_url,
                    password=args.root_password,
                    expected_incident_id=incident_id,
                    expected_reason=incident_reason,
                    expected_mode=incident_previous_mode,
                    notes="formal probe failure cleanup",
                    verification={"campaign": suffix, "phase": "failure_cleanup", "transport": "playwright_chromium"},
                    artifact_dir=artifact_dir,
                    suffix=f"{suffix}_cleanup",
                    allow_already_resolved=True,
                )
                incident_active = False
                _must(root.login(), "cleanup_incident_root_relogin_after_resolve")
                incident_readback = _must(cleanup_recovery["incident_after"], "cleanup_incident_readback")
                incident_row = (
                    incident_readback.get("incident")
                    if isinstance(incident_readback.get("incident"), Mapping)
                    else {}
                )
                mode_readback = _mode(_must(cleanup_recovery["mode_after"], "cleanup_incident_mode_readback"))
                if incident_row.get("active") is True or (
                    incident_previous_mode and mode_readback != incident_previous_mode
                ):
                    raise RuntimeError(f"incident_cleanup_not_terminal:{incident_row}:{mode_readback}")
        except Exception as exc:
            cleanup_errors.append(f"incident:{exc.__class__.__name__}:{exc}")
        try:
            if governance_execute_attempted and not governance_appeal_approved and user_two.csrf and root.csrf:
                appeals = _must(user_two.request("GET", "/api/appeals"), "cleanup_governance_list")
                if governance_violation_id <= 0:
                    recovered_violation = _exact_governance_violation(
                        appeals,
                        reason=governance_reason,
                        target_user_id=governance_target_user_id,
                        username=user_two.username,
                        actor_username=root.username,
                        allow_absent=True,
                    )
                    governance_violation_id = int(recovered_violation.get("id") or 0)
                    governance_warn_executed = governance_violation_id > 0
                if governance_violation_id <= 0:
                    result.setdefault("cleanup", {})["governance_no_side_effect"] = True
                else:
                    pending = _exact_governance_appeal(
                        appeals,
                        violation_id=governance_violation_id,
                        appeal_id=governance_appeal_id,
                        target_user_id=governance_target_user_id,
                        username=user_two.username,
                        allow_absent=True,
                    )
                    if not pending:
                        _must(user_two.request(
                            "POST",
                            "/api/appeals",
                            json_body={
                                "violation_id": governance_violation_id,
                                "reason": "Formal probe failure rollback",
                            },
                        ), "cleanup_governance_appeal_create")
                        appeals = _must(
                            user_two.request("GET", "/api/appeals"),
                            "cleanup_governance_appeal_lookup",
                        )
                        pending = _exact_governance_appeal(
                            appeals,
                            violation_id=governance_violation_id,
                            target_user_id=governance_target_user_id,
                            username=user_two.username,
                        )
                    governance_appeal_id = int(pending.get("id") or 0)
                    pending_status = str(pending.get("status") or "")
                    if pending_status in {"pending", "reviewing_approve"}:
                        _must(root.request(
                            "POST",
                            f"/api/admin/appeals/{governance_appeal_id}/review",
                            json_body={"action": "approve", "note": "Formal probe failure rollback"},
                        ), "cleanup_governance_appeal_review")
                    elif pending_status != "approved":
                        raise RuntimeError(
                            f"cleanup_governance_appeal_unrecoverable_status:{pending_status}"
                        )
                    restored = _must(
                        user_two.request("GET", "/api/appeals"),
                        "cleanup_governance_restore_readback",
                    )
                    restored_row = _exact_governance_appeal(
                        restored,
                        violation_id=governance_violation_id,
                        appeal_id=governance_appeal_id,
                        target_user_id=governance_target_user_id,
                        username=user_two.username,
                    )
                    restored_violation = _exact_governance_violation(
                        restored,
                        reason=governance_reason,
                        violation_id=governance_violation_id,
                        target_user_id=governance_target_user_id,
                        username=user_two.username,
                        actor_username=root.username,
                    )
                    linked_appeal = (
                        restored_violation.get("appeal")
                        if isinstance(restored_violation.get("appeal"), Mapping)
                        else {}
                    )
                    restored_violation_count = restored.get("violation_count")
                    governance_appeal_approved = bool(
                        restored_row.get("status") == "approved"
                        and str(restored_row.get("reviewed_by") or "") == root.username
                        and restored_violation.get("is_resolved") is True
                        and int(linked_appeal.get("id") or 0) == governance_appeal_id
                        and linked_appeal.get("status") == "approved"
                        and governance_pre_violation_count is not None
                        and restored_violation_count is not None
                        and int(restored_violation_count) == governance_pre_violation_count
                    )
                    if not governance_appeal_approved:
                        raise RuntimeError(
                            f"cleanup_governance_not_restored:{restored_row}:"
                            f"count={restored.get('violation_count')}"
                        )
                    result.setdefault("cleanup", {})["governance_account_restored"] = governance_appeal_approved
        except Exception as exc:
            cleanup_errors.append(f"governance:{exc.__class__.__name__}:{exc}")
        try:
            if share_id and user_one.csrf:
                _must(
                    user_one.request("POST", f"/api/shares/file/{share_id}/revoke", json_body={}),
                    "cleanup_share_revoke",
                )
                if share_token:
                    denied = requests.get(
                        f"{args.base_url.rstrip('/')}/api/storage/shared/{share_token}",
                        verify=False,
                        timeout=60,
                    )
                    if denied.status_code not in {404, 410}:
                        raise RuntimeError(f"cleanup_share_still_accessible:{denied.status_code}")
        except Exception as exc:
            cleanup_errors.append(f"share:{exc.__class__.__name__}:{exc}")
        try:
            for order_uuid in (spot_order_uuid, bot_order_uuid):
                if order_uuid and user_one.csrf:
                    _must(
                        user_one.request("POST", f"/api/trading/orders/{order_uuid}/cancel", json_body={}),
                        f"cleanup_order_cancel_{order_uuid}",
                    )
            if margin_uuid and user_one.csrf:
                margin_cleanup_readback = _must(
                    user_one.request("GET", "/api/trading/dashboard"),
                    "cleanup_margin_status_readback",
                )
                margin_cleanup_state = (
                    margin_cleanup_readback.get("trading")
                    if isinstance(margin_cleanup_readback.get("trading"), Mapping)
                    else {}
                )
                if _margin_cleanup_action(
                    margin_cleanup_state.get("margin_positions") or [],
                    margin_uuid,
                ) == "close":
                    _must(
                        user_one.request("POST", f"/api/trading/margin/{margin_uuid}/close", json_body={}),
                        "cleanup_margin_close",
                    )
            if bot_uuid and user_one.csrf:
                _must(user_one.request("DELETE", f"/api/trading/bots/{bot_uuid}"), "cleanup_bot_delete")
            if any((spot_order_uuid, bot_order_uuid, margin_uuid, bot_uuid)) and user_one.csrf:
                trading_readback = _must(
                    user_one.request("GET", "/api/trading/dashboard"),
                    "cleanup_trading_readback",
                )
                trading_state = (
                    trading_readback.get("trading")
                    if isinstance(trading_readback.get("trading"), Mapping)
                    else {}
                )
                terminal_orders = {
                    str(row.get("order_uuid") or ""): str(row.get("status") or "")
                    for row in (trading_state.get("orders") or [])
                    if isinstance(row, Mapping)
                }
                for order_uuid in (spot_order_uuid, bot_order_uuid):
                    if order_uuid and terminal_orders.get(order_uuid) != "cancelled":
                        raise RuntimeError(f"cleanup_order_not_cancelled:{order_uuid}")
                if margin_uuid:
                    margin_row = _find_by(trading_state.get("margin_positions") or [], "position_uuid", margin_uuid)
                    margin_cleanup_issues = _margin_close_terminal_issues(
                        position_uuid=margin_uuid,
                        margin_terminal=margin_row,
                        funding_before=funding_before,
                        funding_after=_funding_state(trading_state),
                        pool_before=pool_before,
                        pool_after=_funding_pool_state(trading_state),
                        expected_statuses=("closed", "liquidated"),
                    )
                    if margin_cleanup_issues:
                        raise RuntimeError(
                            f"cleanup_margin_not_terminal:{margin_uuid}:"
                            + ",".join(margin_cleanup_issues)
                        )
        except Exception as exc:
            cleanup_errors.append(f"trading:{exc.__class__.__name__}:{exc}")
        try:
            if thread_id and root.csrf:
                _must(
                    root.request("DELETE", f"/api/community/threads/{thread_id}", json_body={"reason": "probe failure cleanup"}),
                    "cleanup_community_thread_delete",
                )
                cleanup_soft_delete = _verify_community_soft_delete(root, user_two, thread_id)
                result.setdefault("cleanup", {})["community_soft_delete"] = cleanup_soft_delete
        except Exception as exc:
            cleanup_errors.append(f"community:{exc.__class__.__name__}:{exc}")
        try:
            if video_id and user_one.csrf:
                _must(
                    user_one.request("DELETE", f"/api/videos/{video_id}/manage"),
                    "cleanup_video_delete",
                )
                video_missing = user_one.request("GET", f"/api/videos/{video_id}/playback")
                if int(video_missing.get("status") or 0) != 404:
                    raise RuntimeError(f"cleanup_video_present:{video_missing}")
            for file_id in (drive_file_id, video_file_id):
                if file_id and user_one.csrf:
                    _must(
                        user_one.request("DELETE", f"/api/cloud-drive/files/{file_id}"),
                        f"cleanup_cloud_file_delete_{file_id}",
                    )
            if any((drive_file_id, video_file_id)) and user_one.csrf:
                files_readback = _must(
                    user_one.request("GET", "/api/cloud-drive/files"),
                    "cleanup_cloud_files_readback",
                )
                remaining = {
                    _cloud_drive_file_id(row)
                    for row in (files_readback.get("files") or [])
                    if isinstance(row, Mapping)
                }
                leaked = {item for item in (drive_file_id, video_file_id) if item and item in remaining}
                if leaked:
                    raise RuntimeError(f"cleanup_cloud_files_present:{sorted(leaked)}")
        except Exception as exc:
            cleanup_errors.append(f"storage:{exc.__class__.__name__}:{exc}")
        try:
            if before_settings and root.csrf:
                # A fresh isolated load runtime normally starts with the audit
                # chain disabled.  Restoring that exact snapshot is subject to
                # the product's dangerous-change confirmation contract.
                restore_payload = _settings_restore_payload(before_settings)
                root.refresh_csrf()
                _must(
                    root.request("PUT", "/api/admin/settings", json_body=restore_payload),
                    "cleanup_settings_restore",
                )
                settings_readback = _must(
                    root.request("GET", "/api/admin/settings"),
                    "cleanup_settings_readback",
                )
                current = (
                    settings_readback.get("settings")
                    if isinstance(settings_readback.get("settings"), Mapping)
                    else {}
                )
                if any(current.get(key) != value for key, value in before_settings.items()):
                    raise RuntimeError("cleanup_settings_restore_mismatch")
                result.setdefault("cleanup", {})["settings_restored"] = True
        except Exception as exc:
            cleanup_errors.append(f"settings:{exc.__class__.__name__}:{exc}")
        try:
            if restart_request_created and not result.get("ok"):
                if restart_request_file.is_symlink():
                    restart_request_file.unlink(missing_ok=True)
                    raise RuntimeError("failed_probe_restart_receipt_was_symlink")
                if restart_request_file.exists():
                    restart_request_file.unlink()
                    directory_fd = os.open(
                        restart_request_file.parent,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                if restart_request_file.exists() or restart_request_file.is_symlink():
                    raise RuntimeError("failed_probe_restart_receipt_not_removed")
                result.setdefault("cleanup", {})["failed_restart_receipt_removed"] = True
        except Exception as exc:
            cleanup_errors.append(f"restart_receipt:{exc.__class__.__name__}:{exc}")
        if cleanup_errors:
            result.setdefault("cleanup", {}).setdefault("errors", []).extend(cleanup_errors)
            result["ok"] = False
            result["errors"].extend(f"cleanup:{item}" for item in cleanup_errors)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"ok": result["ok"], "out": str(out_path), "errors": result["errors"]}, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
