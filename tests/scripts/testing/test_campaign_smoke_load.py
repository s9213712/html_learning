from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from scripts.testing.campaign_smoke_load import (
    DEFAULT_MINIMUM_OPERATIONS,
    LOAD_SAMPLE_CADENCE_SECONDS,
    PASSWORD_ENV_NAMES,
    SMOKE_CONCURRENCY,
    SMOKE_DURATION_SECONDS,
    SMOKE_LOAD_SCHEMA_VERSION,
    SmokeCredentials,
    SmokeLoadConfig,
    SmokeLoadRunner,
    build_parser,
    main,
)


class FakeClock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now = 0.0
        self._epoch = datetime(2026, 7, 13, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += max(0.0, seconds)
        # Yield the GIL so every one of the 32 workers can make progress while
        # logical time advances much faster than wall time.
        time.sleep(0.001)

    def utc_now(self) -> str:
        with self._lock:
            value = self._epoch + timedelta(seconds=self._now)
        return value.isoformat().replace("+00:00", "Z")


class FakeResponse:
    def __init__(self, status: int, body: dict, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.content = b"json"
        self.headers = headers or {}

    def json(self) -> dict:
        return dict(self._body)


class FakeSession:
    def __init__(self, identity: int, *, fail_public: bool = False, explode_with: str = "", hollow_path: str = "") -> None:
        self.identity = identity
        self.fail_public = fail_public
        self.explode_with = explode_with
        self.hollow_path = hollow_path
        self.logged_in = False
        self.closed = False

    def request(self, method: str, url: str, *, headers: dict, json: dict | None, timeout: float) -> FakeResponse:
        del timeout
        path = urlsplit(url).path
        if self.explode_with:
            raise OSError(self.explode_with)
        if self.fail_public and path == "/api/version":
            return FakeResponse(503, {"ok": False})
        if self.hollow_path == path:
            return FakeResponse(200, {"ok": True})
        if path == "/api/csrf-token":
            prefix = "user" if self.logged_in else "public"
            return FakeResponse(200, {"ok": True, "csrf_token": f"{prefix}-{self.identity}"})
        if path == "/api/login":
            assert method == "POST"
            assert headers.get("X-CSRF-Token") == f"public-{self.identity}"
            assert json == {"username": "test", "password": "only-in-memory"}
            self.logged_in = True
            return FakeResponse(200, {"ok": True, "msg": "logged in"})
        if path == "/api/me":
            return FakeResponse(200 if self.logged_in else 401, {
                "ok": self.logged_in,
                "id": 1,
                "username": "test",
                "role": "user",
                "status": "active",
            })
        if path == "/api/points/wallet":
            return FakeResponse(200 if self.logged_in else 401, {
                "ok": self.logged_in,
                "wallet": {"points_balance": 100, "points_frozen": 0},
            })
        if path == "/api/version":
            return FakeResponse(200, {
                "ok": True,
                "app": "hackme",
                "release_id": "test-release",
                "version": "test-version",
                "started_at": "2026-07-13T00:00:00Z",
                "server_time": {},
            })
        if path == "/api/readyz":
            return FakeResponse(200, {
                "ok": True,
                "status": "ready",
                "checks": {"db": {"ok": True}},
                "backpressure": {},
            })
        return FakeResponse(404, {"ok": False})

    def close(self) -> None:
        self.closed = True


class FakeSessionFactory:
    def __init__(self, *, fail_public: bool = False, explode_with: str = "", hollow_path: str = "") -> None:
        self._lock = threading.Lock()
        self._next = 0
        self.sessions: list[FakeSession] = []
        self.fail_public = fail_public
        self.explode_with = explode_with
        self.hollow_path = hollow_path

    def __call__(self) -> FakeSession:
        with self._lock:
            session = FakeSession(
                self._next,
                fail_public=self.fail_public,
                explode_with=self.explode_with,
                hollow_path=self.hollow_path,
            )
            self._next += 1
            self.sessions.append(session)
            return session


def config(tmp_path: Path, **changes) -> SmokeLoadConfig:
    values = {
        "base_url": "http://127.0.0.1:54321",
        "report_path": tmp_path / "report.json",
        "stop_file": tmp_path / "stop",
        "duration_seconds": 0.08,
        "concurrency": 32,
        "minimum_runtime_seconds": 0.07,
        "minimum_operations": 32,
        "request_timeout_seconds": 0.25,
        "operation_interval_seconds": 0.0,
        "monitor_interval_seconds": 0.002,
        "silent_worker_seconds": 1.0,
        "enforce_level0_contract": False,
        "supervisor_controlled": False,
    }
    values.update(changes)
    return SmokeLoadConfig(**values)


def credentials() -> SmokeCredentials:
    return SmokeCredentials(username="test", password="only-in-memory")


def test_level0_cli_contract_is_fixed_and_has_no_credential_arguments(tmp_path: Path) -> None:
    cfg = SmokeLoadConfig(
        base_url="http://localhost:54321",
        report_path=tmp_path / "report.json",
        stop_file=tmp_path / "stop",
    )
    assert cfg.duration_seconds == SMOKE_DURATION_SECONDS
    assert cfg.concurrency == SMOKE_CONCURRENCY
    assert cfg.minimum_operations == DEFAULT_MINIMUM_OPERATIONS
    assert cfg.request_timeout_seconds == 10.0

    with pytest.raises(ValueError, match="fixed at 180 seconds"):
        SmokeLoadConfig(
            base_url="http://localhost:54321",
            report_path=tmp_path / "report.json",
            stop_file=tmp_path / "stop",
            duration_seconds=179,
        )

    parser = build_parser()
    option_strings = {
        option
        for action in parser._actions
        for option in action.option_strings
    }
    assert not any("password" in option or "credential" in option for option in option_strings)
    assert "--duration" not in option_strings
    assert "--concurrency" not in option_strings


def test_credentials_are_loaded_from_environment_only() -> None:
    result = SmokeCredentials.from_environment({"HACKME_CAMPAIGN_TEST_PASSWORD": "secret"})
    assert result == SmokeCredentials(username="test", password="secret")
    custom = SmokeCredentials.from_environment({
        "HACKME_SMOKE_USERNAME": "qa-member",
        "HACKME_SMOKE_TEST_PASSWORD": "secret-2",
    })
    assert custom.username == "qa-member"
    with pytest.raises(ValueError, match="environment"):
        SmokeCredentials.from_environment({})


def test_bootstrap_failure_still_atomically_writes_schema_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PASSWORD_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    report_path = tmp_path / "bootstrap-failure.json"

    exit_code = main([
        "--base-url",
        "http://localhost:54321",
        "--report",
        str(report_path),
        "--stop-file",
        str(tmp_path / "stop"),
    ])

    assert exit_code == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == SMOKE_LOAD_SCHEMA_VERSION
    assert report["terminal"]["reason"] == "BOOTSTRAP_FAILURE"
    assert report["ok"] is False
    assert not list(tmp_path.glob(".bootstrap-failure.json.*.tmp"))


def test_successful_mix_proves_32_workers_csrf_rotation_and_atomic_report(tmp_path: Path) -> None:
    sessions = FakeSessionFactory()
    runner = SmokeLoadRunner(config(tmp_path), credentials(), session_factory=sessions, clock=FakeClock())

    report = runner.run()

    assert report["schema_version"] == SMOKE_LOAD_SCHEMA_VERSION
    assert report["scope"]["level"] == 0
    assert report["scope"]["full_feature_coverage_claimed"] is False
    assert report["terminal"]["reason"] == "DURATION_COMPLETE"
    assert report["ok"] is True
    assert report["classification"] == "PASS"
    assert report["contract"]["tls_verify"] is False
    assert all(report["gates"].values())
    assert report["gates"]["ordinary_latency_p95_within_3s"] is True
    assert report["gates"]["ordinary_latency_p99_within_8s"] is True
    assert report["gates"]["auth_login_p99_within_8s"] is True
    assert report["metrics"]["workers_ready"] == 32
    assert report["metrics"]["workers_completed"] == 32
    assert report["metrics"]["csrf_rotations"] == 16
    assert report["metrics"]["operations_completed"] >= 32
    assert report["metrics"]["max_active_workers"] == 32
    assert report["metrics"]["inflight_final"] == 0
    assert report["metrics"]["status_counts"] == {"200": report["metrics"]["attempts"]}
    assert report["metrics"]["wire_attempts"] == report["metrics"]["attempts"]
    assert report["metrics"]["logical_successes"] == report["metrics"]["operations_completed"]
    assert report["metrics"]["logical_failures"] == 0
    assert report["metrics"]["throughput_by_second"]
    assert report["metrics"]["load_samples"]
    assert report["metrics"]["operations"]["auth.login"]["latency_ms"]["samples"] == 16
    assert all({
        "elapsed_seconds",
        "active_workers",
        "active_workers_min",
        "active_workers_max",
        "inflight_requests",
        "operations_completed",
        "operations_completed_delta",
        "terminal",
    } <= set(row) for row in report["metrics"]["load_samples"])
    sample_policy = report["metrics"]["load_sample_policy"]
    assert sample_policy["cadence_seconds"] == LOAD_SAMPLE_CADENCE_SECONDS
    assert sample_policy["actual_samples"] <= sample_policy["maximum_samples"]
    assert sample_policy["overflowed"] is False
    assert report["metrics"]["load_samples"][-1]["terminal"] is True
    assert all(session.closed for session in sessions.sessions)
    assert all(session.verify is False for session in sessions.sessions)

    disk = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert disk == report
    assert "only-in-memory" not in (tmp_path / "report.json").read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".report.json.*.tmp"))


