#!/usr/bin/env python3
"""Production-equivalent security sentinel for the isolated campaign target."""

from __future__ import annotations

import json
import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests
import urllib3

from scripts.testing.audit_evidence_triad import (
    ARCHIVE_SCHEMA_VERSION as AUDIT_EVIDENCE_ARCHIVE_SCHEMA_VERSION,
    SCHEMA_VERSION as AUDIT_EVIDENCE_SCHEMA_VERSION,
    AuditEvidencePaths,
    capture_audit_evidence,
    create_audit_evidence_archive,
    validate_audit_evidence_archive,
    validate_audit_evidence_receipt,
)


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
    audit_evidence_output_dir: Path | None = None
    audit_evidence_target: str = "security_sentinel"


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

    def _online_audit_evidence(self) -> tuple[dict[str, Any], dict[str, Any]]:
        output_dir = self.config.audit_evidence_output_dir
        if output_dir is None:
            raise RuntimeError("online audit evidence output directory is not configured")
        try:
            capture_audit_evidence(
                paths=AuditEvidencePaths.for_runtime(self.config.runtime_root),
                output_dir=output_dir,
                target=self.config.audit_evidence_target,
                mode="online",
            )
            receipt_path = Path(output_dir) / "receipt.json"
            receipt_bytes = receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes)
            if not isinstance(receipt, Mapping):
                raise RuntimeError("audit evidence receipt root is not an object")
            validation = validate_audit_evidence_receipt(
                receipt,
                required_mode="online",
                required_target=self.config.audit_evidence_target,
                artifact_root=Path(output_dir),
            )
            archive_path = Path(output_dir).with_name(
                f"{Path(output_dir).name}.tar"
            )
            archive = create_audit_evidence_archive(
                output_dir=Path(output_dir),
                archive_path=archive_path,
            )
            archive_validation = validate_audit_evidence_archive(
                archive_path,
                required_mode="online",
                required_target=self.config.audit_evidence_target,
                expected_sha256=str(archive.get("sha256") or ""),
                expected_size=int(archive.get("size") or 0),
            )
            reference = {
                "schema_version": "hackme.audit-evidence-triad-reference/v1",
                "receipt_schema_version": AUDIT_EVIDENCE_SCHEMA_VERSION,
                "mode": "online",
                "target": self.config.audit_evidence_target,
                "receipt_path": str(receipt_path.resolve(strict=True)),
                "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "receipt_size_bytes": len(receipt_bytes),
                "receipt": dict(receipt),
                "validation": validation,
                "archive_schema_version": AUDIT_EVIDENCE_ARCHIVE_SCHEMA_VERSION,
                "archive_path": str(archive_path.resolve(strict=True)),
                "archive_sha256": archive.get("sha256"),
                "archive_size_bytes": archive.get("size"),
                "archive_validation": archive_validation,
            }
            check = {
                "name": "audit_evidence_triad_online",
                "ok": validation.get("ok") is True,
                "status": 200,
                "elapsed_ms": 0.0,
                "error": "",
                "detail": {
                    "receipt_schema_version": AUDIT_EVIDENCE_SCHEMA_VERSION,
                    "mode": "online",
                    "target": self.config.audit_evidence_target,
                    "receipt_sha256": reference["receipt_sha256"],
                    "receipt_size_bytes": reference["receipt_size_bytes"],
                    "artifact_files_verified": validation.get("artifact_files_verified"),
                    "validation_classification": validation.get("classification"),
                    "validation_errors": validation.get("errors"),
                    "archive_schema_version": AUDIT_EVIDENCE_ARCHIVE_SCHEMA_VERSION,
                    "archive_sha256": archive.get("sha256"),
                    "archive_size_bytes": archive.get("size"),
                    "archive_validation_classification": archive_validation.get(
                        "classification"
                    ),
                    "archive_validation_errors": archive_validation.get("errors"),
                },
            }
            if archive_validation.get("ok") is not True:
                check["ok"] = False
            return reference, check
        except Exception as exc:
            validation = {
                "schema_version": "hackme.audit-evidence-triad-validation/v1",
                "ok": False,
                "classification": "FAIL_HARNESS",
                "errors": [f"capture_failed:{exc.__class__.__name__}"],
                "artifact_files_verified": False,
            }
            return {
                "schema_version": "hackme.audit-evidence-triad-reference/v1",
                "receipt_schema_version": AUDIT_EVIDENCE_SCHEMA_VERSION,
                "mode": "online",
                "target": self.config.audit_evidence_target,
                "receipt_path": "",
                "receipt_sha256": "",
                "receipt_size_bytes": 0,
                "receipt": None,
                "validation": validation,
                "archive_schema_version": AUDIT_EVIDENCE_ARCHIVE_SCHEMA_VERSION,
                "archive_path": "",
                "archive_sha256": "",
                "archive_size_bytes": 0,
                "archive_validation": {
                    "schema_version": "hackme.audit-evidence-triad-archive-validation/v1",
                    "ok": False,
                    "classification": "FAIL_HARNESS",
                    "errors": [f"capture_failed:{exc.__class__.__name__}"],
                },
            }, {
                "name": "audit_evidence_triad_online",
                "ok": False,
                "status": 0,
                "elapsed_ms": 0.0,
                "error": f"{exc.__class__.__name__}: {exc}",
                "detail": {
                    "receipt_schema_version": AUDIT_EVIDENCE_SCHEMA_VERSION,
                    "mode": "online",
                    "target": self.config.audit_evidence_target,
                    "receipt_sha256": "",
                    "receipt_size_bytes": 0,
                    "artifact_files_verified": False,
                    "validation_classification": "FAIL_HARNESS",
                    "validation_errors": validation["errors"],
                    "archive_schema_version": AUDIT_EVIDENCE_ARCHIVE_SCHEMA_VERSION,
                    "archive_sha256": "",
                    "archive_size_bytes": 0,
                    "archive_validation_classification": "FAIL_HARNESS",
                    "archive_validation_errors": validation["errors"],
                },
            }

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
        security_body = security_center.get("body")
        security_body = security_body if isinstance(security_body, Mapping) else {}
        security_payload_raw = security_body.get("security_center")
        security_payload = security_payload_raw if isinstance(security_payload_raw, Mapping) else {}
        settings_raw = security_payload.get("settings")
        settings = settings_raw if isinstance(settings_raw, Mapping) else {}
        required_settings = {
            "audit_chain_enabled": True,
            "feature_audit_log_enabled": True,
            "login_violation_enabled": True,
            "rate_limit_violation_enabled": True,
        }
        settings_ok = all(settings.get(name) is expected for name, expected in required_settings.items())
        audit_integrity_raw = security_payload.get("audit_integrity")
        audit_integrity = audit_integrity_raw if isinstance(audit_integrity_raw, Mapping) else {}
        audit_integrity_required_fields = ("enabled", "ok", "broken_at")
        audit_integrity_missing_fields = [
            field for field in audit_integrity_required_fields if field not in audit_integrity
        ]
        audit_integrity_valid = bool(
            isinstance(audit_integrity_raw, Mapping)
            and not audit_integrity_missing_fields
            and audit_integrity.get("enabled") is True
            and audit_integrity.get("ok") is True
            and audit_integrity.get("broken_at") is None
        )
        checks.append({
            **self._check("production_security_controls", security_center, expected_statuses={200}),
            "ok": (
                int(security_center.get("status") or 0) == 200
                and settings_ok
                and audit_integrity_valid
            ),
            "detail": {
                "required_settings": {name: settings.get(name) for name in required_settings},
                "audit_integrity_required_fields": list(audit_integrity_required_fields),
                "audit_integrity_missing_fields": audit_integrity_missing_fields,
                "audit_integrity_enabled": audit_integrity.get("enabled"),
                "audit_integrity_ok": audit_integrity.get("ok"),
                "audit_integrity_broken_at": audit_integrity.get("broken_at"),
                "audit_integrity_valid": audit_integrity_valid,
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

        audit_evidence: dict[str, Any] | None = None
        if self.config.audit_evidence_output_dir is not None:
            audit_evidence, audit_check = self._online_audit_evidence()
            checks.append(audit_check)

        failed_checks = [item["name"] for item in checks if not item.get("ok")]
        classification = "PASS"
        if failed_checks:
            validation = (
                audit_evidence.get("validation")
                if isinstance(audit_evidence, Mapping)
                else None
            )
            archive_validation = (
                audit_evidence.get("archive_validation")
                if isinstance(audit_evidence, Mapping)
                else None
            )
            classification = (
                str(archive_validation.get("classification"))
                if isinstance(archive_validation, Mapping)
                and archive_validation.get("ok") is not True
                else
                str(validation.get("classification"))
                if isinstance(validation, Mapping) and validation.get("ok") is not True
                else "FAIL_PRODUCT"
            )

        return {
            "schema_version": SECURITY_SENTINEL_SCHEMA_VERSION,
            "checked_at": utc_now(),
            "target": self.config.base_url.rstrip("/"),
            "runtime_root": str(self.config.runtime_root),
            "checks": checks,
            "failed_checks": failed_checks,
            "audit_evidence": audit_evidence,
            "classification": classification,
            "ok": bool(checks) and not failed_checks,
        }


def atomic_write_result(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
