from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.testing.campaign_security_sentinel import (
    ProductionSecuritySentinel,
    SecuritySentinelConfig,
)
from services.server.database import get_audit_db
from services.system import audit as audit_service


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


_DEFAULT_AUDIT_INTEGRITY = object()


class FakeSession:
    verify = False

    def __init__(
        self,
        *,
        production: bool = True,
        audit_integrity: Any = _DEFAULT_AUDIT_INTEGRITY,
    ):
        self.role = "anonymous"
        self.production = production
        self.csrf_token = ""
        self.audit_integrity = (
            {
                "enabled": True,
                "ok": True,
                "broken_at": None,
                "details": "integrity OK",
            }
            if audit_integrity is _DEFAULT_AUDIT_INTEGRITY
            else audit_integrity
        )

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
                "audit_integrity": self.audit_integrity,
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


def test_formal_security_sentinel_produces_and_verifies_online_triad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = config(tmp_path)
    runtime = base.runtime_root
    for directory in (
        runtime / "database",
        runtime / "logs",
        runtime / "anchors",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    seed = "5" * 48
    key = bytes.fromhex("6a" * 32)
    (runtime / ".chain_seed").write_text(seed, encoding="utf-8")
    (runtime / ".integrity_key").write_bytes(key)
    database = runtime / "database" / "audit.db"
    audit_service.configure_audit_service(
        get_db=lambda: get_audit_db(str(database)),
        chain_seed=seed,
        integrity_key=key,
        audit_log_path=str(runtime / "logs" / "audit.log"),
        audit_anchor_path=str(runtime / "anchors" / "audit_head.jsonl"),
        audit_anchor_latest_path=str(runtime / "anchors" / "audit_head_latest.json"),
        audit_anchor_interval_seconds=60,
    )
    monkeypatch.setattr(audit_service, "_last_audit_anchor_at", 0.0)
    audit_service.audit(
        "security_sentinel_fixture",
        "127.0.0.1",
        user="root",
        success=True,
        ua="pytest",
        detail="formal-online-triad",
    )
    output_dir = tmp_path / "online-triad"
    formal = replace(base, audit_evidence_output_dir=output_dir)

    result = ProductionSecuritySentinel(
        formal,
        session_factory=FakeSession,
    ).run_once()

    assert result["ok"] is True
    assert result["classification"] == "PASS"
    assert result["audit_evidence"]["validation"]["ok"] is True
    assert result["audit_evidence"]["validation"]["artifact_files_verified"] is True
    assert (output_dir / "receipt.json").is_file()
    check = next(
        row for row in result["checks"]
        if row["name"] == "audit_evidence_triad_online"
    )
    assert check["ok"] is True


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


@pytest.mark.parametrize(
    ("audit_integrity", "expected_missing_fields"),
    [
        pytest.param(None, ["enabled", "ok", "broken_at"], id="audit-integrity-null"),
        pytest.param({}, ["enabled", "ok", "broken_at"], id="all-required-fields-missing"),
        pytest.param(
            {"ok": True, "broken_at": None},
            ["enabled"],
            id="enabled-missing",
        ),
        pytest.param(
            {"enabled": True, "broken_at": None},
            ["ok"],
            id="ok-missing",
        ),
        pytest.param(
            {"enabled": True, "ok": True},
            ["broken_at"],
            id="broken-at-missing",
        ),
        pytest.param(
            {"enabled": None, "ok": True, "broken_at": None},
            [],
            id="enabled-none",
        ),
        pytest.param(
            {"enabled": False, "ok": True, "broken_at": None},
            [],
            id="enabled-false",
        ),
        pytest.param(
            {"enabled": True, "ok": None, "broken_at": None},
            [],
            id="ok-none",
        ),
        pytest.param(
            {"enabled": True, "ok": False, "broken_at": None},
            [],
            id="ok-false",
        ),
        pytest.param(
            {"enabled": True, "ok": True, "broken_at": 7},
            [],
            id="broken-at-present",
        ),
    ],
)
def test_production_security_controls_fail_closed_on_invalid_audit_integrity(
    tmp_path: Path,
    audit_integrity: Any,
    expected_missing_fields: list[str],
) -> None:
    result = ProductionSecuritySentinel(
        config(tmp_path),
        session_factory=lambda: FakeSession(audit_integrity=audit_integrity),
    ).run_once()

    assert result["ok"] is False
    assert "production_security_controls" in result["failed_checks"]
    check = next(
        item for item in result["checks"]
        if item["name"] == "production_security_controls"
    )
    assert check["ok"] is False
    assert check["detail"]["audit_integrity_valid"] is False
    assert check["detail"]["audit_integrity_missing_fields"] == expected_missing_fields