def test_preexisting_stop_file_is_normal_terminal_but_not_fake_green(tmp_path: Path) -> None:
    cfg = config(tmp_path, minimum_runtime_seconds=0.01)
    cfg.stop_file.write_text("stop", encoding="utf-8")
    runner = SmokeLoadRunner(cfg, credentials(), session_factory=FakeSessionFactory(), clock=FakeClock())

    report = runner.run()

    assert report["terminal"] == {
        "state": "STOPPED",
        "reason": "STOP_FILE",
        "graceful": True,
        "normal": True,
        "error": "",
    }
    assert report["ok"] is False
    assert report["gates"]["minimum_runtime"] is False
    assert json.loads(cfg.report_path.read_text(encoding="utf-8"))["terminal"]["reason"] == "STOP_FILE"


def test_supervisor_stop_file_passes_only_after_required_runtime_and_gates(tmp_path: Path) -> None:
    clock = FakeClock()
    cfg = config(tmp_path, supervisor_controlled=True)
    runner = SmokeLoadRunner(cfg, credentials(), session_factory=FakeSessionFactory(), clock=clock)

    def release() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if clock.monotonic() >= 0.075 and runner._successful_operations >= 32:
                cfg.stop_file.write_text("stop", encoding="utf-8")
                return
            time.sleep(0.001)

    control = threading.Thread(target=release, daemon=True)
    control.start()
    report = runner.run()
    control.join(timeout=1)

    assert report["terminal"]["reason"] == "STOP_FILE"
    assert report["runtime_seconds"] >= cfg.minimum_runtime_seconds
    assert report["ok"] is True
    assert all(report["gates"].values())


