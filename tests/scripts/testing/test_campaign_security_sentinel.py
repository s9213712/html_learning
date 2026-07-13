from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.testing.campaign_security_sentinel import (
    ProductionSecuritySentinel,
    SecuritySentinelConfig,
)


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


class FakeSession:
    verify = False

    def __init__(self, *, production: bool = True):
        self.role = "anonymous"
        self.production = production
        self.csrf_token = ""

    def request(self, method: str, url: str, *, headers=None, json=None, **_kwargs: Any):
        path = "/" + url.split("/", 3)[3]
        headers = headers or {}
        if path == "/api/version":
            return FakeResponse(200, {"ok": True})
        if path == "/api/csrf-token":
            self.csrf_token = f"{self.role}-csrf"
            return FakeResponse(200, {"ok": True, "csrf_token": self.csrf_token})
        if path == "/api/login":
            if headers.get("X-CSRF-Token") != self.csrf_token:
                return FakeResponse(403, {"ok": False})
            username = (json or {}).get("username")
            self.role = {"root": "root", "admin": "manager", "test": "user"}.get(username, "anonymous")
            return FakeResponse(200, {"ok": True}) if self.role != "anonymous" else FakeResponse(401, {"ok": False})
        if path == "/api/me":
            return FakeResponse(200, {"ok": True}) if self.role != "anonymous" else FakeResponse(401, {"ok": False})
        if path == "/api/root/server-mode":
            if self.role == "anonymous":
                return FakeResponse(401, {"ok": False})
            if self.role != "root":
                return FakeResponse(403, {"ok": False})
            return FakeResponse(200, {"ok": True, "mode": "production" if self.production else "dev_ready"})
        if path == "/api/admin/system-reset":
            if headers.get("X-CSRF-Token") != f"{self.role}-csrf":
                return FakeResponse(403, {"ok": False})
            return FakeResponse(400, {"ok": False, "msg": "confirm required"})
        if path == "/api/admin/security-center":
            return FakeResponse(200, {"ok": True, "security_center": {
                "mode": "production",
                "settings": {
                    "audit_chain_enabled": True,
                    "feature_audit_log_enabled": True,
                    "login_violation_enabled": True,
                    "rate_limit_violation_enabled": True,
                },
                "audit_integrity": {"ok": True},
            }})
        if path == "/api/root/server-mode/logs/verify":
            return FakeResponse(200, {"ok": True})
        raise AssertionError((method, path, self.role))


def config(tmp_path: Path, *, security: str = "on", mode: str = "production", workers: str = "2") -> SecuritySentinelConfig:
    runtime = tmp_path / "security"
    load = tmp_path / "primary"
    runtime.mkdir()
    load.mkdir()
    return SecuritySentinelConfig(
        base_url="https://127.0.0.1:5003",
        runtime_root=runtime,
        load_target_runtime_root=load,
        credentials={"root": "root-secret", "manager": "manager-secret", "user": "user-secret"},
        launcher_command=(
            "test_for_develop.sh",
            "--security", security,
            "--server-mode", mode,
            "--gunicorn-workers", workers,
        ),
        cross_worker_requests=4,
    )


def test_production_security_sentinel_verifies_real_boundaries(tmp_path: Path) -> None:
    result = ProductionSecuritySentinel(config(tmp_path), session_factory=FakeSession).run_once()

    assert result["ok"] is True
    assert result["failed_checks"] == []
    names = {item["name"] for item in result["checks"]}
    assert {
        "production_launcher_contract",
        "login_missing_csrf_denied",
        "authenticated_missing_csrf_denied",
        "manager_root_boundary_denied",
        "user_root_boundary_denied",
        "dangerous_confirmation_required",
        "production_security_controls",
        "audit_log_chain",
        "cross_worker_session_consistency",
    } <= names


def test_security_off_launcher_can_never_pass_sentinel(tmp_path: Path) -> None:
    result = ProductionSecuritySentinel(config(tmp_path, security="off"), session_factory=FakeSession).run_once()

    assert result["ok"] is False
    assert "production_launcher_contract" in result["failed_checks"]


def test_nonproduction_runtime_can_never_pass_sentinel(tmp_path: Path) -> None:
    result = ProductionSecuritySentinel(
        config(tmp_path, mode="dev_ready"),
        session_factory=lambda: FakeSession(production=False),
    ).run_once()

    assert result["ok"] is False
    assert "production_launcher_contract" in result["failed_checks"]
    assert "production_mode_active" in result["failed_checks"]


def test_single_worker_launcher_is_rejected_for_cross_worker_claim(tmp_path: Path) -> None:
    result = ProductionSecuritySentinel(config(tmp_path, workers="1"), session_factory=FakeSession).run_once()
    assert result["ok"] is False
    contract = next(item for item in result["checks"] if item["name"] == "production_launcher_contract")
    assert contract["detail"]["gunicorn_workers"] == 1
