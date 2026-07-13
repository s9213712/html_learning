from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from scripts.testing.campaign_readiness import LayeredReadinessProbe, ReadinessConfig


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


class FakeSession:
    verify = False

    def __init__(self, routes: dict[tuple[str, str], list[FakeResponse] | FakeResponse]):
        self.routes = routes

    def request(self, method: str, url: str, **_kwargs: Any) -> FakeResponse:
        path = "/" + url.split("/", 3)[3]
        key = (method, path)
        value = self.routes[key]
        if isinstance(value, list):
            if len(value) > 1:
                return value.pop(0)
            return value[0]
        return value


def runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "database").mkdir(parents=True)
    sqlite3.connect(root / "database" / "database.db").close()
    (root / "storage").mkdir()
    return root


def success_routes(*, failed_chain: bool = False) -> dict[tuple[str, str], FakeResponse | list[FakeResponse]]:
    ok = FakeResponse(200, {"ok": True})
    return {
        ("GET", "/api/version"): FakeResponse(200, {"ok": True, "version": "test"}),
        ("GET", "/api/csrf-token"): FakeResponse(200, {"ok": True, "csrf_token": "csrf"}),
        ("POST", "/api/login"): ok,
        ("GET", "/api/me"): ok,
        ("GET", "/api/admin/health/readiness"): FakeResponse(200, {"ok": True, "readiness": {"status": "ok", "checks": [{"name": "database_integrity", "ok": True, "severity": "critical"}]}}),
        ("GET", "/api/admin/storage/summary"): ok,
        ("GET", "/api/admin/jobs?limit=1"): ok,
        ("GET", "/api/root/trading/background/status"): ok,
        ("GET", "/api/ai-agent/status"): ok,
        ("GET", "/api/admin/security-center"): ok,
        ("GET", "/api/root/server-mode/logs/verify"): ok,
        ("GET", "/api/root/points/chain/verify"): FakeResponse(202, {"ok": True, "job_uuid": "chain-1", "status_url": "/jobs/chain-1", "latest_snapshot_url": "/snapshots/chain"}),
        ("GET", "/jobs/chain-1"): FakeResponse(200, {"ok": True, "job": {"status": "failed" if failed_chain else "success"}}),
        ("GET", "/snapshots/chain"): ok,
        ("GET", "/api/root/points/financial-invariants"): FakeResponse(202, {"ok": True, "job_uuid": "finance-1", "status_url": "/jobs/finance-1", "latest_snapshot_url": "/snapshots/finance"}),
        ("GET", "/jobs/finance-1"): FakeResponse(200, {"ok": True, "job": {"status": "success"}}),
        ("GET", "/snapshots/finance"): ok,
    }


def probe(tmp_path: Path, routes: dict[tuple[str, str], FakeResponse | list[FakeResponse]]) -> LayeredReadinessProbe:
    return LayeredReadinessProbe(
        ReadinessConfig(base_url="https://campaign.invalid", username="root", password="secret", runtime_root=runtime(tmp_path)),
        session=FakeSession(routes),
        sleeper=lambda _seconds: None,
    )


def test_layered_readiness_follows_async_jobs_to_terminal_and_side_effect(tmp_path: Path) -> None:
    result = probe(tmp_path, success_routes()).probe_once()

    assert result["overall"] is True
    assert set(result["layers"]) == {
        "transport_alive",
        "application_ready",
        "dependencies_ready",
        "background_workers_ready",
        "domain_invariants_ready",
    }
    domain = result["layers"]["domain_invariants_ready"]["checks"]
    chain = next(item for item in domain if item["name"] == "points_chain_invariants")
    assert chain["detail"]["terminal_status"] == "success"
    assert chain["detail"]["snapshot_verified"] is True


def test_http_202_with_failed_terminal_job_is_not_ready(tmp_path: Path) -> None:
    result = probe(tmp_path, success_routes(failed_chain=True)).probe_once()

    assert result["overall"] is False
    domain = result["layers"]["domain_invariants_ready"]
    assert domain["ok"] is False
    chain = next(item for item in domain["checks"] if item["name"] == "points_chain_invariants")
    assert chain["error"] == "terminal_job_failed"


def test_transport_200_does_not_fake_application_readiness(tmp_path: Path) -> None:
    routes = success_routes()
    routes[("POST", "/api/login")] = FakeResponse(401, {"ok": False})
    result = probe(tmp_path, routes).probe_once()

    assert result["layers"]["transport_alive"]["ok"] is True
    assert result["layers"]["application_ready"]["ok"] is False
    assert result["layers"]["dependencies_ready"]["blocked_by"] == "application_ready"
    assert result["overall"] is False


def test_dependency_readiness_requires_db_and_storage_write_round_trip(tmp_path: Path) -> None:
    result = probe(tmp_path, success_routes()).probe_once()
    dependencies = {row["name"]: row for row in result["layers"]["dependencies_ready"]["checks"]}

    assert dependencies["database_writable"]["ok"] is True
    assert dependencies["database_writable"]["detail"]["transaction_rolled_back"] is True
    assert dependencies["storage_writable"]["ok"] is True
    assert dependencies["storage_writable"]["detail"]["cleanup"] is True