def test_sigterm_request_gracefully_stops_and_writes_interrupted_report(tmp_path: Path) -> None:
    cfg = config(
        tmp_path,
        duration_seconds=5.0,
        minimum_runtime_seconds=1.0,
        monitor_interval_seconds=0.01,
    )
    runner = SmokeLoadRunner(cfg, credentials(), session_factory=FakeSessionFactory())
    holder: list[dict] = []
    thread = threading.Thread(target=lambda: holder.append(runner.run()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if runner._successful_operations > 0:
            break
        time.sleep(0.005)
    runner.request_stop("SIGTERM")
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert holder[0]["terminal"]["reason"] == "SIGTERM"
    assert holder[0]["terminal"]["graceful"] is True
    assert holder[0]["terminal"]["normal"] is False
    assert holder[0]["ok"] is False
    assert json.loads(cfg.report_path.read_text(encoding="utf-8"))["terminal"]["state"] == "INTERRUPTED"


def test_http_or_transport_errors_fail_closed_without_leaking_secret(tmp_path: Path) -> None:
    bad_status = SmokeLoadRunner(
        config(tmp_path / "status"),
        credentials(),
        session_factory=FakeSessionFactory(fail_public=True),
        clock=FakeClock(),
    ).run()
    assert bad_status["ok"] is False
    assert bad_status["gates"]["zero_unexpected_errors"] is False
    assert bad_status["metrics"]["status_counts"]["503"] > 0

    secret = credentials().password
    transport_config = config(tmp_path / "transport")
    transport = SmokeLoadRunner(
        transport_config,
        credentials(),
        session_factory=FakeSessionFactory(explode_with=f"network failed near {secret}"),
        clock=FakeClock(),
    ).run()
    assert transport["ok"] is False
    assert transport["classification"] == "FAIL_INFRA"
    assert transport["gates"]["zero_transport_errors"] is False
    serialized = transport_config.report_path.read_text(encoding="utf-8")
    assert secret not in serialized
    assert "[redacted]" in serialized


def test_http_200_with_hollow_payload_is_not_function_success(tmp_path: Path) -> None:
    hollow = SmokeLoadRunner(
        config(tmp_path),
        credentials(),
        session_factory=FakeSessionFactory(hollow_path="/api/version"),
        clock=FakeClock(),
    ).run()

    assert hollow["metrics"]["status_counts"]["200"] > 0
    assert hollow["ok"] is False
    assert hollow["gates"]["zero_unexpected_errors"] is False
    assert any(
        "version_identity_missing" in message
        for message in hollow["unexpected_errors"]
    )


def test_controlled_503_is_retried_to_terminal_semantic_success(tmp_path: Path) -> None:
    clock = FakeClock()
    runner = SmokeLoadRunner(
        config(tmp_path),
        credentials(),
        session_factory=FakeSessionFactory(),
        clock=clock,
    )
    runner._worker_state[0] = {
        "last_progress_monotonic": 0.0,
        "logical_requests": 0,
        "logical_successes": 0,
    }

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, *_args: object, **_kwargs: object) -> FakeResponse:
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(
                    503,
                    {
                        "ok": False,
                        "error": "server_busy",
                        "retry_after_seconds": 1,
                    },
                    {"X-Hackme-Backpressure-Rejected": "1"},
                )
            return FakeResponse(200, {
                "ok": True,
                "app": "hackme",
                "release_id": "test-release",
                "version": "test-version",
                "started_at": "2026-07-13T00:00:00Z",
                "server_time": {},
            })

    session = Session()
    ok, body = runner._request(
        session,  # type: ignore[arg-type]
        0,
        "GET",
        "/api/version",
        operation="public:/api/version",
    )

    assert ok is True
    assert body["ok"] is True
    assert session.calls == 2
    assert runner._backpressure_rejections == 1
    assert runner._backpressure_retry_logical_requests == 1
    assert runner._backpressure_retry_successes == 1
    assert runner._backpressure_retry_failures == 0
    operation = runner._operation_metrics["public:/api/version"]
    assert operation == {
        "logical_requests": 1,
        "wire_attempts": 2,
        "wire_controlled_rejections": 1,
        "wire_semantic_successes": 1,
        "logical_successes": 1,
    }
    assert runner._logical_requests == 1
    assert runner._successful_operations == 1
    assert runner._logical_failures == 0
    assert runner._unexpected_errors == []
    assert runner._max_inflight == 1
    assert runner._logical_latencies_ms == [1000.0]


def test_exhausted_controlled_503_is_one_terminal_logical_failure(tmp_path: Path) -> None:
    runner = SmokeLoadRunner(
        config(tmp_path),
        credentials(),
        session_factory=FakeSessionFactory(),
        clock=FakeClock(),
    )
    runner._worker_state[0] = {
        "last_progress_monotonic": 0.0,
        "logical_requests": 0,
        "logical_successes": 0,
    }

    class Session:
        calls = 0

        def request(self, *_args: object, **_kwargs: object) -> FakeResponse:
            self.calls += 1
            return FakeResponse(
                503,
                {"ok": False, "error": "server_busy", "retry_after_seconds": 0},
                {"X-Hackme-Backpressure-Rejected": "1"},
            )

    session = Session()
    ok, _ = runner._request(
        session,  # type: ignore[arg-type]
        0,
        "GET",
        "/api/version",
        operation="public:/api/version",
    )

    assert ok is False
    assert session.calls == 3
    operation = runner._operation_metrics["public:/api/version"]
    assert operation == {
        "logical_requests": 1,
        "wire_attempts": 3,
        "wire_controlled_rejections": 3,
        "logical_failures": 1,
    }
    assert runner._backpressure_retry_logical_requests == 1
    assert runner._backpressure_retry_successes == 0
    assert runner._backpressure_retry_failures == 1
    assert runner._logical_failures == 1
    assert len(runner._unexpected_errors) == 1


def test_load_sampling_is_cadenced_bounded_and_deduplicates_dense_polling(tmp_path: Path) -> None:
    runner = SmokeLoadRunner(
        config(
            tmp_path,
            duration_seconds=86_400.0,
            minimum_runtime_seconds=0.0,
            supervisor_controlled=False,
        ),
        credentials(),
        session_factory=FakeSessionFactory(),
        clock=FakeClock(),
    )
    runner._active_workers = 32
    runner._inflight = 12
    runner._successful_operations = 10

    # Dense polling at one timestamp used to append every observation.  It now
    # contributes to one interval aggregate without controlling artifact size.
    for _ in range(20_000):
        runner._record_load_observation(183.154321)
    assert len(runner._load_samples) == 1
    assert runner._load_samples[0]["observation_count"] == 1

    runner._successful_operations = 20
    runner._record_load_observation(184.2)
    runner._active_workers = 0
    runner._inflight = 0
    runner._record_load_observation(184.2, terminal=True)

    assert len(runner._load_samples) == 3
    assert runner._load_samples[1]["observation_count"] == 20_000
    assert runner._load_samples[1]["operations_completed_delta"] == 10
    assert runner._load_samples[-1]["terminal"] is True
    assert runner._maximum_load_samples() == 86_402
    assert len(runner._load_samples) <= runner._maximum_load_samples()
    assert runner._load_samples_overflowed is False


def test_supervisor_post_deadline_monitor_never_zero_sleeps_or_spins(tmp_path: Path) -> None:
    class StopAfterClock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []
            self.runner: SmokeLoadRunner | None = None

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds
            if len(self.sleeps) == 12:
                assert self.runner is not None
                self.runner.request_stop("STOP_FILE")

        def utc_now(self) -> str:
            return "2026-07-13T00:00:00Z"

    clock = StopAfterClock()
    runner = SmokeLoadRunner(
        config(
            tmp_path,
            duration_seconds=0.01,
            minimum_runtime_seconds=0.0,
            monitor_interval_seconds=0.002,
            supervisor_controlled=True,
        ),
        credentials(),
        session_factory=FakeSessionFactory(),
        clock=clock,
    )
    clock.runner = runner
    runner._run_started = 0.0

    runner._monitor()

    assert runner._monitor_iterations == 12
    assert len(clock.sleeps) == 12
    assert all(value == 0.002 for value in clock.sleeps)
    assert clock.now > runner.config.duration_seconds
