#!/usr/bin/env python3
"""Production-equivalent security sentinel for the isolated campaign target."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests
import urllib3


SECURITY_SENTINEL_SCHEMA_VERSION = "hackme.production-security-sentinel.v1"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SecuritySentinelConfig:
    base_url: str
    runtime_root: Path
    load_target_runtime_root: Path
    credentials: Mapping[str, str]
    launcher_command: tuple[str, ...]
    timeout_seconds: float = 15.0
    cross_worker_requests: int = 12


class _Client:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float,
        session_factory: Callable[[], requests.Session],
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session_factory()
        self.session.verify = False
        self.csrf = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        include_csrf: bool = True,
    ) -> dict[str, Any]:
        # The sentinel is deliberately low-frequency and boundary-focused.
        # Use a fresh TLS connection for each assertion so an idle pooled
        # socket cannot turn a server-generated 401/403 into a client-side
        # RemoteDisconnected result.  Load/keep-alive behaviour is measured by
        # the separate primary target.
        headers: dict[str, str] = {"Connection": "close"}
        if include_csrf and self.csrf and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers["X-CSRF-Token"] = self.csrf
        started = time.monotonic()
        try:
            response = self.session.request(
                method.upper(),
                f"{self.base_url}{path}",
                headers=headers,
                json=dict(json_body) if json_body is not None else None,
                timeout=self.timeout,
            )
            try:
                body = response.json() if response.content else {}
            except Exception:
                body = {}
            return {
                "status": int(response.status_code),
                "ok": 200 <= int(response.status_code) < 300 and isinstance(body, dict) and body.get("ok") is not False,
                "body": body if isinstance(body, dict) else {},
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            }
        except Exception as exc:
            return {
                "status": 0,
                "ok": False,
                "body": {},
                "error": f"{exc.__class__.__name__}: {exc}",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            }

    def bootstrap_csrf(self) -> dict[str, Any]:
        response = self.request("GET", "/api/csrf-token")
        self.csrf = str((response.get("body") or {}).get("csrf_token") or "")
        return response

    def login(self, username: str, password: str) -> dict[str, Any]:
        csrf = self.bootstrap_csrf()
        if not csrf.get("ok") or not self.csrf:
            return {"ok": False, "status": int(csrf.get("status") or 0), "error": "csrf_bootstrap_failed"}
        login = self.request("POST", "/api/login", json_body={"username": username, "password": password})
        if not login.get("ok"):
            return login
        # Successful login invalidates the public token and rotates a
        # user-owned CSRF cookie.  Refresh the mirrored header token before
        # testing any authenticated write boundary.
        rotated = self.bootstrap_csrf()
        if not rotated.get("ok") or not self.csrf:
            return {"ok": False, "status": int(rotated.get("status") or 0), "error": "csrf_rotation_failed"}
        return login


class ProductionSecuritySentinel:
    """Checks production mode, CSRF, RBAC, confirmation, audit, and sessions."""

    def __init__(
        self,
        config: SecuritySentinelConfig,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ) -> None:
        self.config = config
        self.session_factory = session_factory

    @staticmethod
    def _flag_value(command: tuple[str, ...], flag: str) -> str:
        try:
            index = command.index(flag)
            return command[index + 1]
        except (ValueError, IndexError):
            return ""

    def _launcher_contract(self) -> dict[str, Any]:
        command = tuple(self.config.launcher_command)
        security = self._flag_value(command, "--security")
        server_mode = self._flag_value(command, "--server-mode")
        workers_raw = self._flag_value(command, "--gunicorn-workers")
        try:
            workers = int(workers_raw)
        except ValueError:
            workers = 0
        isolated = self.config.runtime_root.resolve() != self.config.load_target_runtime_root.resolve()
        return {
            "name": "production_launcher_contract",
            "ok": security == "on" and server_mode == "production" and workers >= 2 and isolated,
            "status": 200,
            "detail": {
                "security": security,
                "server_mode": server_mode,
                "gunicorn_workers": workers,
                "isolated_runtime": isolated,
            },
        }

    @staticmethod
    def _check(name: str, response: Mapping[str, Any], *, expected_statuses: set[int], detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        status = int(response.get("status") or 0)
        return {
            "name": name,
            "ok": status in expected_statuses,
            "status": status,
            "elapsed_ms": float(response.get("elapsed_ms") or 0.0),
            "error": str(response.get("error") or ""),
            "detail": dict(detail or {}),
        }

    def _client(self) -> _Client:
        return _Client(
            self.config.base_url,
            timeout=self.config.timeout_seconds,
            session_factory=self.session_factory,
        )

    def run_once(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = [self._launcher_contract()]
        anonymous = self._client()
        version = anonymous.request("GET", "/api/version")
        checks.append(self._check("transport", version, expected_statuses={200}))
        protected = anonymous.request("GET", "/api/root/server-mode")
        checks.append(self._check("anonymous_root_denied", protected, expected_statuses={401, 403}))

        anonymous.bootstrap_csrf()
        missing_login_csrf = anonymous.request(
            "POST",
            "/api/login",
            json_body={"username": "root", "password": self.config.credentials.get("root", "")},
            include_csrf=False,
        )
        checks.append(self._check("login_missing_csrf_denied", missing_login_csrf, expected_statuses={400, 403, 419}))

        clients: dict[str, _Client] = {}
        identities = {"root": "root", "manager": "admin", "user": "test"}
        for role, username in identities.items():
            client = self._client()
            login = client.login(username, str(self.config.credentials.get(role) or ""))
            checks.append(self._check(f"{role}_login", login, expected_statuses={200}))
            clients[role] = client

        root_mode = clients["root"].request("GET", "/api/root/server-mode")
        mode_body = root_mode.get("body") or {}
        mode_value = mode_body.get("mode")
        if isinstance(mode_value, Mapping):
            mode_value = mode_value.get("current_mode")
        checks.append({
            **self._check("production_mode_active", root_mode, expected_statuses={200}),
            "ok": int(root_mode.get("status") or 0) == 200 and mode_value == "production",
            "detail": {"mode": mode_value},
        })
        for role in ("manager", "user"):
            response = clients[role].request("GET", "/api/root/server-mode")
            checks.append(self._check(f"{role}_root_boundary_denied", response, expected_statuses={403}))

        missing_csrf = clients["root"].request(
            "POST",
            "/api/admin/system-reset",
            json_body={},
            include_csrf=False,
        )
        checks.append(self._check("authenticated_missing_csrf_denied", missing_csrf, expected_statuses={400, 403, 419}))
        # With the rotated authenticated CSRF token, the same request must now
        # reach the action-level confirmation guard and remain a no-op.
        wrong_confirmation = clients["root"].request("POST", "/api/admin/system-reset", json_body={})
        checks.append(self._check("dangerous_confirmation_required", wrong_confirmation, expected_statuses={400}))

        security_center = clients["root"].request("GET", "/api/admin/security-center")
        security_payload = (security_center.get("body") or {}).get("security_center") or {}
        settings = security_payload.get("settings") or {}
        required_settings = {
            "audit_chain_enabled": True,
            "feature_audit_log_enabled": True,
            "login_violation_enabled": True,
            "rate_limit_violation_enabled": True,
        }
        settings_ok = all(settings.get(name) is expected for name, expected in required_settings.items())
        audit_integrity = security_payload.get("audit_integrity") or {}
        checks.append({
            **self._check("production_security_controls", security_center, expected_statuses={200}),
            "ok": int(security_center.get("status") or 0) == 200 and settings_ok and audit_integrity.get("ok") is not False,
            "detail": {
                "required_settings": {name: settings.get(name) for name in required_settings},
                "audit_integrity_ok": audit_integrity.get("ok"),
                "reported_mode": security_payload.get("mode"),
            },
        })
        log_chain = clients["root"].request("GET", "/api/root/server-mode/logs/verify")
        checks.append({
            **self._check("audit_log_chain", log_chain, expected_statuses={200}),
            "ok": int(log_chain.get("status") or 0) == 200 and (log_chain.get("body") or {}).get("ok") is not False,
        })

        session_statuses: list[int] = []
        for _ in range(max(2, int(self.config.cross_worker_requests))):
            response = clients["root"].request("GET", "/api/me")
            session_statuses.append(int(response.get("status") or 0))
        checks.append({
            "name": "cross_worker_session_consistency",
            "ok": bool(session_statuses) and all(status == 200 for status in session_statuses),
            "status": 200 if session_statuses and all(status == 200 for status in session_statuses) else 0,
            "elapsed_ms": 0.0,
            "error": "",
            "detail": {"requests": len(session_statuses), "statuses": session_statuses},
        })

        return {
            "schema_version": SECURITY_SENTINEL_SCHEMA_VERSION,
            "checked_at": utc_now(),
            "target": self.config.base_url.rstrip("/"),
            "runtime_root": str(self.config.runtime_root),
            "checks": checks,
            "failed_checks": [item["name"] for item in checks if not item.get("ok")],
            "ok": bool(checks) and all(item.get("ok") for item in checks),
        }


def atomic_write_result(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
