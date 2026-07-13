from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.testing import campaign_observability as observability
from scripts.testing.campaign_observability import (
    GIB,
    RESOURCE_SAMPLE_SCHEMA_VERSION,
    ProcessRoleRegistry,
    ResourceCollector,
    ResourceCollectorConfig,
)


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def fake_proc(tmp_path: Path, *, major: int, minor: int) -> Path:
    proc = tmp_path / "proc"
    write(proc / "loadavg", "0.10 0.20 0.30 1/100 1\n")
    write(
        proc / "meminfo",
        "MemTotal:       16777216 kB\nMemAvailable:    8388608 kB\nSwapTotal:       1048576 kB\nSwapFree:         524288 kB\n",
    )
    for name in ("cpu", "memory", "io"):
        write(proc / "pressure" / name, "some avg10=0.10 avg60=0.20 avg300=0.30 total=100\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    write(proc / "diskstats", f"{major} {minor} testdisk 10 0 100 20 20 0 200 30 0 40 50 0 0 0 0 0 0\n")
    return proc


def fake_cgroup(tmp_path: Path) -> Path:
    cgroup = tmp_path / "cgroup"
    write(cgroup / "cpu.stat", "usage_usec 1000\nnr_throttled 0\nthrottled_usec 0\n")
    write(cgroup / "memory.current", "1048576\n")
    write(cgroup / "memory.swap.current", "0\n")
    write(cgroup / "memory.events", "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    write(cgroup / "memory.swap.events", "high 0\nmax 0\nfail 0\n")
    write(cgroup / "pids.current", "5\n")
    write(cgroup / "pids.events", "max 0\n")
    write(cgroup / "memory.high", str(7 * GIB))
    write(cgroup / "memory.max", str(8 * GIB))
    write(cgroup / "memory.swap.max", str(GIB))
    write(cgroup / "cpu.max", "600000 100000\n")
    write(cgroup / "pids.max", "768\n")
    return cgroup


def collector(
    tmp_path: Path,
    *,
    cgroup_event_baseline: dict[str, dict[str, int]] | None = None,
) -> ResourceCollector:
    data = tmp_path / "data"
    data.mkdir()
    proc = fake_proc(tmp_path, major=os.major(os.stat(data).st_dev), minor=os.minor(os.stat(data).st_dev))
    runtime = tmp_path / "runtime"
    write(runtime / "database" / "database.db", "db")
    write(runtime / "database" / "database.db-wal", "wal")
    config = ResourceCollectorConfig(
        cgroup_path=fake_cgroup(tmp_path),
        sample_path=tmp_path / "artifacts" / "resource.jsonl",
        runtime_roots={"primary": runtime},
        campaign_data_root=data,
        proc_root=proc,
        minimum_disk_free_bytes=0,
        cgroup_event_baseline=cgroup_event_baseline or {},
    )
    return ResourceCollector(config)


def test_resource_sample_is_self_describing_and_completeness_uses_fields(tmp_path: Path) -> None:
    result = collector(tmp_path)
    first = result.collect(monotonic_ns=1_000_000_000)
    # First delta sample has no block-I/O rate or WAL rate and must say so.
    assert first["sample_schema_version"] == RESOURCE_SAMPLE_SCHEMA_VERSION
    assert "host.block_io.read_bytes_per_second" in first["expected_fields"]
    assert "host.block_io.read_bytes_per_second" in first["missing_fields"]
    assert "databases.wal_growth_bytes_per_second" in first["missing_fields"]
    assert first["hard_limit_state"]["ok"] is True

    diskstats = result.config.proc_root / "diskstats"
    major = os.major(os.stat(result.config.campaign_data_root).st_dev)
    minor = os.minor(os.stat(result.config.campaign_data_root).st_dev)
    write(diskstats, f"{major} {minor} testdisk 12 0 120 24 25 0 250 36 0 45 60 0 0 0 0 0 0\n")
    second = result.collect(monotonic_ns=6_000_000_000)

    assert second["host"]["block_io"]["read_bytes_per_second"] > 0
    assert second["host"]["block_io"]["write_bytes_per_second"] > 0
    assert second["databases"]["wal_growth_bytes_per_second"] == 0
    assert second["collector_errors"] == {}
    assert len(second["valid_fields"]) > len(second["missing_fields"])


def test_summary_fails_when_any_mandatory_field_is_below_95_percent(tmp_path: Path) -> None:
    result = collector(tmp_path)
    result.collect(monotonic_ns=1_000_000_000)
    summary = result.summary(minimum_ratio=0.95)

    assert summary["samples"] == 1
    assert summary["ok"] is False
    assert "host.block_io.await_ms" in summary["mandatory_fields_below_threshold"]
    assert "databases.wal_growth_bytes_per_second" in summary["mandatory_fields_below_threshold"]


def test_oom_counter_increase_trips_hard_stop(tmp_path: Path) -> None:
    result = collector(
        tmp_path,
        cgroup_event_baseline={
            "memory.events": {"max": 0, "oom": 0, "oom_kill": 0},
            "pids.events": {"max": 0},
        },
    )
    write(result.config.cgroup_path / "memory.events", "low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    sample = result.collect(monotonic_ns=1_000_000_000)

    assert sample["hard_limit_state"]["ok"] is False
    assert "CGROUP_OOM_COUNTER_INCREASED" in sample["hard_limit_state"]["tripped"]
    assert sample["hard_limit_state"]["checks"]["cgroup_oom"]["value"][
        "memory.events.oom_kill"
    ]["delta"] == 1


def test_cgroup_limit_drift_is_fail_closed(tmp_path: Path) -> None:
    result = collector(tmp_path)
    write(result.config.cgroup_path / "memory.max", str(9 * GIB))
    sample = result.collect(monotonic_ns=1_000_000_000)

    assert sample["cgroup"]["limits_verified"] is False
    assert sample["cgroup"]["limit_mismatches"]["memory.max"]["expected"] == str(8 * GIB)
    assert "CGROUP_LIMIT_DRIFT" in sample["hard_limit_state"]["tripped"]


def test_process_registry_requires_pid_start_identity(tmp_path: Path) -> None:
    registry = ProcessRoleRegistry()
    identity = registry.register(
        "load_generator",
        os.getpid(),
        start_ticks=observability.process_start_ticks(os.getpid()),
        required=True,
    )
    assert registry.snapshot()["load_generator"][identity] is True
    registry.unregister("load_generator", identity)
    assert registry.snapshot() == {}


def test_process_role_fields_remain_a_set_when_cgroup_membership_is_checked(tmp_path: Path) -> None:
    membership = ""
    for row in Path(f"/proc/{os.getpid()}/cgroup").read_text(encoding="utf-8").splitlines():
        parts = row.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            membership = "/" + parts[2].strip().lstrip("/")
            break
    assert membership
    registry = ProcessRoleRegistry()
    registry.register("security_sentinel", os.getpid(), required=True)
    data = tmp_path / "data"
    data.mkdir()
    config = ResourceCollectorConfig(
        cgroup_path=tmp_path / "unused-cgroup",
        sample_path=tmp_path / "unused.jsonl",
        runtime_roots={},
        campaign_data_root=data,
        process_registry=registry,
        expected_process_cgroup=membership,
    )
    result = ResourceCollector(config)
    errors: dict[str, str] = {}

    rows, expected_fields = result._collect_process_roles(errors)

    assert errors == {}
    assert rows["security_sentinel"]["process_count"] >= 1
    assert "process_roles.security_sentinel.fd_count" in expected_fields

    outside_registry = ProcessRoleRegistry(
        expected_cgroup=f"{membership.rstrip('/')}/not-the-current-cgroup"
    )
    with pytest.raises(RuntimeError, match="pid_outside_campaign_cgroup"):
        outside_registry.register("security_sentinel", os.getpid(), required=True)


def test_health_samples_require_ready_db_semantics_and_trip_hard_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = collector(tmp_path)
    result.config.health_targets = {"primary": "https://127.0.0.1:54443"}

    class Response:
        status_code = 200

        def __init__(self, payload: dict[str, object]):
            self.payload = payload

        def json(self) -> dict[str, object]:
            return self.payload

    healthy = Response({
        "ok": True,
        "status": "ready",
        "checks": {"db": {"ok": True}},
        "backpressure": {},
    })
    monkeypatch.setattr(observability.requests, "get", lambda *_args, **_kwargs: healthy)
    first = result.collect(monotonic_ns=1_000_000_000)

    assert first["health"]["primary"]["semantic_ready"] is True
    assert "health.primary.semantic_ready" in first["valid_fields"]
    assert first["hard_limit_state"]["ok"] is True

    hollow = Response({"ok": True})
    monkeypatch.setattr(observability.requests, "get", lambda *_args, **_kwargs: hollow)
    second = result.collect(monotonic_ns=2_000_000_000)

    assert second["health"]["primary"]["status_code"] == 200
    assert second["health"]["primary"]["semantic_ready"] is False
    assert "PRIMARY_READINESS_LOST" in second["hard_limit_state"]["tripped"]
