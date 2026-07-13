#!/usr/bin/env python3
"""Layered application readiness checks with async terminal verification."""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests
import urllib3


READINESS_SCHEMA_VERSION = "hackme.layered-readiness.v1"
SUCCESS_TERMINAL_STATES = frozenset({"success", "succeeded", "completed", "complete", "done"})
FAILURE_TERMINAL_STATES = frozenset({"failed", "failure", "cancelled", "canceled", "expired", "timed_out", "timeout"})

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _status(value: Any) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class ReadinessConfig:
    base_url: str
    username: str
    password: str
    runtime_root: Path
    request_timeout_seconds: float = 15.0
    async_timeout_seconds: float = 120.0
    async_poll_seconds: float = 1.0


class LayeredReadinessProbe:
    """Proves transport, dependencies, workers, and domain invariants."""

    def __init__(
        self,
        config: ReadinessConfig,
        *,
        session: requests.Session | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.base_url = str(config.base_url).rstrip("/")
        self.session = session or requests.Session()
        self.session.verify = False
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.csrf = ""

    def _request(self, method: str, path: str, *, json_body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if method.upper() not in {"GET", "HEAD", "OPTIONS"} and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        started = self.monotonic()
        try:
            response = self.session.request(
                method.upper(),
                f"{self.base_url}{path}",
                json=dict(json_body) if json_body is not None else None,
                headers=headers,
                timeout=self.config.request_timeout_seconds,
            )
            try:
                body = response.json() if response.content else {}
            except Exception:
                body = {}
            return {
                "status": int(response.status_code),
                "elapsed_ms": round((self.monotonic() - started) * 1000, 3),
                "body": body if isinstance(body, dict) else {},
                "ok": 200 <= int(response.status_code) < 300 and isinstance(body, dict) and body.get("ok") is not False,
            }
        except Exception as exc:
            return {
                "status": 0,
                "elapsed_ms": round((self.monotonic() - started) * 1000, 3),
                "body": {},
                "ok": False,
                "error": f"{exc.__class__.__name__}: {exc}",
            }

    @staticmethod
    def _check(name: str, response: Mapping[str, Any], *, detail: Any = None, ok: bool | None = None) -> dict[str, Any]:
        return {
            "name": name,
            "ok": bool(response.get("ok") if ok is None else ok),
            "status": int(response.get("status") or 0),
            "elapsed_ms": float(response.get("elapsed_ms") or 0.0),
            "error": str(response.get("error") or ""),
            "detail": detail,
        }

    def _login(self) -> dict[str, Any]:
        csrf = self._request("GET", "/api/csrf-token")
        body = csrf.get("body") or {}
        self.csrf = str(body.get("csrf_token") or "")
        if not csrf.get("ok") or not self.csrf:
            return self._check("csrf_bootstrap", csrf, ok=False, detail={"csrf_present": False})
        login = self._request(
            "POST",
            "/api/login",
            json_body={"username": self.config.username, "password": self.config.password},
        )
        return self._check(
            "root_login",
            login,
            ok=bool(login.get("ok")),
            detail={"csrf_present": True, "authenticated": bool(login.get("ok"))},
        )

    def _database_write_check(self) -> dict[str, Any]:
        candidates = [
            self.config.runtime_root / "database" / "database.db",
            self.config.runtime_root / "database.db",
        ]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        started = self.monotonic()
        if path is None:
            return {"name": "database_writable", "ok": False, "status": 0, "elapsed_ms": 0.0, "error": "database_file_missing", "detail": None}
        try:
            connection = sqlite3.connect(str(path), timeout=5)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TEMP TABLE campaign_readiness_probe(value INTEGER)")
                connection.execute("INSERT INTO campaign_readiness_probe(value) VALUES (1)")
                row = connection.execute("SELECT value FROM campaign_readiness_probe").fetchone()
                connection.rollback()
            finally:
                connection.close()
            return {
                "name": "database_writable",
                "ok": bool(row and row[0] == 1),
                "status": 200,
                "elapsed_ms": round((self.monotonic() - started) * 1000, 3),
                "error": "",
                "detail": {"database": path.name, "transaction_rolled_back": True},
            }
        except Exception as exc:
            return {
                "name": "database_writable",
                "ok": False,
                "status": 0,
                "elapsed_ms": round((self.monotonic() - started) * 1000, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
                "detail": {"database": path.name},
            }

    def _storage_write_check(self) -> dict[str, Any]:
        storage = self.config.runtime_root / "storage"
        started = self.monotonic()
        marker = storage / f".campaign-readiness-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        try:
            storage.mkdir(parents=True, exist_ok=True)
            with marker.open("wb") as handle:
                handle.write(b"campaign-readiness\n")
                handle.flush()
                os.fsync(handle.fileno())
            readable = marker.read_bytes() == b"campaign-readiness\n"
            marker.unlink()
            return {
                "name": "storage_writable",
                "ok": readable and not marker.exists(),
                "status": 200,
                "elapsed_ms": round((self.monotonic() - started) * 1000, 3),
                "error": "",
                "detail": {"round_trip": readable, "cleanup": not marker.exists()},
            }
        except Exception as exc:
            try:
                marker.unlink(missing_ok=True)
            except Exception:
                pass
            return {
                "name": "storage_writable",
                "ok": False,
                "status": 0,
                "elapsed_ms": round((self.monotonic() - started) * 1000, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
                "detail": None,
            }

    def _terminal_check(self, name: str, start_path: str) -> dict[str, Any]:
        started_at = self.monotonic()
        start = self._request("GET", start_path)
        body = start.get("body") or {}
        if int(start.get("status") or 0) == 200:
            return self._check(name, start, ok=bool(start.get("ok")), detail={"terminal": True, "async": False})
        if int(start.get("status") or 0) != 202 or not start.get("ok"):
            return self._check(name, start, ok=False, detail={"terminal": False, "async": False})
        status_url = str(body.get("status_url") or "")
        latest_url = str(body.get("latest_snapshot_url") or "")
        job_uuid = str(body.get("job_uuid") or body.get("job_id") or "")
        if not status_url or not latest_url or not job_uuid:
            return self._check(name, start, ok=False, detail={"terminal": False, "async": True, "contract_error": "job_identity_or_urls_missing"})
        deadline = self.monotonic() + self.config.async_timeout_seconds
        polls = 0
        terminal: dict[str, Any] = {}
        while self.monotonic() < deadline:
            polls += 1
            terminal = self._request("GET", status_url)
            job = (terminal.get("body") or {}).get("job") or {}
            job_status = _status(job.get("status"))
            if job_status in SUCCESS_TERMINAL_STATES:
                snapshot = self._request("GET", latest_url)
                ok = bool(snapshot.get("ok") and (snapshot.get("body") or {}).get("ok") is not False)
                return {
                    "name": name,
                    "ok": ok,
                    "status": int(snapshot.get("status") or 0),
                    "elapsed_ms": round((self.monotonic() - started_at) * 1000, 3),
                    "error": str(snapshot.get("error") or ""),
                    "detail": {
                        "async": True,
                        "job_uuid": job_uuid,
                        "terminal_status": job_status,
                        "polls": polls,
                        "snapshot_verified": ok,
                    },
                }
            if job_status in FAILURE_TERMINAL_STATES:
                return {
                    "name": name,
                    "ok": False,
                    "status": int(terminal.get("status") or 0),
                    "elapsed_ms": round((self.monotonic() - started_at) * 1000, 3),
                    "error": f"terminal_job_{job_status}",
                    "detail": {"async": True, "job_uuid": job_uuid, "terminal_status": job_status, "polls": polls},
                }
            self.sleeper(self.config.async_poll_seconds)
        return {
            "name": name,
            "ok": False,
            "status": int(terminal.get("status") or 0),
            "elapsed_ms": round((self.monotonic() - started_at) * 1000, 3),
            "error": "async_terminal_timeout",
            "detail": {"async": True, "job_uuid": job_uuid, "terminal_status": "unknown", "polls": polls},
        }

    @staticmethod
    def _layer(checks: list[dict[str, Any]]) -> dict[str, Any]:
        return {"ok": bool(checks) and all(item.get("ok") for item in checks), "checks": checks}

    def probe_once(self) -> dict[str, Any]:
        started = self.monotonic()
        version = self._request("GET", "/api/version")
        transport = self._layer([
            self._check(
                "transport_alive",
                version,
                ok=bool(version.get("ok") and int(version.get("status") or 0) == 200),
                detail={"version_present": bool((version.get("body") or {}).get("version"))},
            )
        ])
        if not transport["ok"]:
            layers = {
                "transport_alive": transport,
                "application_ready": {"ok": False, "checks": [], "blocked_by": "transport_alive"},
                "dependencies_ready": {"ok": False, "checks": [], "blocked_by": "transport_alive"},
                "background_workers_ready": {"ok": False, "checks": [], "blocked_by": "transport_alive"},
                "domain_invariants_ready": {"ok": False, "checks": [], "blocked_by": "transport_alive"},
            }
            return self._result(layers, started)

        login = self._login()
        me_response = self._request("GET", "/api/me") if login["ok"] else {"ok": False, "status": 0}
        application = self._layer([
            login,
            self._check("authenticated_application", me_response, ok=bool(me_response.get("ok"))),
        ])
        if not application["ok"]:
            layers = {
                "transport_alive": transport,
                "application_ready": application,
                "dependencies_ready": {"ok": False, "checks": [], "blocked_by": "application_ready"},
                "background_workers_ready": {"ok": False, "checks": [], "blocked_by": "application_ready"},
                "domain_invariants_ready": {"ok": False, "checks": [], "blocked_by": "application_ready"},
            }
            return self._result(layers, started)

        readiness = self._request("GET", "/api/admin/health/readiness")
        readiness_body = (readiness.get("body") or {}).get("readiness") or {}
        readiness_checks = readiness_body.get("checks") or []
        critical_failed = [row.get("name") for row in readiness_checks if not row.get("ok") and row.get("severity") == "critical"]
        degraded_failed = [row.get("name") for row in readiness_checks if not row.get("ok") and row.get("severity") != "critical"]
        storage_api = self._request("GET", "/api/admin/storage/summary")
        jobs = self._request("GET", "/api/admin/jobs?limit=1")
        dependencies = self._layer([
            self._check(
                "application_dependencies",
                readiness,
                ok=bool(readiness.get("ok") and readiness_body.get("status") == "ok" and not critical_failed and not degraded_failed),
                detail={"status": readiness_body.get("status"), "critical_failed": critical_failed, "degraded_failed": degraded_failed},
            ),
            self._database_write_check(),
            self._storage_write_check(),
            self._check("storage_registry", storage_api, ok=bool(storage_api.get("ok"))),
            self._check("job_registry", jobs, ok=bool(jobs.get("ok"))),
        ])

        trading = self._request("GET", "/api/root/trading/background/status")
        ai_agent = self._request("GET", "/api/ai-agent/status")
        background = self._layer([
            self._check("trading_background_engine", trading, ok=bool(trading.get("ok"))),
            self._check("ai_agent_runtime", ai_agent, ok=bool(ai_agent.get("ok"))),
        ])

        security = self._request("GET", "/api/admin/security-center")
        log_chain = self._request("GET", "/api/root/server-mode/logs/verify")
        points_chain = self._terminal_check("points_chain_invariants", "/api/root/points/chain/verify")
        finance = self._terminal_check("financial_invariants", "/api/root/points/financial-invariants")
        domain = self._layer([
            self._check("security_center", security, ok=bool(security.get("ok"))),
            self._check("log_chain", log_chain, ok=bool(log_chain.get("ok"))),
            points_chain,
            finance,
        ])
        return self._result({
            "transport_alive": transport,
            "application_ready": application,
            "dependencies_ready": dependencies,
            "background_workers_ready": background,
            "domain_invariants_ready": domain,
        }, started)

    def _result(self, layers: Mapping[str, Mapping[str, Any]], started: float) -> dict[str, Any]:
        return {
            "schema_version": READINESS_SCHEMA_VERSION,
            "checked_at": utc_now(),
            "base_url": self.base_url,
            "elapsed_seconds": round(self.monotonic() - started, 3),
            "layers": dict(layers),
            "overall": all(bool(layer.get("ok")) for layer in layers.values()),
        }

    def wait_until_ready(self, *, timeout_seconds: float, consecutive_passes: int = 2) -> dict[str, Any]:
        deadline = self.monotonic() + max(0.1, float(timeout_seconds))
        required = max(1, int(consecutive_passes))
        consecutive = 0
        attempts: list[dict[str, Any]] = []
        while self.monotonic() < deadline:
            result = self.probe_once()
            attempts.append(result)
            consecutive = consecutive + 1 if result.get("overall") else 0
            if consecutive >= required:
                return {
                    "schema_version": READINESS_SCHEMA_VERSION,
                    "overall": True,
                    "consecutive_passes": consecutive,
                    "required_consecutive_passes": required,
                    "attempts": attempts,
                    "final": result,
                }
            self.sleeper(min(1.0, max(0.0, deadline - self.monotonic())))
        return {
            "schema_version": READINESS_SCHEMA_VERSION,
            "overall": False,
            "consecutive_passes": consecutive,
            "required_consecutive_passes": required,
            "attempts": attempts,
            "final": attempts[-1] if attempts else {},
        }

