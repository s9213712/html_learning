from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.testing import campaign_observability as observability
from scripts.testing.campaign_observability import (
    GIB,
    MIB,
    RESOURCE_SAMPLE_SCHEMA_VERSION,
    ProcessRoleRegistry,
    ResourceCollector,
    ResourceCollectorConfig,
    collect_host_safety_preflight,
    wait_for_host_safety_preflight,
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
    write(cgroup / "memory.high", str(5 * GIB))
    write(cgroup / "memory.max", str(6 * GIB))
    write(cgroup / "memory.swap.max", str(512 * MIB))
    write(cgroup / "cpu.max", "300000 100000\n")
    write(cgroup / "pids.max", "384\n")
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
    assert sample["cgroup"]["limit_mismatches"]["memory.max"]["expected"] == str(6 * GIB)
    assert "CGROUP_LIMIT_DRIFT" in sample["hard_limit_state"]["tripped"]


@pytest.mark.parametrize(
    ("pressure_name", "pressure_value", "reason_code"),
    (
        ("io", 91.0, "HOST_IO_PRESSURE_HIGH"),
        ("memory", 12.0, "HOST_MEMORY_PRESSURE_HIGH"),
    ),
)
def test_host_pressure_trips_hard_stop(
    tmp_path: Path,
    pressure_name: str,
    pressure_value: float,
    reason_code: str,
) -> None:
    result = collector(tmp_path)
    write(
        result.config.proc_root / "pressure" / pressure_name,
        "some avg10=95.00 avg60=80.00 avg300=20.00 total=100\n"
        f"full avg10={pressure_value:.2f} avg60=70.00 avg300=10.00 total=100\n",
    )

    sample = result.collect(monotonic_ns=1_000_000_000)

    assert sample["hard_limit_state"]["ok"] is False
    assert reason_code in sample["hard_limit_state"]["tripped"]


def test_host_swap_and_load_trip_hard_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(observability.os, "cpu_count", lambda: 1)
    result = collector(tmp_path)
    result.config.maximum_host_load1_per_cpu = 0.01
    write(
        result.config.proc_root / "meminfo",
        "MemTotal:       16777216 kB\n"
        "MemAvailable:    8388608 kB\n"
        "SwapTotal:       1048576 kB\n"
        "SwapFree:         131072 kB\n",
    )

    sample = result.collect(monotonic_ns=1_000_000_000)

    assert "HOST_LOAD1_HIGH" in sample["hard_limit_state"]["tripped"]
    assert "HOST_SWAP_USAGE_HIGH" in sample["hard_limit_state"]["tripped"]


def test_host_safety_preflight_fails_closed_on_io_pressure(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    proc = fake_proc(
        tmp_path,
        major=os.major(os.stat(data).st_dev),
        minor=os.minor(os.stat(data).st_dev),
    )
    write(
        proc / "pressure" / "io",
        "some avg10=94.00 avg60=90.00 avg300=50.00 total=100\n"
        "full avg10=92.00 avg60=88.00 avg300=45.00 total=100\n",
    )

    result = collect_host_safety_preflight(proc_root=proc)

    assert result["ok"] is False
    assert result["errors"] == {}
    assert "HOST_IO_PRESSURE_HIGH" in result["tripped"]
    assert result["checks"]["host_io_pressure"]["value"] == {
        "avg10": 92.0,
        "avg60": 88.0,
    }


def test_host_startup_safety_uses_three_percent_io_headroom(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    proc = fake_proc(
        tmp_path,
        major=os.major(os.stat(data).st_dev),
        minor=os.minor(os.stat(data).st_dev),
    )
    write(
        proc / "pressure" / "io",
        "some avg10=3.00 avg60=3.00 avg300=1.00 total=100\n"
        "full avg10=3.00 avg60=3.00 avg300=1.00 total=100\n",
    )

    boundary = observability.collect_host_startup_safety_preflight(
        proc_root=proc,
    )
    default = collect_host_safety_preflight(proc_root=proc)

    assert boundary["ok"] is True
    assert boundary["checks"]["host_io_pressure"]["maximum"] == {
        "avg10": 3.0,
        "avg60": 3.0,
    }
    assert default["ok"] is True
    assert default["checks"]["host_io_pressure"]["maximum"] == {
        "avg10": 10.0,
        "avg60": 10.0,
    }

    write(
        proc / "pressure" / "io",
        "some avg10=3.01 avg60=3.00 avg300=1.00 total=100\n"
        "full avg10=3.01 avg60=3.00 avg300=1.00 total=100\n",
    )
    over = observability.collect_host_startup_safety_preflight(proc_root=proc)

    assert over["ok"] is False
    assert over["tripped"] == ["HOST_IO_PRESSURE_HIGH"]

    write(
        proc / "pressure" / "io",
        "some avg10=10.00 avg60=10.00 avg300=1.00 total=100\n"
        "full avg10=10.00 avg60=10.00 avg300=1.00 total=100\n",
    )
    exact_hard_boundary = (
        observability.collect_host_startup_safety_preflight(proc_root=proc)
    )
    hard_boundary_check = exact_hard_boundary["checks"][
        "host_io_pressure_hard_limit"
    ]
    assert hard_boundary_check["ok"] is True
    assert hard_boundary_check["exceeded"] is False
    assert exact_hard_boundary["tripped"] == ["HOST_IO_PRESSURE_HIGH"]

    for avg10, avg60 in ((10.01, 10.0), (10.0, 10.01)):
        write(
            proc / "pressure" / "io",
            f"some avg10={avg10:.2f} avg60={avg60:.2f} "
            "avg300=1.00 total=100\n"
            f"full avg10={avg10:.2f} avg60={avg60:.2f} "
            "avg300=1.00 total=100\n",
        )
        hard = observability.collect_host_startup_safety_preflight(
            proc_root=proc,
        )

        assert hard["checks"]["host_io_pressure_hard_limit"] == {
            "ok": False,
            "evaluated": True,
            "exceeded": True,
            "value": {"avg10": avg10, "avg60": avg60},
            "maximum": {"avg10": 10.0, "avg60": 10.0},
            "reason_code": "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
        }
        assert hard["tripped"] == [
            "HOST_IO_PRESSURE_HIGH",
            "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
        ]

    waited = wait_for_host_safety_preflight(
        collector=lambda: hard,
        sleeper=lambda _seconds: pytest.fail("hard limit must not wait"),
    )

    assert waited["admission_wait"]["sample_count"] == 1
    assert waited["admission_wait"]["non_waitable"] == [
        "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
    ]


def test_host_safety_preflight_rejects_missing_telemetry(tmp_path: Path) -> None:
    result = collect_host_safety_preflight(proc_root=tmp_path / "missing-proc")

    assert result["ok"] is False
    assert "HOST_SAFETY_TELEMETRY_INCOMPLETE" in result["tripped"]
    assert result["errors"]


def test_host_safety_admission_wait_requires_two_safe_io_samples() -> None:
    unsafe = {
        "at": "t0",
        "ok": False,
        "tripped": ["HOST_IO_PRESSURE_HIGH"],
        "checks": {"host_io_pressure": {"value": {"avg10": 5.0, "avg60": 5.0}}},
    }
    safe = {
        "at": "t1",
        "ok": True,
        "tripped": [],
        "checks": {"host_io_pressure": {"value": {"avg10": 2.0, "avg60": 3.0}}},
    }
    samples = iter((unsafe, safe, safe))
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    result = wait_for_host_safety_preflight(
        timeout_seconds=5.0,
        poll_seconds=1.0,
        collector=lambda: next(samples),
        clock=lambda: now[0],
        sleeper=sleep,
    )

    assert result["ok"] is True
    assert result["admission_wait"]["sample_count"] == 3
    assert result["admission_wait"]["waited_seconds"] == 2.0


def test_host_safety_admission_wait_rejects_non_io_failure_immediately() -> None:
    now = [0.0]
    result = wait_for_host_safety_preflight(
        timeout_seconds=30.0,
        collector=lambda: {
            "at": "t0",
            "ok": False,
            "tripped": ["HOST_MEMORY_AVAILABLE_LOW"],
            "checks": {},
        },
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert result["ok"] is False
    assert result["admission_wait"]["sample_count"] == 1
    assert result["admission_wait"]["non_waitable"] == [
        "HOST_MEMORY_AVAILABLE_LOW"
    ]


def test_host_safety_admission_wait_times_out_without_relaxing_io_limit() -> None:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    result = wait_for_host_safety_preflight(
        timeout_seconds=2.0,
        poll_seconds=1.0,
        collector=lambda: {
            "at": "busy",
            "ok": False,
            "tripped": ["HOST_IO_PRESSURE_HIGH"],
            "checks": {
                "host_io_pressure": {"value": {"avg10": 5.0, "avg60": 5.0}}
            },
        },
        clock=lambda: now[0],
        sleeper=sleep,
    )

    assert result["ok"] is False
    assert "HOST_IO_PRESSURE_HIGH" in result["tripped"]
    assert "HOST_SAFETY_ADMISSION_TIMEOUT" in result["tripped"]
    assert result["admission_wait"]["sample_count"] == 3


def _diskstats_row(
    *,
    reads: int = 0,
    read_ms: int = 0,
    writes: int = 0,
    write_ms: int = 0,
    in_flight: int = 0,
    weighted_io_ms: int = 0,
    flushes: int = 0,
    flush_ms: int = 0,
) -> str:
    return (
        f"8 48 sdd {reads} 0 0 {read_ms} {writes} 0 0 {write_ms} "
        f"{in_flight} 0 {weighted_io_ms} 0 0 0 0 {flushes} {flush_ms}"
    )


def test_startup_block_io_sampler_rejects_a_delayed_rolling_window() -> None:
    now = [0.0]
    row = [_diskstats_row()]
    sampler = observability.HostStartupBlockIoSampler(
        device_major_minor=(8, 48),
        read_text=lambda _path: row[0],
        clock=lambda: now[0],
    )

    assert sampler.sample()["status"] == "pending"
    for second in (1.0, 2.0, 3.0):
        now[0] = second
        assert sampler.sample()["status"] == "pending"

    row[0] = _diskstats_row(
        writes=10,
        write_ms=100,
        weighted_io_ms=400,
        flushes=2,
        flush_ms=80,
    )
    now[0] = 4.0
    safe = sampler.sample()
    assert safe["status"] == "safe"
    assert safe["interval_seconds"] == 4.0

    row[0] = _diskstats_row(
        writes=20,
        write_ms=1400,
        weighted_io_ms=6000,
        flushes=4,
        flush_ms=600,
    )
    now[0] = 5.0
    unsafe = sampler.sample()
    assert unsafe["status"] == "unsafe"
    assert unsafe["reason_codes"] == [
        "block_io_write_await_high",
        "block_io_flush_await_high",
        "block_io_average_queue_high",
    ]


def test_host_safety_wait_treats_block_pending_as_waitable() -> None:
    now = [0.0]
    block_samples = iter((
        {
            "status": "pending",
            "safe": False,
            "reason_codes": ["block_io_baseline_pending"],
        },
        {"status": "safe", "safe": True, "reason_codes": []},
        {"status": "safe", "safe": True, "reason_codes": []},
    ))

    class BlockSampler:
        @staticmethod
        def sample() -> dict[str, object]:
            return next(block_samples)

    def collect() -> dict[str, object]:
        return {
            "at": "safe",
            "ok": True,
            "tripped": [],
            "checks": {
                "host_io_pressure": {
                    "value": {"avg10": 1.0, "avg60": 1.0},
                },
            },
        }

    result = wait_for_host_safety_preflight(
        timeout_seconds=5.0,
        poll_seconds=1.0,
        required_consecutive_safe=2,
        collector=collect,
        block_io_sampler=BlockSampler(),  # type: ignore[arg-type]
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert result["ok"] is True
    assert result["block_io"]["status"] == "safe"
    assert result["admission_wait"]["sample_count"] == 3
    assert result["admission_wait"]["waited_seconds"] == 2.0


def test_host_safety_wait_rejects_unavailable_block_telemetry() -> None:
    class BlockSampler:
        @staticmethod
        def sample() -> dict[str, object]:
            return {
                "status": "unavailable",
                "safe": False,
                "reason_codes": ["block_io_telemetry_unavailable"],
            }

    result = wait_for_host_safety_preflight(
        collector=lambda: {
            "at": "safe",
            "ok": True,
            "tripped": [],
            "checks": {
                "host_io_pressure": {
                    "value": {"avg10": 1.0, "avg60": 1.0},
                },
            },
        },
        block_io_sampler=BlockSampler(),  # type: ignore[arg-type]
        sleeper=lambda _seconds: pytest.fail("telemetry failure must not wait"),
    )

    assert result["ok"] is False
    assert result["admission_wait"]["non_waitable"] == [
        "HOST_BLOCK_DEVICE_TELEMETRY_INCOMPLETE",
    ]


def test_gpu_temperature_trips_hard_stop_before_thermal_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = collector(tmp_path)
    result.config.require_gpu = True
    monkeypatch.setattr(
        result,
        "_collect_gpu",
        lambda _errors: [{
            "index": 0,
            "utilization_percent": 40.0,
            "memory_used_mib": 1024.0,
            "memory_total_mib": 8192.0,
            "temperature_c": 81.0,
        }],
    )

    sample = result.collect(monotonic_ns=1_000_000_000)

    assert sample["hard_limit_state"]["ok"] is False
    assert "GPU_TEMPERATURE_HIGH" in sample["hard_limit_state"]["tripped"]


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
